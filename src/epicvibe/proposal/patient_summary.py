"""Patient summary built from CDS Hooks prefetch (and, optionally, live FHIR reads).

The summary is the *only* patient data handed to the LLM, so it deliberately
carries no direct identifiers: no name, no MRN, no date of birth -- age and sex
only.  `patient_id`/`encounter_id` live on the model for plumbing but are
excluded from the prompt by `ProposalEngine`.
"""

import logging
from datetime import date, datetime

from fhir.resources.R4B.bundle import Bundle
from pydantic import BaseModel

log = logging.getLogger("epicvibe.proposal")

VITAL_LOINC = {
    "8867-4", "8480-6", "8462-4", "8310-5", "9279-1", "2708-6", "59408-5",
    "29463-7", "8302-2", "39156-5", "85354-9",
}


class CodedItem(BaseModel):
    code: str
    system: str = ""
    display: str = ""


class Demographics(BaseModel):
    age: int | None = None
    sex: str = ""


class ObservationItem(BaseModel):
    code: str
    system: str = ""
    display: str = ""
    value: str = ""
    unit: str = ""
    date: str = ""
    category: str = ""          # "vital-signs" | "laboratory" | ""
    interpretation: str = ""


class AllergyItem(BaseModel):
    code: str = ""
    system: str = ""
    display: str = ""
    criticality: str = ""
    reactions: list[str] = []


class EncounterInfo(BaseModel):
    encounter_class: str = ""
    type: list[str] = []
    reasons: list[str] = []
    start: str = ""


class PatientSummary(BaseModel):
    patient_id: str
    encounter_id: str | None = None
    demographics: Demographics = Demographics()
    conditions: list[CodedItem] = []
    medications: list[CodedItem] = []
    observations: list[ObservationItem] = []
    allergies: list[AllergyItem] = []
    encounter: EncounterInfo | None = None
    active_orders: list[CodedItem] = []
    notes: list[str] = []

    # --- convenience views ---------------------------------------------------
    @property
    def problems(self) -> list[CodedItem]:
        """Alias for `conditions` (the active problem list)."""
        return self.conditions

    @property
    def vitals(self) -> list[ObservationItem]:
        return [o for o in self.observations if o.category == "vital-signs"]

    @property
    def labs(self) -> list[ObservationItem]:
        return [o for o in self.observations if o.category != "vital-signs"]

    def data_counts(self) -> dict[str, int]:
        return {
            "conditions": len(self.conditions),
            "medications": len(self.medications),
            "observations": len(self.observations),
            "vitals": len(self.vitals),
            "labs": len(self.labs),
            "allergies": len(self.allergies),
            "active_orders": len(self.active_orders),
            "encounter": 1 if self.encounter else 0,
        }


# --------------------------------------------------------------------------- #
# raw-dict parsing helpers (we parse dicts, not fhir.resources models, so that
# partial / vendor-flavoured payloads degrade gracefully instead of raising)
# --------------------------------------------------------------------------- #

def _concept_codings(concept: dict | None) -> list[CodedItem]:
    if not isinstance(concept, dict):
        return []
    out = [CodedItem(code=c.get("code") or "", system=c.get("system") or "",
                     display=c.get("display") or "")
           for c in concept.get("coding") or [] if c.get("code")]
    if not out and concept.get("text"):
        out = [CodedItem(code="", display=concept["text"])]
    return out


def _concept_text(concept: dict | None) -> str:
    if not isinstance(concept, dict):
        return ""
    if concept.get("text"):
        return concept["text"]
    for c in concept.get("coding") or []:
        if c.get("display"):
            return c["display"]
        if c.get("code"):
            return c["code"]
    return ""


def _codings(resource, attr: str) -> list[CodedItem]:
    """Coding extraction from a fhir.resources model (legacy path)."""
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


