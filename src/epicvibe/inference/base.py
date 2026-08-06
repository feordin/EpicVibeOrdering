from typing import Protocol


class InferenceProvider(Protocol):
    async def complete_json(self, *, system: str, user: str, json_schema: dict) -> dict: ...


class FakeProvider:
    def __init__(self, response: dict):
        self.response = response
        self.calls: list[dict] = []

    async def complete_json(self, *, system: str, user: str, json_schema: dict) -> dict:
        self.calls.append({"system": system, "user": user})
        return self.response
