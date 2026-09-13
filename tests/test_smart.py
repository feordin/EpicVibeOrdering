"""End-to-end SMART on FHIR test: launch -> callback -> app -> proposal -> submit.

The EHR is a tiny in-test FastAPI app (SMART discovery, auto-approving OAuth,
a handful of FHIR reads and a create endpoint) reached over an ASGI transport.
"""

import json
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from epicvibe.cds.app import create_app
from epicvibe.config import Settings

EHR_BASE = "http://ehr.test"
ISS = f"{EHR_BASE}/fhir"
PATIENT_ID = "pat-okafor"
ENCOUNTER_ID = "enc-okafor-1"


# --------------------------------------------------------------------------- #
# fake EHR
# --------------------------------------------------------------------------- #

def _bundle(*resources: dict) -> dict:
    return {"resourceType": "Bundle", "type": "searchset",
            "entry": [{"resource": r} for r in resources]}


PATIENT = {"resourceType": "Patient", "id": PATIENT_ID, "gender": "male",
           "birthDate": "1959-04-02",
           "name": [{"family": "Okafor", "given": ["Daniel"]}],
           "identifier": [{"system": "urn:oid:1.2.3", "value": "MRN-9931"}]}

CONDITIONS = _bundle(
    {"resourceType": "Condition", "id": "c1",
     "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "J18.9",
                          "display": "Community-acquired pneumonia"}]}},
    {"resourceType": "Condition", "id": "c2",
     "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "J44.9",
                          "display": "COPD"}]}},
    {"resourceType": "Condition", "id": "c3",
     "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "N18.3",
                          "display": "Chronic kidney disease stage 3"}]}})

MEDICATIONS = _bundle(
    {"resourceType": "MedicationRequest", "id": "m1", "status": "active", "intent": "order",
     "medicationCodeableConcept": {"coding": [
         {"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "745679",
          "display": "tiotropium inhalation"}]}})

OBSERVATIONS = _bundle(
    {"resourceType": "Observation", "id": "o1", "status": "final",
     "category": [{"coding": [{"code": "laboratory"}]}],
     "code": {"coding": [{"system": "http://loinc.org", "code": "48642-3",
                          "display": "eGFR"}]},
     "effectiveDateTime": "2026-09-12T08:00:00Z",
     "valueQuantity": {"value": 48, "unit": "mL/min/1.73m2"}},
    {"resourceType": "Observation", "id": "o2", "status": "final",
     "category": [{"coding": [{"code": "vital-signs"}]}],
     "code": {"coding": [{"system": "http://loinc.org", "code": "59408-5",
                          "display": "SpO2"}]},
     "effectiveDateTime": "2026-09-12T08:05:00Z",
     "valueQuantity": {"value": 91, "unit": "%"}})

ALLERGIES = _bundle(
    {"resourceType": "AllergyIntolerance", "id": "a1", "criticality": "low",
     "code": {"coding": [{"system": "http://snomed.info/sct", "code": "91936005",
                          "display": "Penicillin allergy"}]},
     "reaction": [{"manifestation": [{"text": "rash"}]}]})

ENCOUNTER = {"resourceType": "Encounter", "id": ENCOUNTER_ID, "status": "in-progress",
             "class": {"code": "EMER", "display": "emergency"},
             "reasonCode": [{"text": "shortness of breath"}],
             "period": {"start": "2026-09-12T07:30:00Z"}}


