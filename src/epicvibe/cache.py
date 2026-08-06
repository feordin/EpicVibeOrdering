import time
from typing import Any, Callable


class ProposalCache:
    def __init__(self, ttl_seconds: int, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_seconds
        self._clock = clock
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        stored_at, value = hit
        if self._clock() - stored_at > self._ttl:
            del self._data[key]
            return None
        return value

    def put(self, key: str, value: Any) -> None:
        self._data[key] = (self._clock(), value)
