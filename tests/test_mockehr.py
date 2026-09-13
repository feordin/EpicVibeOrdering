"""Mock EHR tests.

The end-to-end tests wire the real CDS service in-process via ``httpx.ASGITransport``
so the whole chain -- chart open -> patient-view -> background job -> order-select ->
accept -> resource in the FHIR store -- runs without a network.
"""
import base64
import hashlib

import httpx
import pytest

from epicvibe.cds.app import create_app as create_cds_app
from epicvibe.config import Settings
from epicvibe.inference.demo import DemoProvider
from epicvibe.mockehr.app import create_app as create_mockehr_app
from epicvibe.mockehr.fhir_api import capability_statement, smart_configuration
from epicvibe.mockehr.settings import MockEhrSettings
from epicvibe.mockehr.store import FhirStore, MissingContextValue, resolve_template

PATIENT_IDS = {"pat-santos", "pat-okafor", "pat-brooks"}

VERIFIER = "a" * 64
CHALLENGE = base64.urlsafe_b64encode(
    hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")


def auth(token: str = "") -> dict:
    """Authorization header for the FHIR REST surface."""
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------- fixtures
@pytest.fixture
def settings() -> MockEhrSettings:
    return MockEhrSettings(base_url="http://localhost:8100",
                           cds_base_url="http://localhost:8000")


@pytest.fixture
def store(settings) -> FhirStore:
    return FhirStore(settings.fixtures_dir)


@pytest.fixture
def cds_app():
    return create_cds_app(Settings(_env_file=None, audit_db_path=":memory:"),
                          provider=DemoProvider())


@pytest.fixture
def cds_client(cds_app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=cds_app),
                             base_url="http://cds.test")


@pytest.fixture
def app(settings, cds_client):
    return create_mockehr_app(settings, cds_client=cds_client)


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://ehr.test")


# ----------------------------------------------------------------------- store
def test_store_loads_three_patients(store):
    assert {p["id"] for p in store.all_of("Patient")} == PATIENT_IDS
    assert store.get("Patient", "pat-santos")["name"][0]["family"] == "Santos"
    assert store.get("Encounter", "enc-santos-1")["status"] == "in-progress"


def test_store_search_by_patient(store):
    conditions = store.search("Condition", {"patient": "pat-okafor"})
    assert {c["id"] for c in conditions} == {"cond-okafor-cap", "cond-okafor-copd",
                                             "cond-okafor-ckd3"}
    # subject= is an accepted alias, and Patient/<id> references resolve too
    assert len(store.search("Condition", {"subject": "Patient/pat-okafor"})) == 3
    # AllergyIntolerance links through `patient`, not `subject`
    assert [a["id"] for a in store.search("AllergyIntolerance",
                                          {"patient": "pat-santos"})] == ["alg-santos-pcn"]


def test_store_search_filters_and_ignores_unknown_params(store):
    active = store.search("MedicationRequest", {"patient": "pat-brooks", "status": "active"})
    assert len(active) == 3
    assert store.search("MedicationRequest", {"patient": "pat-brooks",
                                              "status": "cancelled"}) == []
    # clinical-status, _id, code, category
    assert len(store.search("Condition", {"patient": "pat-santos",
                                          "clinical-status": "active"})) == 3
    assert [c["id"] for c in store.search("Condition", {"_id": "cond-santos-htn"})] \
        == ["cond-santos-htn"]
    by_code = store.search("Observation", {"patient": "pat-brooks",
                                           "code": "http://loinc.org|30934-4"})
    assert [o["id"] for o in by_code] == ["obs-brooks-bnp"]
    vitals = store.search("Observation", {"patient": "pat-santos", "category": "vital-signs"})
    assert {o["id"] for o in vitals} == {"obs-santos-bmi", "obs-santos-bp"}
    # unknown params must not filter anything out
    assert len(store.search("Condition", {"patient": "pat-santos", "_sort": "-date",
                                          "made-up": "x"})) == 3