def make_ehr() -> FastAPI:
    ehr = FastAPI()
    ehr.state.created = []

    @ehr.get("/fhir/.well-known/smart-configuration")
    async def smart_config() -> dict:
        return {"authorization_endpoint": f"{EHR_BASE}/oauth/authorize",
                "token_endpoint": f"{EHR_BASE}/oauth/token",
                "capabilities": ["launch-ehr", "client-public"]}

    @ehr.get("/oauth/authorize")
    async def authorize(request: Request) -> RedirectResponse:
        params = dict(request.query_params)
        ehr.state.authorize_params = params
        target = params["redirect_uri"] + "?" + urlencode(
            {"code": "auth-code-123", "state": params["state"]})
        return RedirectResponse(target, status_code=302)

    @ehr.post("/oauth/token")
    async def token(request: Request) -> dict:
        # parsed by hand: python-multipart is not a dependency of this project
        form = dict(parse_qsl((await request.body()).decode()))
        ehr.state.token_form = form
        return {"access_token": "tok-abc", "token_type": "Bearer", "expires_in": 3600,
                "scope": "patient/*.read patient/ServiceRequest.write",
                "patient": PATIENT_ID, "encounter": ENCOUNTER_ID}

    @ehr.get("/fhir/Patient/{pid}")
    async def patient(pid: str) -> dict:
        return PATIENT

    @ehr.get("/fhir/Encounter/{eid}")
    async def encounter(eid: str) -> dict:
        return ENCOUNTER

    @ehr.get("/fhir/{resource_type}")
    async def search(resource_type: str) -> dict:
        return {"Condition": CONDITIONS, "MedicationRequest": MEDICATIONS,
                "Observation": OBSERVATIONS, "AllergyIntolerance": ALLERGIES}.get(
                    resource_type, _bundle())

    @ehr.post("/fhir/{resource_type}")
    async def create(resource_type: str, request: Request) -> JSONResponse:
        body = await request.json()
        new_id = f"{resource_type.lower()}-{len(ehr.state.created) + 1}"
        ehr.state.created.append(body)
        return JSONResponse({**body, "id": new_id}, status_code=201)

    return ehr


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def ehr():
    return make_ehr()


def _make_app(ehr, **overrides):
    application = create_app(Settings(
        _env_file=None, audit_db_path=":memory:", inference_provider="demo",
        smart_allowed_issuers=[ISS],
        smart_redirect_uri="http://app.test/smart/callback",
        smart_launch_url="http://app.test/smart/launch", **overrides))
    application.state.http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ehr), base_url=EHR_BASE)
    return application


@pytest.fixture
def app(ehr):
    """Default app: `smart_submit_mode` is "handback" (the Epic-viable path)."""
    return _make_app(ehr)


@pytest.fixture
def fhir_app(ehr):
    """`smart_submit_mode="fhir"` — direct FHIR create, as used against the mock
    EHR / HAPI.  Not viable inside Epic; see docs/spikes/fhir-order-writeback.md."""
    return _make_app(ehr, smart_submit_mode="fhir")


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://app.test")


async def _launch(app, ehr, client: httpx.AsyncClient) -> httpx.Response:
    """Drive launch -> authorize -> callback the way a browser would."""
    r = await client.get("/smart/launch", params={"iss": ISS, "launch": "launch-1"})
    assert r.status_code == 302
    authorize_url = r.headers["location"]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ehr),
                                 base_url=EHR_BASE) as browser:
        r2 = await browser.get(authorize_url)
    assert r2.status_code == 302
    query = parse_qs(urlparse(r2.headers["location"]).query)

    return await client.get("/smart/callback",
                            params={"code": query["code"][0], "state": query["state"][0]})


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #

