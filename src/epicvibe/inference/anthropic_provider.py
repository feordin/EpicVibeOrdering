import httpx


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

    async def complete_json(self, *, system: str, user: str, json_schema: dict) -> dict:
        resp = await self._client.post("/v1/messages", json={
            "model": self.model, "max_tokens": 2048, "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [{"name": "emit_proposal",
                       "description": "Emit the order proposal.",
                       "input_schema": json_schema}],
            "tool_choice": {"type": "tool", "name": "emit_proposal"},
        })
        resp.raise_for_status()
        for block in resp.json().get("content", []):
            if block.get("type") == "tool_use":
                return block["input"]
        raise ValueError("no tool_use block in model response")
