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


class RefinementCache(ProposalCache):
    """Clinician-refined proposals handed back from the SMART app.

    Keyed by encounter id, with the patient id as a fallback key so a SMART
    launch that carries no encounter context still reaches the next hook for
    that patient.  Same TTL semantics as `ProposalCache`; separate instance so
    a refinement never expires with (or is overwritten by) the AI proposal.
    """

    @staticmethod
    def keys(encounter_id: str | None, patient_id: str | None) -> list[str]:
        out = []
        if encounter_id:
            out.append(encounter_id)
        if patient_id:
            out.append(f"pat:{patient_id}")
        return out

    def put_for(self, encounter_id: str | None, patient_id: str | None, value: Any) -> list[str]:
        keys = self.keys(encounter_id, patient_id)
        for key in keys:
            self.put(key, value)
        return keys

    def get_for(self, encounter_id: str | None, patient_id: str | None) -> Any | None:
        for key in self.keys(encounter_id, patient_id):
            hit = self.get(key)
            if hit is not None:
                return hit
        return None


def refinement_cache(state: Any) -> RefinementCache:
    """Lazily attach the hand-back store to an app state object."""
    cache = getattr(state, "refinements", None)
    if cache is None:
        ttl = getattr(getattr(state, "settings", None), "smart_handback_ttl_seconds", 3600)
        cache = RefinementCache(ttl)
        state.refinements = cache
    return cache
