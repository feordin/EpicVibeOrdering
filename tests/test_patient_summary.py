import json
from pathlib import Path

import httpx
from fastapi import FastAPI

from epicvibe.proposal.fhir_pull import fetch_missing, search_paths
from epicvibe.proposal.patient_summary import summarize_prefetch, summarize_resources

def _body():
    return json.loads(Path("fixtures/hooks/patient_view.json").read_text())

def test_summarize():
    body = _body()
    s = summarize_prefetch(body["context"], body["prefetch"])
    assert s.patient_id == "PAT1" and s.encounter_id == "ENC1"
    assert s.conditions[0].code == "E11.9"
    assert s.medications[0].display.startswith("lisinopril")

def test_missing_prefetch_is_soft():
    body = _body()
    s = summarize_prefetch(body["context"], {})
    assert s.conditions == [] and s.medications == []
    assert any("conditions" in n for n in s.notes)

def test_malformed_bundle_is_soft():
    body = _body()
    s = summarize_prefetch(body["context"], {"conditions": {"resourceType": "Bundle", "entry": "junk"}})
    assert s.conditions == []


# --------------------------------------------------------------------------- #
# richer prefetch
# --------------------------------------------------------------------------- #

def test_new_prefetch_keys_are_parsed():
    body = _body()
    s = summarize_prefetch(body["context"], body["prefetch"])

    assert s.demographics.sex == "female" and s.demographics.age and s.demographics.age > 40
    assert {o.display for o in s.labs} == {"Hemoglobin A1c"}
    assert s.labs[0].value == "8.4" and s.labs[0].unit == "%" and s.labs[0].date == "2026-08-30"
    assert len(s.vitals) == 1 and "148" in s.vitals[0].value        # BP components flattened
    assert s.allergies[0].display == "Allergy to penicillin"
    assert s.allergies[0].criticality == "high" and s.allergies[0].reactions == ["hives"]
    assert s.encounter.encounter_class == "ambulatory"
    assert s.encounter.reasons == ["New diabetes diagnosis follow-up"]
    assert s.problems == s.conditions                               # alias
    counts = s.data_counts()
    assert counts["observations"] == 2 and counts["allergies"] == 1 and counts["encounter"] == 1


def test_summary_carries_no_direct_identifiers():
    body = _body()
    s = summarize_prefetch(body["context"], body["prefetch"])
    blob = json.dumps(s.model_dump(exclude={"patient_id", "encounter_id"}))
    for identifier in ("Santos", "Maria", "MRN-1001", "1974-03-11"):
        assert identifier not in blob


def test_new_keys_are_optional():
    body = _body()
    prefetch = {k: v for k, v in body["prefetch"].items() if k in ("conditions", "medications")}
    s = summarize_prefetch(body["context"], prefetch)
    assert s.conditions and s.observations == [] and s.allergies == [] and s.encounter is None


def test_summarize_resources_from_direct_reads():
    s = summarize_resources("PAT1", "ENC1", {
        "Patient": [{"resourceType": "Patient", "gender": "male", "birthDate": "1950-01-01"}],
        "Condition": [{"resourceType": "Condition",
                       "code": {"coding": [{"code": "I50.22", "display": "HFrEF"}]}}],
        "ServiceRequest": [{"resourceType": "ServiceRequest",
                            "code": {"coding": [{"code": "PREF_BNP", "display": "BNP"}]}}],
    })
    assert s.demographics.sex == "male" and s.conditions[0].code == "I50.22"
    assert s.active_orders[0].display == "BNP"


# --------------------------------------------------------------------------- #
# fetch_missing
# --------------------------------------------------------------------------- #

def _fhir_stub() -> FastAPI:
    ehr = FastAPI()
    ehr.state.paths = []

    @ehr.get("/fhir/Patient/{pid}")
    async def patient(pid: str) -> dict:
        ehr.state.paths.append("Patient")
        return {"resourceType": "Patient", "gender": "female", "birthDate": "1974-03-11"}

    @ehr.get("/fhir/Encounter/{eid}")
    async def encounter(eid: str) -> dict:
        ehr.state.paths.append("Encounter")
        return {"resourceType": "Encounter", "class": {"display": "ambulatory"}}

    @ehr.get("/fhir/{resource_type}")
    async def search(resource_type: str) -> dict:
        ehr.state.paths.append(resource_type)
        if resource_type == "Observation":
            return {"resourceType": "Bundle", "entry": [{"resource": {
                "resourceType": "Observation",
                "category": [{"coding": [{"code": "laboratory"}]}],
                "code": {"coding": [{"system": "http://loinc.org", "code": "4548-4",
                                     "display": "Hemoglobin A1c"}]},
                "effectiveDateTime": "2026-08-30",
                "valueQuantity": {"value": 8.4, "unit": "%"}}}]}
        if resource_type == "AllergyIntolerance":
            return {"resourceType": "Bundle", "entry": [{"resource": {
                "resourceType": "AllergyIntolerance",
                "code": {"coding": [{"code": "91936005", "display": "Penicillin"}]}}}]}
        return {"resourceType": "Bundle", "entry": []}

    return ehr


async def test_fetch_missing_fills_gaps():
    body = _body()
    prefetch = {k: v for k, v in body["prefetch"].items() if k in ("conditions", "medications")}
    summary = summarize_prefetch(body["context"], prefetch)
    assert summary.observations == [] and summary.allergies == []

    ehr = _fhir_stub()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ehr),
                                 base_url="http://ehr.test") as client:
        filled = await fetch_missing(summary, "http://ehr.test/fhir",
                                     {"access_token": "t", "token_type": "Bearer"},
                                     "PAT1", "ENC1", client=client, timeout=5.0)

    assert filled.demographics.sex == "female"
    assert filled.labs[0].display == "Hemoglobin A1c" and filled.labs[0].value == "8.4"
    assert filled.allergies[0].display == "Penicillin"
    assert filled.encounter.encounter_class == "ambulatory"
    # prefetch already supplied these, so they must NOT be re-fetched
    assert "Condition" not in ehr.state.paths and "MedicationRequest" not in ehr.state.paths
    assert any("live FHIR pull filled" in n for n in filled.notes)


async def test_fetch_missing_is_fail_soft():
    body = _body()
    summary = summarize_prefetch(body["context"], {})
    before = summary.model_dump()

    dead = FastAPI()

    @dead.get("/{path:path}")
    async def boom(path: str):
        from fastapi import HTTPException
        raise HTTPException(status_code=500)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=dead, raise_app_exceptions=False),
                                 base_url="http://ehr.test") as client:
        out = await fetch_missing(summary, "http://ehr.test/fhir", None, "PAT1", "ENC1",
                                  client=client, timeout=2.0)
    assert out.conditions == [] and out.medications == []
    assert out.model_dump()["conditions"] == before["conditions"]


async def test_fetch_missing_without_server_is_a_noop():
    body = _body()
    summary = summarize_prefetch(body["context"], {})
    out = await fetch_missing(summary, None, None, "PAT1", "ENC1")
    assert out is summary


def test_search_paths_shape():
    paths = search_paths("PAT1", "ENC1")
    assert paths["observations"] == "Observation?patient=PAT1&_count=50"
    assert paths["encounter"] == "Encounter/ENC1"
    assert "encounter" not in search_paths("PAT1", None)
