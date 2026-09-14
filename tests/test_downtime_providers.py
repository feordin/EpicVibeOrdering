"""The three inference providers + the provider factory.

No network anywhere: the Anthropic client is a stub and Ollama is served by an
`httpx.MockTransport`.
"""

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
    OllamaProvider,
    OllamaProviderError,
    ProviderError,
    build_provider,
    relax_json_schema,
)
from epicvibe.downtime.schema import FilledTemplate, TemplateSelection, strict_json_schema

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




# ---------------------------------------------------------------------------
# Ollama provider
# ---------------------------------------------------------------------------


def ollama(responses, **kwargs):
    """An OllamaProvider wired to a MockTransport that replays `responses`.

    Each entry is either an `httpx.Response`, or a string that becomes a normal
    `/api/chat` envelope with that string as `message.content`, or an exception
    to raise. The list of captured request bodies is returned alongside.
    """
    seen: list[dict] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json={"model": "m", "done": True,
                                         "message": {"role": "assistant", "content": item}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OllamaProvider(client=client, **kwargs), seen


FILL_SCHEMA = strict_json_schema(FilledTemplate)


async def test_request_shape_is_ollama_structured_output():
    provider, seen = ollama([json.dumps(PAYLOAD)], model="qwen2.5:7b",
                            base_url="http://box:11434/", num_ctx=4096, keep_alive="30m")
    assert await provider.complete_json(system="sys", user="usr",
                                        json_schema=FILL_SCHEMA) == PAYLOAD

    body = seen[0]
    assert body["model"] == "qwen2.5:7b"
    assert body["stream"] is False
    assert body["keep_alive"] == "30m"
    assert body["options"] == {"temperature": 0, "num_ctx": 4096}
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
    ]
    # The schema goes in `format`, not in a tool - Ollama has no tool-forcing.
    assert body["format"]["properties"]["template_id"]["type"] == "string"


def test_base_url_trailing_slash_is_normalised():
    assert OllamaProvider(base_url="http://localhost:11434/").base_url == "http://localhost:11434"


def test_describe_names_the_local_model():
    assert OllamaProvider(model="llama3.1:8b").describe() == "ollama:llama3.1:8b"


# --- schema relaxation ------------------------------------------------------


def test_relax_drops_the_strict_only_keywords_ollama_cannot_lower():
    """Ollama compiles `format` into a llama.cpp grammar. `additionalProperties`
    has no grammar meaning, and a list-typed union is not a shape it lowers."""
    strict = strict_json_schema(FilledTemplate)
    assert strict["additionalProperties"] is False
    assert strict["$defs"]["FilledField"]["properties"]["value"]["type"] == ["string", "null"]

    relaxed = relax_json_schema(strict)
    assert "additionalProperties" not in relaxed
    value = relaxed["$defs"]["FilledField"]["properties"]["value"]
    assert "type" not in value
    assert {b["type"] for b in value["anyOf"]} == {"string", "null"}
    # ...and the strict input is left untouched, since the engine reuses it.
    assert strict["additionalProperties"] is False


def test_relax_keeps_required_so_a_small_model_cannot_answer_with_an_empty_object():
    relaxed = relax_json_schema(strict_json_schema(TemplateSelection))
    assert set(relaxed["required"]) == {"template_id", "confidence", "rationale", "alternatives"}
    assert relaxed["properties"]["confidence"]["enum"] == ["high", "medium", "low"]
    assert relaxed["properties"]["alternatives"]["items"] == {"$ref": "#/$defs/TemplateAlternative"}


def test_relax_keeps_a_real_additional_properties_subschema():
    """Only strict mode's `false` is meaningless to a grammar - a subschema there
    constrains the extra keys and has to survive."""
    relaxed = relax_json_schema({"type": "object", "additionalProperties": {"type": "string"}})
    assert relaxed["additionalProperties"] == {"type": "string"}


def test_relax_does_not_copy_node_level_keywords_into_the_union_branches():
    relaxed = relax_json_schema({"type": ["string", "null"], "title": "Value",
                                 "default": None, "maxLength": 20})
    assert relaxed["anyOf"] == [{"type": "string", "maxLength": 20}, {"type": "null"}]
    assert relaxed["title"] == "Value" and relaxed["default"] is None


