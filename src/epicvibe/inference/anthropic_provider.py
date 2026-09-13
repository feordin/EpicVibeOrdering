import httpx

MAX_TOKENS = 8192

# Follow-up: migrate to the official `anthropic` Python SDK (retries, streaming,
# typed errors) instead of this hand-rolled httpx call.


class AnthropicProvider:
    def __init__(self, api_key: str, model: str,
                 base_url: str = "https://api.anthropic.com",
                 client: httpx.AsyncClient | None = None):
        self.model = model
        self._client = client or httpx.AsyncClient(
            base_url=base_url, timeout=30.0,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"})
        if client is not None:
            self._client.headers.setdefault("x-api-key", api_key)
            self._client.headers.setdefault("anthropic-version", "2023-06-01")

    def describe(self) -> str:
        return f"anthropic:{self.model}"

    async def complete_json(self, *, system: str, user: str, json_schema: dict) -> dict:
        resp = await self._client.post("/v1/messages", json={
            "model": self.model, "max_tokens": MAX_TOKENS, "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [{"name": "emit_proposal",
                       "description": "Emit the order proposal.",
                       "input_schema": json_schema}],
            "tool_choice": {"type": "tool", "name": "emit_proposal"},
        })
        resp.raise_for_status()
        body = resp.json()
        if body.get("stop_reason") == "refusal":
            category = ((body.get("refusal") or {}).get("category")
                        or body.get("refusal_category") or "unspecified")
            raise ValueError(f"model refused to answer (category: {category})")
        for block in body.get("content", []):
            if block.get("type") == "tool_use":
                return block["input"]
        raise ValueError("no tool_use block in model response")