def test_store_create_and_update(store):
    created = store.create("ServiceRequest", {"status": "draft", "intent": "proposal",
                                              "subject": {"reference": "Patient/pat-santos"}})
    assert created["id"].startswith("servicerequest-gen-")
    assert created["meta"]["lastUpdated"].endswith("Z")
    assert store.get("ServiceRequest", created["id"]) is created
    updated = store.update("ServiceRequest", created["id"], {**created, "status": "active"})
    assert updated["status"] == "active"
    assert updated["meta"]["versionId"] == "2"


def test_prefetch_template_resolution(store):
    context = {"patientId": "pat-santos", "encounterId": "enc-santos-1"}
    query = resolve_template(
        "Condition?patient={{context.patientId}}&clinical-status=active", context)
    assert query == "Condition?patient=pat-santos&clinical-status=active"
    bundle = store.search_relative(query, base_url="http://localhost:8100/fhir")
    assert bundle["resourceType"] == "Bundle" and bundle["type"] == "searchset"
    assert bundle["total"] == 3
    assert bundle["entry"][0]["fullUrl"].startswith("http://localhost:8100/fhir/Condition/")
    # instance read form
    single = store.search_relative("Patient/pat-brooks")
    assert single["total"] == 1


def test_prefetch_template_with_a_missing_context_value_is_not_executed(store):
    # No encounterId in context: `Encounter/{{context.encounterId}}` would otherwise
    # collapse to `Encounter/` and return EVERY patient's encounters.
    with pytest.raises(MissingContextValue):
        resolve_template("Encounter/{{context.encounterId}}", {"patientId": "pat-santos"})
    with pytest.raises(MissingContextValue):
        resolve_template("Encounter/{{context.encounterId}}",
                         {"patientId": "pat-santos", "encounterId": ""})


def test_instance_read_with_an_empty_id_is_an_empty_result(store):
    assert store.search_relative("Encounter/")["total"] == 0
    assert store.search_relative("Encounter/  ")["total"] == 0
    assert len(store.all_of("Encounter")) >= 3          # ...and not because there are none


def test_prefetch_omits_keys_whose_context_value_is_missing(app):
    hooks = app.state.hooks
    service = {"id": "x", "hook": "patient-view",
               "prefetch": {"patient": "Patient/{{context.patientId}}",
                            "encounter": "Encounter/{{context.encounterId}}"}}
    prefetch = hooks.resolve_prefetch(service, {"patientId": "pat-santos"})
    assert set(prefetch) == {"patient"}                 # the key is absent, not empty
    assert prefetch["patient"]["total"] == 1

    both = hooks.resolve_prefetch(
        service, {"patientId": "pat-santos", "encounterId": "enc-santos-1"})
    assert set(both) == {"patient", "encounter"}
    assert [e["resource"]["id"] for e in both["encounter"]["entry"]] == ["enc-santos-1"]


def test_fixtures_dir_default_is_repo_relative(tmp_path, monkeypatch):
    from epicvibe.mockehr.settings import default_fixtures_dir

    monkeypatch.chdir(tmp_path)                         # nothing resolvable from here
    resolved = default_fixtures_dir()
    assert resolved.is_absolute() and resolved.is_dir()
    assert MockEhrSettings().fixtures_dir == resolved
    assert FhirStore(MockEhrSettings().fixtures_dir).all_of("Patient")


def test_app_refuses_to_start_with_an_empty_store(tmp_path, settings):
    empty = settings.model_copy(update={"fixtures_dir": tmp_path})
    with pytest.raises(RuntimeError, match="0 Patient resources"):
        create_mockehr_app(empty)


def test_smart_configuration_and_capability_statement():
    config = smart_configuration("http://localhost:8100")
    assert config["authorization_endpoint"] == "http://localhost:8100/oauth/authorize"
    assert config["token_endpoint"] == "http://localhost:8100/oauth/token"
    cap = capability_statement("http://localhost:8100")
    security = cap["rest"][0]["security"]["extension"][0]
    assert security["url"].endswith("/oauth-uris")
    uris = {e["url"]: e["valueUri"] for e in security["extension"]}
    assert uris["authorize"] == "http://localhost:8100/oauth/authorize"
    assert uris["token"] == "http://localhost:8100/oauth/token"