def bundle_resources(raw) -> list[dict]:
    """Resources from a raw searchset Bundle dict (or a single resource dict)."""
    if not isinstance(raw, dict):
        return []
    if raw.get("resourceType") == "Bundle":
        return [e["resource"] for e in raw.get("entry") or []
                if isinstance(e, dict) and isinstance(e.get("resource"), dict)]
    if raw.get("resourceType"):
        return [raw]
    return []


def _age_from_birth_date(birth_date: str) -> int | None:
    try:
        born = date.fromisoformat(birth_date[:10])
    except Exception:
        return None
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def parse_patient(raw: dict) -> Demographics:
    """Age + sex only.  Name / MRN / DOB are deliberately dropped here."""
    if not isinstance(raw, dict):
        return Demographics()
    age = None
    if raw.get("birthDate"):
        age = _age_from_birth_date(raw["birthDate"])
    for ext in raw.get("extension") or []:
        if str(ext.get("url", "")).endswith("patient-age") and ext.get("valueInteger"):
            age = ext["valueInteger"]
    return Demographics(age=age, sex=raw.get("gender") or "")


def _observation_value(raw: dict) -> tuple[str, str]:
    if isinstance(raw.get("valueQuantity"), dict):
        q = raw["valueQuantity"]
        val = q.get("value")
        return ("" if val is None else str(val)), (q.get("unit") or q.get("code") or "")
    if isinstance(raw.get("valueCodeableConcept"), dict):
        return _concept_text(raw["valueCodeableConcept"]), ""
    if raw.get("valueString"):
        return str(raw["valueString"]), ""
    if raw.get("valueBoolean") is not None:
        return str(raw["valueBoolean"]), ""
    comps = []
    for comp in raw.get("component") or []:
        name = _concept_text(comp.get("code"))
        q = comp.get("valueQuantity") or {}
        if q.get("value") is not None:
            comps.append(f"{name} {q['value']}{q.get('unit', '')}")
    return ("; ".join(comps), "")


def parse_observation(raw: dict) -> ObservationItem | None:
    codings = _concept_codings(raw.get("code"))
    if not codings:
        return None
    primary = codings[0]
    value, unit = _observation_value(raw)
    category = ""
    for cat in raw.get("category") or []:
        for c in cat.get("coding") or []:
            if c.get("code") in ("vital-signs", "laboratory"):
                category = c["code"]
    if not category and primary.code in VITAL_LOINC:
        category = "vital-signs"
    when = (raw.get("effectiveDateTime") or raw.get("issued")
            or (raw.get("effectivePeriod") or {}).get("start") or "")
    return ObservationItem(
        code=primary.code, system=primary.system,
        display=primary.display or _concept_text(raw.get("code")),
        value=value, unit=unit, date=str(when)[:10], category=category,
        interpretation=_concept_text((raw.get("interpretation") or [{}])[0])
        if raw.get("interpretation") else "")


def parse_allergy(raw: dict) -> AllergyItem | None:
    codings = _concept_codings(raw.get("code"))
    primary = codings[0] if codings else CodedItem(code="")
    reactions: list[str] = []
    for reaction in raw.get("reaction") or []:
        for manifestation in reaction.get("manifestation") or []:
            text = _concept_text(manifestation)
            if text:
                reactions.append(text)
    if not primary.code and not primary.display and not reactions:
        return None
    return AllergyItem(code=primary.code, system=primary.system, display=primary.display,
                       criticality=raw.get("criticality") or "", reactions=reactions)


def parse_encounter(raw: dict) -> EncounterInfo | None:
    if not isinstance(raw, dict):
        return None
    klass = raw.get("class") or {}
    reasons = [_concept_text(rc) for rc in raw.get("reasonCode") or []]
    types = [_concept_text(t) for t in raw.get("type") or []]
    info = EncounterInfo(
        encounter_class=(klass.get("display") or klass.get("code") or ""),
        type=[t for t in types if t], reasons=[r for r in reasons if r],
        start=str((raw.get("period") or {}).get("start") or "")[:10])
    if not (info.encounter_class or info.type or info.reasons or info.start):
        return None
    return info


