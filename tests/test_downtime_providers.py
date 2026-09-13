"""Anthropic SDK provider + provider factory. No network: the client is a stub."""

import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from epicvibe.downtime.config import DowntimeSettings
from epicvibe.downtime.providers import (
    AnthropicProviderError,
    AnthropicSdkProvider,
    KeywordFakeProvider,
    build_provider,
)
from epicvibe.downtime.schema import FilledTemplate, strict_json_schema

SCHEMA = strict_json_schema(FilledTemplate)
PAYLOAD = {"template_id": "ed-cap-admission", "patient_fields": [], "orders": [],
           "unresolved": [], "warnings": []}


def tool_use(payload, name="emit"):
    return SimpleNamespace(type="tool_use", name=name, input=payload)


class StubMessages:
    """Records every create() call and replays a scripted list of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class StubClient:
    def __init__(self, *responses):
        self.messages = StubMessages(responses)


def bad_request(message="strict schema rejected"):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(400, request=request, json={"error": {"message": message}})
    return anthropic.BadRequestError(message, response=response, body=None)


# --------------------------------------------------------------------------


async def test_forced_tool_call_shape():
    client = StubClient(SimpleNamespace(stop_reason="tool_use", content=[tool_use(PAYLOAD)]))
    provider = AnthropicSdkProvider(api_key="k", model="claude-opus-5", client=client)

    out = await provider.complete_json(system="sys", user="usr", json_schema=SCHEMA)
    assert out == PAYLOAD

    call = client.messages.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["max_tokens"] == 16000
    assert call["system"] == "sys"
    assert call["messages"] == [{"role": "user", "content": "usr"}]
    assert call["tool_choice"] == {"type": "tool", "name": "emit"}
    tool = call["tools"][0]
    assert tool["name"] == "emit" and tool["strict"] is True
    assert tool["input_schema"] is SCHEMA
    assert tool["input_schema"]["additionalProperties"] is False


async def test_tool_input_arriving_as_a_json_string_is_parsed():
    client = StubClient(SimpleNamespace(stop_reason="tool_use",
                                        content=[tool_use(json.dumps(PAYLOAD))]))
    provider = AnthropicSdkProvider(api_key="k", client=client)
    assert await provider.complete_json(system="s", user="u", json_schema=SCHEMA) == PAYLOAD


async def test_text_blocks_before_the_tool_use_are_skipped():
    client = StubClient(SimpleNamespace(stop_reason="tool_use", content=[
        SimpleNamespace(type="text", text="thinking out loud"),
        tool_use(PAYLOAD),
    ]))
    provider = AnthropicSdkProvider(api_key="k", client=client)
    assert await provider.complete_json(system="s", user="u", json_schema=SCHEMA) == PAYLOAD


async def test_refusal_raises_a_clear_error():
    client = StubClient(SimpleNamespace(
        stop_reason="refusal",
        stop_details=SimpleNamespace(type="refusal", category="bio", explanation="no"),
        content=[],
    ))
    provider = AnthropicSdkProvider(api_key="k", client=client)
    with pytest.raises(AnthropicProviderError, match="refused"):
        await provider.complete_json(system="s", user="u", json_schema=SCHEMA)


async def test_missing_tool_use_block_raises():
    client = StubClient(SimpleNamespace(stop_reason="end_turn",
                                        content=[SimpleNamespace(type="text", text="hi")]))
    provider = AnthropicSdkProvider(api_key="k", client=client)
    with pytest.raises(AnthropicProviderError, match="no 'emit' tool_use block"):
        await provider.complete_json(system="s", user="u", json_schema=SCHEMA)


async def test_a_single_bad_request_degrades_the_strict_tool_schema():
    """The clinician's capture must survive a schema the API will not take strictly."""
    client = StubClient(
        bad_request("tool schema is not strict-compatible"),
        SimpleNamespace(stop_reason="tool_use", content=[tool_use(PAYLOAD)]),
    )
    provider = AnthropicSdkProvider(api_key="k", client=client)
    assert provider._strict is True

    assert await provider.complete_json(system="s", user="u", json_schema=SCHEMA) == PAYLOAD
    assert provider._strict is False
    assert len(client.messages.calls) == 2
    assert client.messages.calls[0]["tools"][0]["strict"] is True
    assert "strict" not in client.messages.calls[1]["tools"][0]