# ------------------------------------------------------------------ FHIR REST
async def test_fhir_rest_surface(app):
    async with client(app) as c:
        meta = await c.get("/fhir/metadata")
        assert meta.status_code == 200
        assert meta.json()["resourceType"] == "CapabilityStatement"

        smart = await c.get("/fhir/.well-known/smart-configuration")
        assert smart.json()["token_endpoint"].endswith("/oauth/token")

        token = app.state.oauth.mint_token()
        read = await c.get("/fhir/Patient/pat-santos", headers=auth(token))
        assert read.status_code == 200
        assert read.json()["name"][0]["family"] == "Santos"

        assert (await c.get("/fhir/Patient/nope", headers=auth(token))).status_code == 404

        search = await c.get("/fhir/Condition", headers=auth(token),
                             params={"patient": "pat-brooks", "clinical-status": "active"})
        body = search.json()
        assert body["type"] == "searchset" and body["total"] == 3

        created = await c.post("/fhir/ServiceRequest", headers=auth(token), json={
            "resourceType": "ServiceRequest", "status": "draft", "intent": "proposal",
            "subject": {"reference": "Patient/pat-santos"},
            "code": {"text": "Comprehensive metabolic panel"}})
        assert created.status_code == 201
        new_id = created.json()["id"]
        assert "Location" in created.headers

        put = await c.put(f"/fhir/ServiceRequest/{new_id}", headers=auth(token),
                          json={**created.json(), "status": "active"})
        assert put.status_code == 200 and put.json()["status"] == "active"


async def test_fhir_requires_a_bearer_token_the_stub_minted(app):
    async with client(app) as c:
        # open endpoints: a client needs these before it can get a token
        assert (await c.get("/fhir/metadata")).status_code == 200
        assert (await c.get("/fhir/.well-known/smart-configuration")).status_code == 200

        no_header = await c.get("/fhir/Patient/pat-santos")
        assert no_header.status_code == 401
        assert no_header.headers["WWW-Authenticate"].startswith("Bearer")
        assert no_header.json()["resourceType"] == "OperationOutcome"

        assert (await c.get("/fhir/Patient/pat-santos",
                            headers={"Authorization": "Basic abc"})).status_code == 401
        assert (await c.get("/fhir/Patient/pat-santos",
                            headers=auth("not-a-real-token"))).status_code == 401
        assert (await c.get("/fhir/Condition", headers=auth("nope"))).status_code == 401
        assert (await c.post("/fhir/ServiceRequest", headers=auth("nope"),
                             json={"resourceType": "ServiceRequest"})).status_code == 401
        assert (await c.put("/fhir/ServiceRequest/x", headers=auth("nope"),
                            json={})).status_code == 401

        assert (await c.get("/fhir/Patient/pat-santos",
                            headers=auth(app.state.oauth.mint_token()))).status_code == 200


# ---------------------------------------------------------------------- OAuth
async def test_oauth_code_to_token_roundtrip(app):
    async with client(app) as c:
        launch = await c.post("/api/smart/launch",
                              json={"patientId": "pat-santos",
                                    "url": "http://localhost:8000/smart/launch"})
        info = launch.json()
        assert info["encounterId"] == "enc-santos-1"
        assert "iss=http://localhost:8100/fhir" in info["url"]
        assert f"launch={info['launchId']}" in info["url"]

        authorize = await c.get("/oauth/authorize", params={
            "response_type": "code", "client_id": "epicvibe-order-assistant",
            "redirect_uri": "http://localhost:8000/smart/callback",
            "scope": "launch openid fhirUser patient/*.read", "state": "xyz 123&x=1",
            "aud": "http://localhost:8100/fhir", "launch": info["launchId"],
            "code_challenge": CHALLENGE, "code_challenge_method": "S256"})
        assert authorize.status_code == 302
        location = httpx.URL(authorize.headers["location"])
        params = dict(location.params)
        # state and code are URL-encoded, so a hostile state cannot inject parameters
        assert params["state"] == "xyz 123&x=1" and params["code"]
        assert "xyz+123%26x%3D1" in authorize.headers["location"]

        token = await c.post("/oauth/token", data={
            "grant_type": "authorization_code", "code": params["code"],
            "redirect_uri": "http://localhost:8000/smart/callback",
            "client_id": "epicvibe-order-assistant", "code_verifier": VERIFIER})
        body = token.json()
        assert body["token_type"] == "Bearer"
        assert body["patient"] == "pat-santos"
        assert body["encounter"] == "enc-santos-1"
        assert body["expires_in"] == 3600
        assert body["need_patient_banner"] is False
        assert "id_token" not in body

        # codes are single use
        reuse = await c.post("/oauth/token", data={"grant_type": "authorization_code",
                                                   "code": params["code"]})
        assert reuse.status_code == 400 and reuse.json()["error"] == "invalid_grant"


