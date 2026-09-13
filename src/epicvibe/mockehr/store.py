"""A tiny in-memory FHIR R4 server backing the mock EHR.

Raw dicts on purpose: this is a demo harness, not a conformant server, so nothing here
validates against ``fhir.resources`` on the hot path. Search is deliberately tolerant --
unknown parameters are ignored rather than rejected, which is what keeps CDS Hooks
prefetch templates from blowing up.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("epicvibe.mockehr.store")

#: Resource types whose patient link lives on ``patient`` rather than ``subject``.
PATIENT_FIELD = {
    "AllergyIntolerance": "patient",
    "Immunization": "patient",
    "Coverage": "patient",
}

#: Search params we understand. Anything else is ignored (tolerant by design).
KNOWN_PARAMS = {"patient", "subject", "_id", "code", "category", "clinical-status",
                "status", "intent", "encounter", "identifier"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _bare_id(value: str) -> str:
    """``Patient/pat-santos`` -> ``pat-santos``; also tolerates full URLs."""
    value = (value or "").strip()
    if value.startswith("http"):
        value = value.rsplit("/", 1)[-1]
    return value.split("/")[-1]


def _concept_codes(concept: Any) -> set[str]:
    codes: set[str] = set()
    if not isinstance(concept, dict):
        return codes
    for coding in concept.get("coding") or []:
        code, system = coding.get("code"), coding.get("system")
        if code:
            codes.add(code)
            if system:
                codes.add(f"{system}|{code}")
    return codes


def _resource_codes(resource: dict) -> set[str]:
    codes: set[str] = set()
    for field in ("code", "medicationCodeableConcept", "type", "vaccineCode"):
        value = resource.get(field)
        if isinstance(value, list):
            for item in value:
                codes |= _concept_codes(item)
        else:
            codes |= _concept_codes(value)
    return codes


def _category_codes(resource: dict) -> set[str]:
    categories = resource.get("category")
    if isinstance(categories, dict):
        categories = [categories]
    codes: set[str] = set()
    for cat in categories or []:
        if isinstance(cat, str):
            codes.add(cat)
        else:
            codes |= _concept_codes(cat)
    return codes


def _token_values(param: str) -> list[str]:
    """``active,inactive`` -> ``[active, inactive]``; strips ``system|`` prefixes later."""
    return [v for v in (p.strip() for p in param.split(",")) if v]


def _token_match(candidates: set[str], wanted: str) -> bool:
    if wanted in candidates:
        return True
    # `system|code` search against a bare stored code, and vice versa.
    if "|" in wanted:
        return wanted.split("|", 1)[1] in candidates
    return False


class FhirStore:
    """Resource store keyed by ``(resourceType, id)``."""

    def __init__(self, fixtures_dir: Path | str):
        self.fixtures_dir = Path(fixtures_dir)
        self._by_type: dict[str, dict[str, dict]] = {}
        self._counter = 0
        self.load()

    # ------------------------------------------------------------------ loading
    def load(self) -> None:
        """(Re)load every fixture bundle, discarding anything created at runtime."""
        self._by_type = {}
        self._counter = 0
        if not self.fixtures_dir.is_dir():
            log.warning("mock EHR fixtures dir not found: %s", self.fixtures_dir)
            return
        for path in sorted(self.fixtures_dir.glob("*.json")):
            try:
                bundle = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                log.exception("failed to parse fixture %s", path)
                continue
            for entry in bundle.get("entry") or []:
                resource = entry.get("resource")
                if isinstance(resource, dict) and resource.get("resourceType"):
                    self.put(resource)
        log.info("mock EHR loaded %d resources from %s", self.count(), self.fixtures_dir)

    def count(self) -> int:
        return sum(len(v) for v in self._by_type.values())

    def summary(self) -> dict[str, int]:
        return {rtype: len(items) for rtype, items in sorted(self._by_type.items())}

    # ------------------------------------------------------------------- access
    def put(self, resource: dict) -> dict:
        rtype = resource["resourceType"]
        rid = resource.get("id") or self._next_id(rtype)
        resource["id"] = rid
        meta = resource.setdefault("meta", {})
        meta.setdefault("versionId", "1")
        meta["lastUpdated"] = _now()
        self._by_type.setdefault(rtype, {})[rid] = resource
        return resource

    def create(self, rtype: str, resource: dict) -> dict:
        resource = dict(resource)
        resource["resourceType"] = rtype
        resource["id"] = self._next_id(rtype)
        # keep any client-supplied meta (e.g. `source`) but re-stamp the version
        meta = dict(resource.get("meta") or {})
        meta.pop("versionId", None)
        meta.pop("lastUpdated", None)
        resource["meta"] = meta
        return self.put(resource)

    def update(self, rtype: str, rid: str, resource: dict) -> dict:
        resource = dict(resource)
        resource["resourceType"] = rtype
        resource["id"] = rid
        existing = self.get(rtype, rid)
        if existing:
            version = int((existing.get("meta") or {}).get("versionId", "1")) + 1
            resource.setdefault("meta", {})["versionId"] = str(version)
        return self.put(resource)

    def get(self, rtype: str, rid: str) -> dict | None:
        return self._by_type.get(rtype, {}).get(rid)

    def all_of(self, rtype: str) -> list[dict]:
        return list(self._by_type.get(rtype, {}).values())

    def _next_id(self, rtype: str) -> str:
        self._counter += 1
        return f"{rtype.lower()}-gen-{self._counter}"

    # ------------------------------------------------------------------- search
    def search(self, rtype: str, params: dict[str, Any]) -> list[dict]:
        """Filter resources of ``rtype``. Unknown params are ignored."""
        results = self.all_of(rtype)
        patient_field = PATIENT_FIELD.get(rtype, "subject")
        for raw_key, raw_value in params.items():
            key = raw_key.split(":", 1)[0]  # drop modifiers e.g. code:text
            if key not in KNOWN_PARAMS:
                continue
            values = raw_value if isinstance(raw_value, list) else [raw_value]
            value = str(values[0])
            if key in ("patient", "subject"):
                wanted = _bare_id(value)
                results = [r for r in results
                           if _bare_id((r.get(patient_field) or {}).get("reference", "")) == wanted
                           or _bare_id((r.get("subject") or {}).get("reference", "")) == wanted]
            elif key == "_id":
                wanted = set(_token_values(value))
                results = [r for r in results if r.get("id") in wanted]
            elif key == "encounter":
                wanted = _bare_id(value)
                results = [r for r in results
                           if _bare_id((r.get("encounter") or {}).get("reference", "")) == wanted]
            elif key == "code":
                wanted_tokens = _token_values(value)
                results = [r for r in results
                           if any(_token_match(_resource_codes(r), w) for w in wanted_tokens)]
            elif key == "category":
                wanted_tokens = _token_values(value)
                results = [r for r in results
                           if any(_token_match(_category_codes(r), w) for w in wanted_tokens)]
            elif key == "clinical-status":
                wanted_tokens = _token_values(value)
                results = [r for r in results
                           if any(_token_match(_concept_codes(r.get("clinicalStatus")), w)
                                  for w in wanted_tokens)]
            elif key in ("status", "intent"):
                wanted = set(_token_values(value))
                results = [r for r in results if r.get(key) in wanted]
            elif key == "identifier":
                wanted_tokens = _token_values(value)
                results = [r for r in results
                           if any(w.split("|")[-1] == (i.get("value") or "")
                                  for i in (r.get("identifier") or []) for w in wanted_tokens)]
        return results

    def search_bundle(self, rtype: str, params: dict[str, Any], *,
                      base_url: str = "") -> dict:
        resources = self.search(rtype, params)
        return self.as_bundle(resources, base_url=base_url)

    @staticmethod
    def as_bundle(resources: Iterable[dict], *, base_url: str = "") -> dict:
        entries = []
        for res in resources:
            entry: dict = {"resource": res}
            if base_url:
                entry["fullUrl"] = f"{base_url.rstrip('/')}/{res['resourceType']}/{res['id']}"
            entry["search"] = {"mode": "match"}
            entries.append(entry)
        return {"resourceType": "Bundle", "type": "searchset",
                "total": len(entries), "entry": entries}

    # --------------------------------------------------------- relative queries
    def search_relative(self, query: str, *, base_url: str = "") -> dict:
        """Run a FHIR relative search such as ``Condition?patient=pat-santos``."""
        query = (query or "").lstrip("/")
        parts = urlsplit(query)
        raw_segments = parts.path.split("/")
        segments = [s for s in raw_segments if s]
        if not segments:
            return self.as_bundle([], base_url=base_url)
        rtype = segments[0]
        if "?" not in query and len(raw_segments) >= 2:
            # ``Encounter/`` is an instance read with an empty id -- it is NOT a
            # type-level search, and must never fan out to every resource.
            resource = self.get(rtype, raw_segments[1].strip())
            return self.as_bundle([resource] if resource else [], base_url=base_url)
        params = {k: v[0] for k, v in parse_qs(parts.query).items()}
        return self.search_bundle(rtype, params, base_url=base_url)


_TEMPLATE = re.compile(r"\{\{\s*context\.(\w+)\s*\}\}")


class MissingContextValue(KeyError):
    """A ``{{context.*}}`` placeholder had no value in the hook context.

    Per the CDS Hooks spec the CDS client must omit a prefetch key whose template
    cannot be fully resolved -- executing the half-substituted query instead would
    leak every resource of that type (cross-patient), which is exactly the bug this
    exception exists to prevent.
    """


def resolve_template(template: str, context: dict) -> str:
    """Substitute ``{{context.patientId}}``-style tokens from a hook context.

    Raises :class:`MissingContextValue` when any placeholder has no value.
    """
    def repl(match: re.Match) -> str:
        key = match.group(1)
        value = context.get(key)
        if value is None or str(value).strip() == "":
            raise MissingContextValue(key)
        return str(value)
    return _TEMPLATE.sub(repl, template)