async def test_strict_400_falls_back_to_non_strict_and_stays_there():
    client = StubClient(
        bad_request(),
        SimpleNamespace(stop_reason="tool_use", content=[tool_use(PAYLOAD)]),
        SimpleNamespace(stop_reason="tool_use", content=[tool_use(PAYLOAD)]),
    )
    provider = AnthropicSdkProvider(api_key="k", client=client)

    assert await provider.complete_json(system="s", user="u", json_schema=SCHEMA) == PAYLOAD
    assert "strict" in client.messages.calls[0]["tools"][0]
    assert "strict" not in client.messages.calls[1]["tools"][0]

    # The second call must not retry the strict form again.
    await provider.complete_json(system="s", user="u", json_schema=SCHEMA)
    assert "strict" not in client.messages.calls[2]["tools"][0]


async def test_non_400_errors_propagate():
    client = StubClient(RuntimeError("connection reset"))
    provider = AnthropicSdkProvider(api_key="k", client=client)
    with pytest.raises(RuntimeError, match="connection reset"):
        await provider.complete_json(system="s", user="u", json_schema=SCHEMA)


# --------------------------------------------------------------------------
# factory / settings
# --------------------------------------------------------------------------


def test_build_provider_defaults_to_the_keyword_provider():
    assert isinstance(build_provider(DowntimeSettings(provider="fake")), KeywordFakeProvider)


def test_build_provider_refuses_the_hosted_model_without_the_phi_gate(monkeypatch):
    # The transcript is ambient clinical audio: name, DOB, complaint. Opting in is
    # explicit, and the error has to name the env var that does it.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    settings = DowntimeSettings(provider="anthropic", anthropic_api_key="k")
    assert settings.allow_phi_to_model is False
    with pytest.raises(ValueError, match="EPICVIBE_DOWNTIME_ALLOW_PHI_TO_MODEL"):
        build_provider(settings)
    # ...and the refusal happens before the key is even looked at
    with pytest.raises(ValueError, match="EPICVIBE_DOWNTIME_ALLOW_PHI_TO_MODEL"):
        build_provider(DowntimeSettings(provider="anthropic", anthropic_api_key=""))


def test_build_provider_requires_a_key_for_anthropic(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("EPICVIBE_ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="no API key"):
        build_provider(DowntimeSettings(provider="anthropic", anthropic_api_key="",
                                        allow_phi_to_model=True))


def test_build_provider_constructs_the_sdk_provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    settings = DowntimeSettings(provider="anthropic", anthropic_api_key="",
                                model="claude-opus-5", allow_phi_to_model=True)
    provider = build_provider(settings)
    assert isinstance(provider, AnthropicSdkProvider)
    assert provider.model == "claude-opus-5"


def test_sdk_client_is_constructed_with_a_timeout_and_retries(monkeypatch):
    seen: dict = {}

    class FakeAsyncAnthropic:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(anthropic, "AsyncAnthropic", FakeAsyncAnthropic)
    AnthropicSdkProvider(api_key="sk-ant-test")
    assert seen == {"api_key": "sk-ant-test", "timeout": 30.0, "max_retries": 2}


def test_api_key_resolution_order(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-anthropic-env")
    monkeypatch.setenv("EPICVIBE_ANTHROPIC_API_KEY", "from-epicvibe-env")
    assert DowntimeSettings(anthropic_api_key="explicit").resolved_api_key() == "explicit"
    assert DowntimeSettings(anthropic_api_key="").resolved_api_key() == "from-anthropic-env"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert DowntimeSettings(anthropic_api_key="").resolved_api_key() == "from-epicvibe-env"