def parse_service_request(raw: dict) -> CodedItem | None:
    codings = _concept_codings(raw.get("code"))
    return codings[0] if codings else None


def _medication_concept(raw: dict) -> dict:
    concept = raw.get("medicationCodeableConcept")
    if isinstance(concept, dict):
        return concept
    ref = (raw.get("medicationReference") or {}).get("reference", "")
    if isinstance(ref, str) and ref.startswith("#"):
        for contained in raw.get("contained") or []:
            if contained.get("id") == ref[1:]:
                return contained.get("code") or {}
    return {}


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #

def summarize_prefetch(context: dict, prefetch: dict) -> PatientSummary:
    """Build a summary from prefetch.

    Backward compatible: `conditions` and `medications` keys behave exactly as
    before.  The optional keys `patient`, `observations`, `allergies`,
    `encounter` and `serviceRequests` are consumed when present and quietly
    skipped (with a note) when absent.
    """
    notes: list[str] = []
    conditions, medications = [], []
    for res in _parse_bundle(prefetch, "conditions", notes):
        conditions.extend(_codings(res, "code"))
    for res in _parse_bundle(prefetch, "medications", notes):
        got = _codings(res, "medicationCodeableConcept")
        if not got:
            got = _concept_codings(_medication_concept(res.model_dump()))
        medications.extend(got)

    demographics = Demographics()
    if prefetch.get("patient") is not None:
        resources = bundle_resources(prefetch["patient"])
        if resources:
            demographics = parse_patient(resources[0])

    observations = []
    for res in bundle_resources(prefetch.get("observations")):
        parsed = parse_observation(res)
        if parsed:
            observations.append(parsed)

    allergies = []
    for res in bundle_resources(prefetch.get("allergies")):
        parsed = parse_allergy(res)
        if parsed:
            allergies.append(parsed)

    encounter = None
    enc_resources = bundle_resources(prefetch.get("encounter"))
    if enc_resources:
        encounter = parse_encounter(enc_resources[0])

    active_orders = []
    for res in bundle_resources(prefetch.get("serviceRequests")):
        parsed = parse_service_request(res)
        if parsed:
            active_orders.append(parsed)

    return PatientSummary(patient_id=context["patientId"],
                          encounter_id=context.get("encounterId"),
                          demographics=demographics,
                          conditions=conditions, medications=medications,
                          observations=observations, allergies=allergies,
                          encounter=encounter, active_orders=active_orders,
                          notes=notes)


def summarize_resources(patient_id: str, encounter_id: str | None,
                        resources: dict[str, list[dict]]) -> PatientSummary:
    """Build a summary from already-fetched raw FHIR resources (SMART app path).

    `resources` maps resource type -> list of raw resource dicts.
    """
    conditions: list[CodedItem] = []
    for res in resources.get("Condition", []):
        conditions.extend(_concept_codings(res.get("code")))
    medications: list[CodedItem] = []
    for res in resources.get("MedicationRequest", []):
        medications.extend(_concept_codings(_medication_concept(res)))
    observations = [o for o in (parse_observation(r) for r in resources.get("Observation", []))
                    if o]
    allergies = [a for a in (parse_allergy(r) for r in resources.get("AllergyIntolerance", []))
                 if a]
    orders = [s for s in (parse_service_request(r) for r in resources.get("ServiceRequest", []))
              if s]
    patients = resources.get("Patient", [])
    demographics = parse_patient(patients[0]) if patients else Demographics()
    encounters = resources.get("Encounter", [])
    encounter = parse_encounter(encounters[0]) if encounters else None
    return PatientSummary(patient_id=patient_id, encounter_id=encounter_id,
                          demographics=demographics, conditions=conditions,
                          medications=medications, observations=observations,
                          allergies=allergies, encounter=encounter,
                          active_orders=orders)


def utcnow_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