# --- response parsing -------------------------------------------------------


async def test_a_fenced_response_is_unwrapped():
    provider, _ = ollama(["```json\n" + json.dumps(PAYLOAD) + "\n```"])
    assert await provider.complete_json(system="s", user="u", json_schema=FILL_SCHEMA) == PAYLOAD


async def test_prose_around_the_object_is_stripped():
    provider, _ = ollama(["Here is the result:\n" + json.dumps(PAYLOAD) + "\nHope that helps!"])
    assert await provider.complete_json(system="s", user="u", json_schema=FILL_SCHEMA) == PAYLOAD


async def test_unparseable_output_is_retried_once_with_a_json_only_nudge():
    provider, seen = ollama(["I cannot comply.", json.dumps(PAYLOAD)])
    assert await provider.complete_json(system="s", user="u",
                                        json_schema=FILL_SCHEMA) == PAYLOAD
    assert len(seen) == 2
    retry_user = seen[1]["messages"][1]["content"]
    assert retry_user.startswith("u")
    assert "Return ONLY the JSON object" in retry_user
    assert seen[1]["messages"][0]["content"] == "s"  # system prompt is unchanged


async def test_two_unparseable_responses_raise_with_the_raw_text():
    provider, seen = ollama(["not json", "still not json"])
    with pytest.raises(OllamaProviderError, match="did not return a JSON object"):
        await provider.complete_json(system="s", user="u", json_schema=FILL_SCHEMA)
    assert len(seen) == 2


async def test_a_json_array_is_not_accepted_as_a_result():
    provider, _ = ollama(["[1, 2, 3]", "[4]"])
    with pytest.raises(OllamaProviderError, match="did not return a JSON object"):
        await provider.complete_json(system="s", user="u", json_schema=FILL_SCHEMA)


async def test_an_empty_response_is_retried_then_raises():
    provider, seen = ollama(["", ""])
    with pytest.raises(OllamaProviderError, match="empty response content"):
        await provider.complete_json(system="s", user="u", json_schema=FILL_SCHEMA)
    assert len(seen) == 2


# --- thinking mode ----------------------------------------------------------


async def test_thinking_is_turned_off_by_default():
    provider, seen = ollama([json.dumps(PAYLOAD)])
    await provider.complete_json(system="s", user="u", json_schema=FILL_SCHEMA)
    assert seen[0]["think"] is False


async def test_think_none_omits_the_key_entirely():
    provider, seen = ollama([json.dumps(PAYLOAD)], think=None)
    await provider.complete_json(system="s", user="u", json_schema=FILL_SCHEMA)
    assert "think" not in seen[0]


async def test_a_model_with_no_thinking_mode_is_retried_without_the_key():
    """`think` is not universally supported; a model that rejects it never had
    the problem it solves, so drop it and carry on rather than failing the capture."""
    provider, seen = ollama([
        httpx.Response(400, json={"error": 'registry.ollama.ai/library/m does not support thinking'}),
        json.dumps(PAYLOAD),
    ])
    assert await provider.complete_json(system="s", user="u",
                                        json_schema=FILL_SCHEMA) == PAYLOAD
    assert seen[0]["think"] is False and "think" not in seen[1]


async def test_the_key_is_not_retried_on_every_later_call():
    provider, seen = ollama([
        httpx.Response(400, json={"error": "does not support thinking"}),
        json.dumps(PAYLOAD),
        json.dumps(PAYLOAD),
    ])
    await provider.complete_json(system="s", user="u", json_schema=FILL_SCHEMA)
    await provider.complete_json(system="s", user="u", json_schema=FILL_SCHEMA)
    assert len(seen) == 3 and "think" not in seen[2]


