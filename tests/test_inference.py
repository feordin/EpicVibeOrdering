import httpx
import pytest
from epicvibe.inference.anthropic_provider import AnthropicProvider
from epicvibe.inference.base import FakeProvider


async def test_fake():
    p = FakeProvider({"order_sets": [], "confidence": "low"})
    out = await p.complete_json(system="s", user="u", json_schema={})
    assert out == {"order_sets": [], "confidence": "low"}


async def test_anthropic_parses_tool_use():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "k"
        return httpx.Response(200, json={"content": [
            {"type": "tool_use", "name": "emit_proposal",
             "input": {"order_sets": [], "confidence": "high"}}]})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                               base_url="https://api.anthropic.com")
    p = AnthropicProvider(api_key="k", model="m", client=client)
    out = await p.complete_json(system="s", user="u", json_schema={"type": "object"})
    assert out["confidence"] == "high"


async def test_anthropic_raises_without_tool_use():
    def handler(request):
        return httpx.Response(200, json={"content": [{"type": "text", "text": "hi"}]})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                               base_url="https://api.anthropic.com")
    p = AnthropicProvider(api_key="k", model="m", client=client)
    with pytest.raises(ValueError):
        await p.complete_json(system="s", user="u", json_schema={})