async def test_dev_launch_id_resolves_to_that_patient(app):
    # A "dev-<patientId>" launch id is unknown to the launch store, but it must not fall
    # back to the first patient (pat-brooks) -- it should resolve to the named patient.
    async with client(app) as c:
        authorize = await c.get("/oauth/authorize", params={
            "response_type": "code", "client_id": "epicvibe-order-assistant",
            "redirect_uri": "http://localhost:8000/smart/callback",
            "scope": "launch openid fhirUser patient/*.read", "state": "abc789",
            "launch": "dev-pat-okafor"})
        location = httpx.URL(authorize.headers["location"])
        params = dict(location.params)
        assert params["state"] == "abc789" and params["code"]

        token = await c.post("/oauth/token", data={
            "grant_type": "authorization_code", "code": params["code"],
            "redirect_uri": "http://localhost:8000/smart/callback",
            "client_id": "epicvibe-order-assistant"})
        body = token.json()
        assert body["patient"] == "pat-okafor"
        assert body["encounter"]


async def _code(c, **overrides) -> str:
    params = {"response_type": "code", "client_id": "epicvibe-order-assistant",
              "redirect_uri": "http://localhost:8000/smart/callback",
              "scope": "launch patient/*.read"}
    params.update(overrides)
    response = await c.get("/oauth/authorize", params=params)
    assert response.status_code == 302, response.text
    return dict(httpx.URL(response.headers["location"]).params)["code"]


async def test_authorize_rejects_unsafe_redirect_uris(app):
    async with client(app) as c:
        # registered client: must stay under the CDS base URL
        evil = await c.get("/oauth/authorize", params={
            "response_type": "code", "client_id": "epicvibe-order-assistant",
            "redirect_uri": "http://evil.example/steal"})
        assert evil.status_code == 400 and "must start with" in evil.json()["detail"]

        # unregistered client: loopback only
        remote = await c.get("/oauth/authorize", params={
            "response_type": "code", "client_id": "some-other-app",
            "redirect_uri": "http://evil.example/cb"})
        assert remote.status_code == 400 and "loopback" in remote.json()["detail"]

        loopback = await c.get("/oauth/authorize", params={
            "response_type": "code", "client_id": "some-other-app",
            "redirect_uri": "http://127.0.0.1:4321/cb"})
        assert loopback.status_code == 302

        # non-http schemes are out
        for uri in ("javascript:alert(1)", "file:///etc/passwd", "ftp://x/y"):
            bad = await c.get("/oauth/authorize", params={
                "response_type": "code", "client_id": "epicvibe-order-assistant",
                "redirect_uri": uri})
            assert bad.status_code == 400


async def test_token_rejects_a_mismatched_redirect_uri_or_client_id(app):
    async with client(app) as c:
        code = await _code(c)
        bad_uri = await c.post("/oauth/token", data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": "http://localhost:8000/somewhere-else",
            "client_id": "epicvibe-order-assistant"})
        assert bad_uri.status_code == 400
        assert bad_uri.json()["error"] == "invalid_grant"

        code = await _code(c)
        bad_client = await c.post("/oauth/token", data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": "http://localhost:8000/smart/callback",
            "client_id": "someone-else"})
        assert bad_client.status_code == 400
        assert "client_id" in bad_client.json()["error_description"]