async def test_a_reply_that_is_all_reasoning_and_no_answer_says_so():
    """Measured with a 26B reasoning model: it burned 25,946 tokens in
    `message.thinking`, hit the context limit, and returned an empty `content`.
    A bare "empty response" would send someone hunting the wrong bug."""
    envelope = httpx.Response(200, json={
        "done_reason": "length", "eval_count": 25946,
        "message": {"role": "assistant", "content": "",
                    "thinking": "Let me work through the transcript..."},
    })
    provider, _ = ollama([envelope], model="gemma4:26b")
    with pytest.raises(OllamaProviderError) as excinfo:
        await provider.complete_json(system="s", user="u", json_schema=FILL_SCHEMA)
    message = str(excinfo.value)
    assert "only reasoning tokens" in message
    assert "done_reason='length'" in message and "25946" in message
    assert "EPICVIBE_DOWNTIME_OLLAMA_THINK" in message


# --- error mapping ----------------------------------------------------------


async def test_a_dead_daemon_names_the_url_and_how_to_fix_it():
    provider, _ = ollama([httpx.ConnectError("connection refused")],
                         base_url="http://localhost:11434")
    with pytest.raises(OllamaProviderError) as excinfo:
        await provider.complete_json(system="s", user="u", json_schema=FILL_SCHEMA)
    message = str(excinfo.value)
    assert "http://localhost:11434/api/chat" in message
    assert "ollama serve" in message


async def test_a_timeout_says_which_knob_to_turn():
    """A 26B model on CPU really can exceed the default, and the clinician needs
    to be told that rather than shown a bare ReadTimeout."""
    provider, _ = ollama([httpx.ReadTimeout("timed out")], model="gemma4:26b", timeout=300)
    with pytest.raises(OllamaProviderError) as excinfo:
        await provider.complete_json(system="s", user="u", json_schema=FILL_SCHEMA)
    message = str(excinfo.value)
    assert "timed out after 300s" in message
    assert "EPICVIBE_DOWNTIME_OLLAMA_TIMEOUT_SECONDS" in message


async def test_a_model_that_is_not_pulled_surfaces_the_daemon_message():
    body = {"error": 'model "gemma4:26b" not found, try pulling it first'}
    provider, _ = ollama([httpx.Response(404, json=body)], model="gemma4:26b")
    with pytest.raises(OllamaProviderError, match="not found, try pulling it first"):
        await provider.complete_json(system="s", user="u", json_schema=FILL_SCHEMA)


async def test_a_200_with_an_error_field_still_raises():
    provider, _ = ollama([httpx.Response(200, json={"error": "out of memory"})])
    with pytest.raises(OllamaProviderError, match="out of memory"):
        await provider.complete_json(system="s", user="u", json_schema=FILL_SCHEMA)


def test_every_provider_failure_is_one_catchable_type():
    """The capture UI catches one thing and shows it to the clinician."""
    assert issubclass(OllamaProviderError, ProviderError)
    assert issubclass(AnthropicProviderError, ProviderError)


# --------------------------------------------------------------------------
# factory / settings
# --------------------------------------------------------------------------




def test_build_provider_constructs_the_ollama_provider_from_settings():
    settings = DowntimeSettings(
        provider="ollama", ollama_base_url="http://box:11434", ollama_model="qwen2.5:7b",
        ollama_timeout_seconds=120, ollama_num_ctx=4096, ollama_keep_alive="1h",
    )
    provider = build_provider(settings)
    assert isinstance(provider, OllamaProvider)
    assert (provider.base_url, provider.model) == ("http://box:11434", "qwen2.5:7b")
    assert (provider.timeout, provider.num_ctx, provider.keep_alive) == (120, 4096, "1h")
    assert provider.think is False


def test_ollama_is_not_gated_on_the_phi_flag():
    """The weights are on this box, so no transcript crosses the trust boundary -
    there is nothing for the hosted-model gate to authorise."""
    settings = DowntimeSettings(provider="ollama")
    assert settings.allow_phi_to_model is False
    assert isinstance(build_provider(settings), OllamaProvider)


def test_provider_describe_strings_identify_the_model_in_eval_output():
    assert KeywordFakeProvider().describe() == "fake:keyword"
    assert AnthropicSdkProvider(api_key="k", model="claude-opus-5",
                                client=StubClient()).describe() == "anthropic:claude-opus-5"



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