async def test_launch_redirect_carries_pkce_and_scopes(app, ehr):
    async with _client(app) as client:
        r = await client.get("/smart/launch", params={"iss": ISS, "launch": "launch-1"})
    assert r.status_code == 302
    params = parse_qs(urlparse(r.headers["location"]).query)
    assert params["response_type"] == ["code"]
    assert params["client_id"] == ["epicvibe-order-assistant"]
    assert params["redirect_uri"] == ["http://app.test/smart/callback"]
    assert params["aud"] == [ISS]
    assert params["launch"] == ["launch-1"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"][0]
    scope = params["scope"][0]
    for needed in ("launch", "patient/*.read", "patient/ServiceRequest.write",
                   "patient/MedicationRequest.write"):
        assert needed in scope


async def test_callback_sets_session_cookie(app, ehr):
    async with _client(app) as client:
        r = await _launch(app, ehr, client)
        assert r.status_code == 302 and r.headers["location"] == "/smart/app"
        assert client.cookies.get("epicvibe_smart")
        assert ehr.state.token_form["grant_type"] == "authorization_code"
        assert ehr.state.token_form["code_verifier"]

        ctx = (await client.get("/smart/api/context")).json()
        assert ctx == {"iss": ISS, "patient": PATIENT_ID, "encounter": ENCOUNTER_ID,
                       "scope": "patient/*.read patient/ServiceRequest.write",
                       "authorized": True, "submit_mode": "handback"}


async def test_app_page_served(app, ehr):
    async with _client(app) as client:
        await _launch(app, ehr, client)
        page = await client.get("/smart/app")
    assert page.status_code == 200
    assert "EpicVibe Order Assistant" in page.text
    assert "/smart/api/proposal" in page.text


async def test_context_requires_session(app):
    async with _client(app) as client:
        assert (await client.get("/smart/api/context")).status_code == 401


async def test_proposal_uses_all_ehr_data(app, ehr):
    async with _client(app) as client:
        await _launch(app, ehr, client)
        data = (await client.post("/smart/api/proposal", json={})).json()

    assert data["banner"]["name"] == "Daniel Okafor"
    assert data["banner"]["mrn"] == "MRN-9931"
    counts = data["counts"]
    assert counts["conditions"] == 3 and counts["medications"] == 1
    assert counts["observations"] == 2 and counts["labs"] == 1 and counts["vitals"] == 1
    assert counts["allergies"] == 1 and counts["encounter"] == 1
    assert data["confidence"] == "high" and data["violations"] == []

    sets = data["order_sets"]
    assert [o["order_set_id"] for o in sets] == ["ED_CAP_ADMIT"]
    items = [i for g in sets[0]["groups"] for i in g["items"]]
    by_id = {i["item_id"]: i for i in items}
    assert by_id["ITEM_CAP_CEFTRIAXONE"]["include"] is True
    assert by_id["ITEM_CAP_LEVOFLOX"]["include"] is False          # proposed but excluded
    fields = {f["name"]: f["value"] for f in by_id["ITEM_CAP_CEFTRIAXONE"]["fields"]}
    assert fields["dose"] == "1 g" and fields["route"] == "IV" and fields["frequency"] == "q24h"
    assert by_id["ITEM_CAP_CEFTRIAXONE"]["evidence"]
    assert len(by_id["ITEM_CAP_CEFTRIAXONE"]["variants"]) == 2


async def test_submit_creates_resources_in_ehr(fhir_app, ehr):
    app = fhir_app
    async with _client(app) as client:
        await _launch(app, ehr, client)
        await client.post("/smart/api/proposal", json={})
        res = (await client.post("/smart/api/submit", json={"items": [
            {"order_set_id": "ED_CAP_ADMIT", "item_id": "ITEM_CAP_CEFTRIAXONE",
             "variant_id": "V_CAP_CTX_1G",
             "fields": {"dose": "2 g", "route": "IV", "frequency": "q24h"}},
            {"order_set_id": "ED_CAP_ADMIT", "item_id": "ITEM_CAP_CXR",
             "variant_id": "V_CAP_CXR_2V", "fields": {"priority": "STAT"}},
            {"order_set_id": "ED_CAP_ADMIT", "item_id": "ITEM_NOPE",
             "variant_id": "V_NOPE", "fields": {}},
        ]})).json()

    assert res["mode"] == "fhir"
    assert [c["item_id"] for c in res["created"]] == ["ITEM_CAP_CEFTRIAXONE", "ITEM_CAP_CXR"]
    assert res["failed"][0]["item_id"] == "ITEM_NOPE"
    assert all(c["id"] for c in res["created"])
    assert res["created"][0]["url"].startswith(f"{ISS}/MedicationRequest/")

    med, sr = ehr.state.created
    assert med["resourceType"] == "MedicationRequest"
    assert med["status"] == "draft" and med["intent"] == "order"
    assert med["subject"]["reference"] == f"Patient/{PATIENT_ID}"
    assert med["encounter"]["reference"] == f"Encounter/{ENCOUNTER_ID}"
    assert med["dosageInstruction"][0]["text"] == "2 g IV q24h"      # user edit honoured
    assert med["dosageInstruction"][0]["doseAndRate"][0]["doseQuantity"] == {
        "value": 2, "unit": "g"}
    assert med["dosageInstruction"][0]["route"]["text"] == "IV"
    assert med["dosageInstruction"][0]["timing"]["repeat"]["period"] == 24
    assert sr["resourceType"] == "ServiceRequest" and sr["priority"] == "stat"


# --------------------------------------------------------------------------- #
# hand-back mode (the Epic-viable write path)
# --------------------------------------------------------------------------- #

async def test_handback_submit_stores_refinement_and_audit_row(app, ehr):
    """Default mode: nothing is POSTed to the EHR; the refined set is parked for
    the next CDS hook on this encounter."""
    from epicvibe.cache import refinement_cache

    async with _client(app) as client:
        await _launch(app, ehr, client)
        await client.post("/smart/api/proposal", json={})
        res = (await client.post("/smart/api/submit", json={"items": [
            {"order_set_id": "ED_CAP_ADMIT", "item_id": "ITEM_CAP_CEFTRIAXONE",
             "variant_id": "V_CAP_CTX_1G",
             "fields": {"dose": "2 g", "route": "IV", "frequency": "q24h"}},
            {"order_set_id": "ED_CAP_ADMIT", "item_id": "ITEM_CAP_CXR",
             "variant_id": "V_CAP_CXR_2V", "fields": {"priority": "STAT"}},
            {"order_set_id": "ED_CAP_ADMIT", "item_id": "ITEM_NOPE",
             "variant_id": "V_NOPE", "fields": {}},
        ]})).json()

    assert res["mode"] == "handback"
    assert res["stored"] == 2
    assert res["encounter"] == ENCOUNTER_ID
    assert "order entry" in res["message"]
    assert res["failed"] == [{"item_id": "ITEM_NOPE", "error": "not in catalog"}]
    assert ehr.state.created == []                      # nothing written to the EHR

    refined = refinement_cache(app.state).get_for(ENCOUNTER_ID, PATIENT_ID)
    assert refined is not None and refined.refined_by_user is True and refined.refined_at
    assert refined.encounter_id == ENCOUNTER_ID and refined.patient_id == PATIENT_ID
    items = {i.item_id: i for o in refined.refined.proposal.order_sets for i in o.items}
    assert items["ITEM_CAP_CEFTRIAXONE"].include is True
    assert items["ITEM_CAP_CEFTRIAXONE"].prepopulated["dose"] == "2 g"   # clinician edit
    assert items["ITEM_CAP_CXR"].include is True
    # The AI proposed ceftriaxone *and* levofloxacin; the clinician kept only one,
    # so the dropped item rides along with include=False for the audit diff.
    assert items["ITEM_CAP_LEVOFLOX"].include is False
    assert refined.original is not None                 # original AI proposal kept

    rows = app.state.audit.proposals(kind="refinement")
    assert len(rows) == 1 and rows[0]["encounter_key"] == ENCOUNTER_ID
    assert "ITEM_CAP_CEFTRIAXONE" in rows[0]["proposal_json"]
    assert [r["kind"] for r in app.state.audit.proposals()] == ["proposal", "refinement"]


async def test_handback_is_also_keyed_by_patient(app, ehr):
    """A launch without encounter context still reaches the next hook."""
    from epicvibe.cache import refinement_cache

    async with _client(app) as client:
        await _launch(app, ehr, client)
        await client.post("/smart/api/proposal", json={})
        await client.post("/smart/api/submit", json={"items": [
            {"item_id": "ITEM_CAP_CXR", "variant_id": "V_CAP_CXR_2V", "fields": {}}]})

    cache = refinement_cache(app.state)
    assert cache.get_for(None, PATIENT_ID) is not None
    assert cache.get_for("other-encounter", PATIENT_ID) is not None


async def test_handback_expires_with_ttl(ehr):
    app = _make_app(ehr, smart_handback_ttl_seconds=1)
    async with _client(app) as client:
        await _launch(app, ehr, client)
        await client.post("/smart/api/proposal", json={})
        await client.post("/smart/api/submit", json={"items": [
            {"item_id": "ITEM_CAP_CXR", "variant_id": "V_CAP_CXR_2V", "fields": {}}]})

    from epicvibe.cache import refinement_cache
    cache = refinement_cache(app.state)
    assert cache._ttl == 1
    assert cache.get_for(ENCOUNTER_ID, PATIENT_ID) is not None
    cache._clock = lambda: __import__("time").monotonic() + 5
    assert cache.get_for(ENCOUNTER_ID, PATIENT_ID) is None


async def test_handback_page_labels_the_button_for_the_mode(app, fhir_app, ehr):
    async with _client(app) as client:
        await _launch(app, ehr, client)
        page = (await client.get("/smart/app")).text
    assert "Send to order entry" in page and "submit_mode" in page
    assert (await _mode(app, ehr)) == "handback"
    assert (await _mode(fhir_app, ehr)) == "fhir"


async def _mode(application, ehr) -> str:
    async with _client(application) as client:
        await _launch(application, ehr, client)
        return (await client.get("/smart/api/context")).json()["submit_mode"]


async def test_submit_requires_session(app):
    async with _client(app) as client:
        r = await client.post("/smart/api/submit", json={"items": []})
    assert r.status_code == 401


async def test_dev_launch_disabled_when_setting_off(ehr):
    application = create_app(Settings(_env_file=None, audit_db_path=":memory:",
                                      inference_provider="demo",
                                      smart_allowed_issuers=[ISS],
                                      smart_dev_mode=False))
    application.state.http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ehr), base_url=EHR_BASE)
    async with _client(application) as client:
        assert (await client.get("/smart/dev/launch")).status_code == 404