async def test_pkce_is_optional_but_verified_when_used(app):
    async with client(app) as c:
        # omitted entirely -> still works
        plain = await c.post("/oauth/token", data={
            "grant_type": "authorization_code", "code": await _code(c)})
        assert plain.status_code == 200 and plain.json()["access_token"]

        code = await _code(c, code_challenge=CHALLENGE, code_challenge_method="S256")
        missing = await c.post("/oauth/token", data={
            "grant_type": "authorization_code", "code": code})
        assert missing.status_code == 400
        assert "code_verifier is required" in missing.json()["error_description"]

        code = await _code(c, code_challenge=CHALLENGE, code_challenge_method="S256")
        wrong = await c.post("/oauth/token", data={
            "grant_type": "authorization_code", "code": code, "code_verifier": "b" * 64})
        assert wrong.status_code == 400
        assert "does not match" in wrong.json()["error_description"]

        code = await _code(c, code_challenge=CHALLENGE, code_challenge_method="S256")
        good = await c.post("/oauth/token", data={
            "grant_type": "authorization_code", "code": code, "code_verifier": VERIFIER})
        assert good.status_code == 200
        assert app.state.oauth.token_info(good.json()["access_token"]) is not None


# ------------------------------------------------------------- hooks_client
async def test_fire_hook_builds_a_conformant_request(app):
    hooks = app.state.hooks
    await hooks.refresh()
    assert hooks.discovery_error is None
    service = hooks.service_for("patient-view")
    body = hooks.build_request("patient-view", service, patient_id="pat-santos",
                               encounter_id="enc-santos-1")
    assert body["hook"] == "patient-view"
    assert len(body["hookInstance"]) == 36
    assert body["fhirServer"] == "http://localhost:8100/fhir"
    auth = body["fhirAuthorization"]
    assert auth["token_type"] == "Bearer" and auth["subject"] == "mockehr"
    assert auth["expires_in"] == 3600 and auth["access_token"].startswith("mockehr-")
    assert body["context"] == {"userId": "Practitioner/pr-1", "patientId": "pat-santos",
                               "encounterId": "enc-santos-1"}
    # every advertised prefetch template was resolved against the local store
    assert set(body["prefetch"]) == set(service["prefetch"])
    conditions = body["prefetch"]["conditions"]
    assert conditions["type"] == "searchset" and conditions["total"] == 3
    assert all(e["resource"]["subject"]["reference"] == "Patient/pat-santos"
               for e in conditions["entry"])
    assert body["prefetch"]["medications"]["total"] == 2


async def test_hook_reports_error_when_cds_service_is_down(settings):
    down = create_mockehr_app(settings.model_copy(
        update={"cds_base_url": "http://127.0.0.1:9", "hook_timeout_seconds": 0.5}))
    async with client(down) as c:
        status = (await c.post("/api/cds/refresh")).json()
        assert status["ok"] is False and status["error"]
        hook = (await c.post("/api/hooks/patient-view",
                             json={"patientId": "pat-santos"})).json()
        assert hook["cards"] == [] and "patient-view" in hook["error"]


async def test_feedback_skipped_when_card_has_no_uuid(app):
    result = await app.state.hooks.send_feedback("epicvibe-order-select", None, None)
    assert result == {"sent": False, "reason": "card has no uuid"}


# --------------------------------------------------------------- chart API
async def test_patient_list_and_chart(app):
    async with client(app) as c:
        patients = (await c.get("/api/patients")).json()["patients"]
        assert {p["id"] for p in patients} == PATIENT_IDS
        brooks = next(p for p in patients if p["id"] == "pat-brooks")
        assert brooks["name"] == "Evelyn R Brooks"
        assert brooks["age"] >= 74 and brooks["mrn"] == "MRN-100812"
        assert brooks["encounterId"] == "enc-brooks-1"

        chart = (await c.get("/api/patients/pat-okafor/chart")).json()
        assert chart["patient"]["name"] == "James A Okafor"
        assert chart["encounter"]["id"] == "enc-okafor-1"
        assert any("pneumonia" in p["display"].lower() for p in chart["problems"])
        assert any(m["display"].startswith("tiotropium") for m in chart["medications"])
        assert [a["display"] for a in chart["allergies"]] == ["Sulfa drugs"]
        assert any(lab["name"] == "eGFR" and lab["value"].startswith("48")
                   for lab in chart["labs"])
        assert any(v["name"] == "SpO2" for v in chart["vitals"])
        # the completed chest x-ray shows up as a filed order, not a draft
        assert chart["orders"]["drafts"] == []
        assert any(o["id"] == "srv-okafor-cxr" and o["status"] == "completed"
                   for o in chart["orders"]["active"])


