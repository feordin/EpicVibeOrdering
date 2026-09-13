from typing import Protocol


class InferenceProvider(Protocol):
    async def complete_json(self, *, system: str, user: str, json_schema: dict) -> dict: ...

    def describe(self) -> str:
        """Short provenance string recorded in the audit log (e.g. "demo")."""
        ...


def describe_provider(provider: object) -> str:
    """Provenance string for whatever provider actually produced a proposal.

    Falls back to the class name so an audit row is never silently attributed
    to the wrong model when a caller injects a custom provider.
    """
    describe = getattr(provider, "describe", None)
    if callable(describe):
        try:
            return str(describe())
        except Exception:
            pass
    return type(provider).__name__


class FakeProvider:
    def __init__(self, response: dict):
        self.response = response
        self.calls: list[dict] = []

    def describe(self) -> str:
        return "fake"

    async def complete_json(self, *, system: str, user: str, json_schema: dict) -> dict:
        self.calls.append({"system": system, "user": user})
        return self.response