async def test_dev_launch_redirects_to_configured_iss(ehr):
    application = create_app(Settings(
        _env_file=None, audit_db_path=":memory:", inference_provider="demo",
        smart_dev_iss=ISS, smart_allowed_issuers=[ISS],
        smart_redirect_uri="http://app.test/smart/callback"))
    application.state.http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ehr), base_url=EHR_BASE)
    async with _client(application) as client:
        r = await client.get("/smart/dev/launch", params={"patient": PATIENT_ID})
    assert r.status_code == 302
    assert parse_qs(urlparse(r.headers["location"]).query)["aud"] == [ISS]


async def test_discovery_falls_back_to_capability_statement(app):
    """No smart-configuration document -> oauth-uris from /metadata."""
    from epicvibe.smart.routes import discover_endpoints

    minimal = FastAPI()

    @minimal.get("/fhir/metadata")
    async def metadata() -> dict:
        return {"resourceType": "CapabilityStatement", "rest": [{"security": {"extension": [
            {"url": "http://fhir-registry.smarthealthit.org/StructureDefinition/oauth-uris",
             "extension": [{"url": "authorize", "valueUri": f"{EHR_BASE}/oauth/authorize"},
                           {"url": "token", "valueUri": f"{EHR_BASE}/oauth/token"}]}]}}]}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=minimal),
                                 base_url=EHR_BASE) as client:
        authorize, token = await discover_endpoints(client, ISS)
    assert authorize == f"{EHR_BASE}/oauth/authorize"
    assert token == f"{EHR_BASE}/oauth/token"