async def test_reset_reloads_fixtures(app):
    async with client(app) as c:
        await c.post("/api/orders/draft", json={"patientId": "pat-santos",
                                                "text": "Throwaway order"})
        chart = (await c.get("/api/patients/pat-santos/chart")).json()
        assert len(chart["orders"]["drafts"]) == 1
        reset = (await c.post("/api/reset")).json()
        assert reset["reset"] is True and reset["resources"]["Patient"] == 3
        chart = (await c.get("/api/patients/pat-santos/chart")).json()
        assert chart["orders"]["drafts"] == []


# ------------------------------------------------------------------ end to end
async def test_end_to_end_chart_open_to_accepted_order(app, cds_app):
    """open chart -> patient-view -> job -> order-select -> accept -> store write."""
    async with client(app) as c:
        first = (await c.post("/api/hooks/patient-view",
                              json={"patientId": "pat-santos"})).json()
        assert first["error"] is None
        assert first["cards"] == []                         # warm-up call, async job queued
        assert first["request"]["hook"] == "patient-view"

        await cds_app.state.runner.join()                   # let the proposal finish

        second = (await c.post("/api/hooks/patient-view",
                               json={"patientId": "pat-santos"})).json()
        assert len(second["cards"]) == 1
        assert "AI order review ready" in second["cards"][0]["summary"]

        select = (await c.post("/api/orders/draft",
                               json={"patientId": "pat-santos",
                                     "text": "Basic Metabolic Panel"})).json()
        hook = select["hook"]
        assert hook["hook"] == "order-select"
        # the manual draft is in draftOrders and is the current selection
        draft_ref = select["draft"]["reference"]
        assert hook["request"]["context"]["selections"] == [draft_ref]
        bundle = hook["request"]["context"]["draftOrders"]
        assert [e["resource"]["id"] for e in bundle["entry"]] == [select["draft"]["id"]]
        assert hook["cards"], "demo provider should produce suggestion cards"

        card = hook["cards"][0]
        suggestion = card["suggestions"][0]
        action = next(a for a in suggestion["actions"] if a["type"] == "create")

        accepted = (await c.post("/api/suggestions/accept", json={
            "patientId": "pat-santos", "resource": action["resource"],
            "serviceId": hook["serviceId"], "cardUuid": card.get("uuid"),
            "suggestionUuid": suggestion["uuid"]})).json()
        created_id = accepted["created"]["id"]

        # the accepted resource really is in the FHIR store, as a draft on this encounter
        stored = (await c.get(
            f"/fhir/{accepted['created']['resourceType']}/{created_id}",
            headers=auth(app.state.oauth.mint_token()))).json()
        assert stored["status"] == "draft"
        assert stored["subject"]["reference"] == "Patient/pat-santos"
        assert stored["encounter"]["reference"] == "Encounter/enc-santos-1"
        assert stored["meta"]["source"] == "cds-suggestion"
        assert created_id in [d["id"] for d in accepted["orders"]["drafts"]]

        signed = (await c.post("/api/orders/sign",
                               json={"patientId": "pat-santos"})).json()
        assert len(signed["signed"]) == 2                   # manual draft + accepted one
        assert signed["hook"]["hook"] == "order-sign"
        assert signed["hook"]["request"]["context"]["draftOrders"]["entry"]
        assert signed["orders"]["drafts"] == []
        assert all(o["status"] == "active"
                   for o in signed["orders"]["active"] if o["id"] == created_id)

        history = (await c.get("/api/dev/history")).json()
        assert [h["hook"] for h in history["history"]][:1] == ["order-sign"]
        assert history["discovery"]["ok"] is True


async def test_order_select_sends_every_draft_as_a_selection(app):
    async with client(app) as c:
        first = (await c.post("/api/orders/draft",
                              json={"patientId": "pat-santos", "text": "CBC"})).json()
        second = (await c.post("/api/orders/draft",
                               json={"patientId": "pat-santos", "text": "BMP"})).json()
        hook = (await c.post("/api/hooks/order-select",
                             json={"patientId": "pat-santos"})).json()
        selections = hook["request"]["context"]["selections"]
        assert set(selections) == {first["draft"]["reference"], second["draft"]["reference"]}
        assert len(hook["request"]["context"]["draftOrders"]["entry"]) == 2


