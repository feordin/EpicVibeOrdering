import logging

from fhir.resources.R4B.bundle import Bundle
from pydantic import BaseModel

log = logging.getLogger("epicvibe.proposal")


class CodedItem(BaseModel):
    code: str
    system: str = ""
    display: str = ""


class PatientSummary(BaseModel):
    patient_id: str
    encounter_id: str | None = None
    conditions: list[CodedItem] = []
    medications: list[CodedItem] = []
    notes: list[str] = []


def _codings(resource, attr: str) -> list[CodedItem]:
    concept = getattr(resource, attr, None)
    if concept is None or not concept.coding:
        return []
    return [CodedItem(code=c.code or "", system=c.system or "", display=c.display or "")
            for c in concept.coding if c.code]


def _parse_bundle(prefetch: dict, key: str, notes: list[str]) -> list:
    raw = prefetch.get(key)
    if raw is None:
        notes.append(f"prefetch key '{key}' missing")
        return []
    try:
        bundle = Bundle.model_validate(raw)
    except Exception:
        notes.append(f"prefetch key '{key}' malformed")
        return []
    return [e.resource for e in (bundle.entry or []) if e.resource is not None]


def summarize_prefetch(context: dict, prefetch: dict) -> PatientSummary:
    notes: list[str] = []
    conditions, medications = [], []
    for res in _parse_bundle(prefetch, "conditions", notes):
        conditions.extend(_codings(res, "code"))
    for res in _parse_bundle(prefetch, "medications", notes):
        medications.extend(_codings(res, "medicationCodeableConcept"))
    return PatientSummary(patient_id=context["patientId"],
                          encounter_id=context.get("encounterId"),
                          conditions=conditions, medications=medications, notes=notes)