async def test_smart_cards_carry_link_and_uuid():
    """Cards emitted by the hooks service link into this app."""
    from pathlib import Path

    from epicvibe.catalog.index import CatalogIndex
    from epicvibe.catalog.loader import load_catalog
    from epicvibe.cds.cards import (render_missing_items_card, render_suggestion_cards,
                                    render_summary_card)
    from epicvibe.proposal.validation import validate_proposal

    index = CatalogIndex(load_catalog(Path("fixtures/catalog/sample_catalog.json")))
    vp = validate_proposal(json.loads(json.dumps({
        "order_sets": [{"order_set_id": "IP_HF_EXACERBATION", "rationale": "HF exacerbation",
                        "items": [{"item_id": "ITEM_HF_FUROSEMIDE", "variant_id": "V_HF_FURO_40",
                                   "include": True, "rationale": "diuresis",
                                   "prepopulated": {"dose": "40 mg", "route": "IV",
                                                    "frequency": "q12h"},
                                   "evidence": ["BNP 1450 pg/mL"]}]}],
        "confidence": "high"})), index)

    url = "http://smart.example/smart/launch"
    cards = render_suggestion_cards(vp, index, "PAT9", encounter_id="ENC9",
                                    user_id="Practitioner/PR1", smart_launch_url=url)
    card = cards[0]
    assert card["uuid"] and card["links"] == [
        {"label": "Open EpicVibe Order Assistant", "url": url, "type": "smart"}]
    resource = card["suggestions"][0]["actions"][0]["resource"]
    assert resource["dosageInstruction"][0]["text"] == "40 mg IV q12h"
    assert resource["encounter"]["reference"] == "Encounter/ENC9"
    assert resource["requester"]["reference"] == "Practitioner/PR1"
    assert resource["authoredOn"]
    assert "BNP 1450 pg/mL" in card["detail"]

    summary = render_summary_card(vp, index, url)
    assert summary["links"][0]["type"] == "smart" and summary["uuid"]
    missing = render_missing_items_card(vp, index, frozenset(), url)
    assert missing["links"][0]["url"] == url