async def test_sign_is_blocked_by_a_critical_card_and_leaves_drafts_alone(app):
    async def critical_hook(hook, **kwargs):
        return {"hook": hook, "serviceId": "stub", "at": "now", "error": None,
                "request": None, "response": None,
                "cards": [{"summary": "Contraindicated", "indicator": "critical"}]}

    async with client(app) as c:
        await c.post("/api/orders/draft", json={"patientId": "pat-santos", "text": "CBC"})
        app.state.hooks.fire_hook = critical_hook
        result = (await c.post("/api/orders/sign",
                               json={"patientId": "pat-santos"})).json()
        assert result["blocked"] is True and result["signed"] == []
        assert result["blockedBy"] == ["Contraindicated"]
        assert len(result["orders"]["drafts"]) == 1
        chart = (await c.get("/api/patients/pat-santos/chart")).json()
        assert len(chart["orders"]["drafts"]) == 1


async def test_sign_does_not_rewrite_the_recorded_hook_request(app):
    async with client(app) as c:
        await c.post("/api/orders/draft", json={"patientId": "pat-santos", "text": "CBC"})
        signed = (await c.post("/api/orders/sign",
                               json={"patientId": "pat-santos"})).json()
        assert signed["blocked"] is False and len(signed["signed"]) == 1
        # the order-sign request is a record of what was SENT: drafts, not signed orders
        sent = signed["hook"]["request"]["context"]["draftOrders"]["entry"]
        assert [e["resource"]["status"] for e in sent] == ["draft"]
        assert [e["resource"]["intent"] for e in sent] == ["proposal"]


async def test_dev_history_redacts_tokens_and_prefetch_bundles(app):
    async with client(app) as c:
        await c.post("/api/hooks/patient-view", json={"patientId": "pat-santos"})
        await c.post("/api/orders/draft", json={"patientId": "pat-santos", "text": "CBC"})
        history = (await c.get("/api/dev/history")).json()["history"]
        entry = next(h for h in history if h["hook"] == "patient-view")

        assert entry["request"]["fhirAuthorization"]["access_token"] == "***"
        for key, bundle in entry["request"]["prefetch"].items():
            assert bundle == {"resourceType": "Bundle", "total": bundle["total"],
                              "_redacted": True}
            assert "entry" not in bundle
        assert entry["prefetchCounts"]["conditions"] == 3
        assert entry["context"]["patientId"] == "pat-santos"

        select = next(h for h in history if h["hook"] == "order-select")
        assert select["context"]["draftOrders"]["_redacted"] is True
        assert "entry" not in select["context"]["draftOrders"]
        # ...and the full CDS response is still there for the dev panel
        assert select["response"] is not None

        raw = (await c.get("/api/dev/history")).text
        assert "mockehr-" not in raw                    # no minted token anywhere


async def test_unknown_patient_chart_is_404(app):
    async with client(app) as c:
        missing = await c.get("/api/patients/pat-nobody/chart")
        assert missing.status_code == 404
        assert "unknown patient" in missing.json()["error"]


async def test_accept_rejects_non_order_resources(app):
    async with client(app) as c:
        result = (await c.post("/api/suggestions/accept", json={
            "patientId": "pat-santos",
            "resource": {"resourceType": "Patient"}})).json()
        assert "unsupported" in result["error"]


async def test_ui_page_served(app):
    async with client(app) as c:
        page = await c.get("/")
        assert page.status_code == 200
        assert "Mock EHR" in page.text
        assert "/api/hooks/patient-view" in page.text
        assert "errBar" in page.text and "function guard(" in page.text
        assert "devRequest" not in page.text          # the raw request is not shown


async def test_orders_panel_has_a_recheck_suggestions_button(app):
    """The way back from the SMART app: re-fire order-select with the current
    drafts so the reviewed set renders as suggestions."""
    async with client(app) as c:
        page = (await c.get("/")).text
    assert "Re-check suggestions" in page and 'id="btnRecheck"' in page
    assert '$("btnRecheck").onclick = fireOrderSelect;' in page
    assert '"/api/hooks/order-select"' in page
