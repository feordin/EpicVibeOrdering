from pathlib import Path

import httpx
import pytest
from epicvibe.catalog.index import CatalogIndex
from epicvibe.catalog.loader import load_catalog
from epicvibe.inference.anthropic_provider import AnthropicProvider
from epicvibe.inference.base import FakeProvider
from epicvibe.inference.demo import DEMO_PROPOSAL, DemoProvider
from epicvibe.proposal.validation import validate_proposal


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


async def test_demo_provider_returns_deterministic_proposal():
    p = DemoProvider()
    out = await p.complete_json(system="s", user="u", json_schema={})
    assert out == DEMO_PROPOSAL


async def test_demo_provider_ignores_input():
    p = DemoProvider()
    out1 = await p.complete_json(system="a", user="b", json_schema={"x": 1})
    out2 = await p.complete_json(system="different", user="also different", json_schema={})
    assert out1 == out2 == DEMO_PROPOSAL


async def test_demo_proposal_validates_against_catalog():
    index = CatalogIndex(load_catalog(Path("fixtures/catalog/sample_catalog.json")))
    vp = validate_proposal(DEMO_PROPOSAL, index)
    assert vp.violations == []
    assert not vp.is_empty