async def test_hook_response_with_links_still_epic_valid():
    from pathlib import Path

    from epicvibe.catalog.index import CatalogIndex
    from epicvibe.catalog.loader import load_catalog
    from epicvibe.cds.cards import render_suggestion_cards
    from epicvibe.proposal.validation import validate_proposal
    from epicvibe.replay.validator import validate_epic_response

    index = CatalogIndex(load_catalog(Path("fixtures/catalog/sample_catalog.json")))
    vp = validate_proposal({"order_sets": [{"order_set_id": "ED_CAP_ADMIT", "rationale": "CAP",
        "items": [{"item_id": "ITEM_CAP_CEFTRIAXONE", "variant_id": "V_CAP_CTX_1G",
                   "include": True, "rationale": "empiric",
                   "prepopulated": {"dose": "1 g", "route": "IV", "frequency": "q24h"}}]}],
        "confidence": "high"}, index)
    response = {"cards": render_suggestion_cards(vp, index, "PAT1", encounter_id="ENC1",
                                                 user_id="Practitioner/PR1")}
    assert validate_epic_response(response, "order-select") == []


# --------------------------------------------------------------------------- #
# issuer allowlist / SSRF + open redirect
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad_iss", [
    "http://169.254.169.254/latest/meta-data",       # cloud metadata (SSRF)
    "http://evil.test/fhir",                         # attacker-controlled EHR
    "http://ehr.test.evil.test/fhir",                # suffix confusion
    "http://ehr.test/fhir/../../internal",           # path traversal
    "file:///etc/passwd",
    "",
])
async def test_launch_rejects_issuers_outside_the_allowlist(app, ehr, bad_iss):
    async with _client(app) as client:
        r = await client.get("/smart/launch", params={"iss": bad_iss})
    assert r.status_code == 400
    assert "issuer" in r.json()["detail"]


async def test_launch_accepts_allowlisted_issuer_with_trailing_slash(app, ehr):
    async with _client(app) as client:
        r = await client.get("/smart/launch", params={"iss": ISS + "/"})
    assert r.status_code == 302
    assert parse_qs(urlparse(r.headers["location"]).query)["aud"] == [ISS]


async def test_dev_launch_rejects_dev_iss_outside_the_allowlist(ehr):
    application = create_app(Settings(
        _env_file=None, audit_db_path=":memory:", inference_provider="demo",
        smart_dev_iss="http://elsewhere.test/fhir", smart_allowed_issuers=[ISS]))
    application.state.http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ehr), base_url=EHR_BASE)
    async with _client(application) as client:
        assert (await client.get("/smart/dev/launch")).status_code == 400


async def test_launch_rejects_discovered_endpoint_on_another_origin():
    """A compromised/hostile discovery document cannot redirect the browser off
    the issuer's origin."""
    evil = FastAPI()

    @evil.get("/fhir/.well-known/smart-configuration")
    async def smart_config() -> dict:
        return {"authorization_endpoint": "http://attacker.test/oauth/authorize",
                "token_endpoint": "http://attacker.test/oauth/token"}

    application = create_app(Settings(
        _env_file=None, audit_db_path=":memory:", inference_provider="demo",
        smart_allowed_issuers=[ISS]))
    application.state.http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=evil), base_url=EHR_BASE)
    async with _client(application) as client:
        r = await client.get("/smart/launch", params={"iss": ISS})
    assert r.status_code == 400
    assert "origin" in r.json()["detail"]


async def test_launch_rejects_token_endpoint_on_another_origin():
    evil = FastAPI()

    @evil.get("/fhir/.well-known/smart-configuration")
    async def smart_config() -> dict:
        return {"authorization_endpoint": f"{EHR_BASE}/oauth/authorize",
                "token_endpoint": "http://attacker.test/oauth/token"}

    application = create_app(Settings(
        _env_file=None, audit_db_path=":memory:", inference_provider="demo",
        smart_allowed_issuers=[ISS]))
    application.state.http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=evil), base_url=EHR_BASE)
    async with _client(application) as client:
        r = await client.get("/smart/launch", params={"iss": ISS})
    assert r.status_code == 400


async def test_default_discovery_client_does_not_follow_redirects():
    """The allowlist is checked before the fetch, so a 30x must not be chased."""
    from epicvibe.smart.routes import _client as build_client

    application = create_app(Settings(_env_file=None, audit_db_path=":memory:"))
    application.state.http_client = None

    class _Req:
        app = application

    client = build_client(_Req())
    try:
        assert client.follow_redirects is False
    finally:
        await client.aclose()


async def test_allowlist_helpers_normalize_trailing_slash():
    from epicvibe.smart.routes import check_endpoint_origin, check_issuer_allowed
    from fastapi import HTTPException

    assert check_issuer_allowed("http://ehr.test/fhir/", [ISS + "/"]) == ISS
    check_endpoint_origin(ISS, f"{EHR_BASE}/oauth/authorize", "")
    with pytest.raises(HTTPException) as exc:
        check_endpoint_origin(ISS, "https://ehr.test/oauth/authorize")   # scheme differs
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException):
        check_endpoint_origin(ISS, "http://ehr.test:8443/oauth/authorize")  # port differs


async def test_smart_proposal_audit_records_the_demo_provider(app, ehr):
    async with _client(app) as client:
        await _launch(app, ehr, client)
        await client.post("/smart/api/proposal", json={})
    rows = app.state.audit.proposals()
    assert rows and rows[0]["model"] == "demo"
