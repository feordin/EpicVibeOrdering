import json
from pathlib import Path
import httpx
from epicvibe.cds.app import create_app
from epicvibe.config import Settings
from epicvibe.inference.base import FakeProvider

GOOD = {"order_sets": [{"order_set_id": "AMB_DM2_NEWDX", "rationale": "new dx", "items": [
    {"item_id": "ITEM_A1C", "variant_id": "V_A1C", "include": True, "rationale": "baseline"},
    {"item_id": "ITEM_METFORMIN", "variant_id": "V_MET_500", "include": True, "rationale": "first-line"}]}],
    "confidence": "high"}

def _app():
    return create_app(Settings(_env_file=None, audit_db_path=":memory:"),
                      provider=FakeProvider(GOOD))

def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

def _body(name):
    return json.loads(Path(f"fixtures/hooks/{name}.json").read_text())

async def test_discovery():
    async with _client(_app()) as c:
        services = (await c.get("/cds-services")).json()["services"]
    assert {s["id"] for s in services} == {"epicvibe-patient-view", "epicvibe-order-select", "epicvibe-order-sign"}
    assert all("conditions" in s["prefetch"] for s in services)

async def test_patient_view_then_order_select_flow():
    app = _app()
    async with _client(app) as c:
        r1 = await c.post("/cds-services/epicvibe-patient-view", json=_body("patient_view"))
        assert r1.json() == {"cards": []}                    # returns before LLM
        await app.state.runner.join()                        # let background job finish
        r2 = await c.post("/cds-services/epicvibe-order-select", json=_body("order_select"))
        cards = r2.json()["cards"]
        assert len(cards) == 1
        labels = [s["label"] for s in cards[0]["suggestions"]]
        assert labels == ["Hemoglobin A1c"]                  # metformin already drafted -> excluded
        assert app.state.audit.proposals()[0]["encounter_key"] == "ENC1"

async def test_order_select_cold_cache_enqueues():
    app = _app()
    async with _client(app) as c:
        r = await c.post("/cds-services/epicvibe-order-select", json=_body("order_select"))
        assert r.json() == {"cards": []}
        await app.state.runner.join()
        assert app.state.cache.get("ENC1") is not None

async def test_order_sign_missing_items():
    app = _app()
    async with _client(app) as c:
        await c.post("/cds-services/epicvibe-patient-view", json=_body("patient_view"))
        await app.state.runner.join()
        body = _body("order_select")
        body["hook"] = "order-sign"
        r = await c.post("/cds-services/epicvibe-order-sign", json=body)
        assert "Hemoglobin A1c" in r.json()["cards"][0]["detail"]

async def test_handler_error_returns_empty_cards():
    app = _app()
    async with _client(app) as c:
        r = await c.post("/cds-services/epicvibe-patient-view", json={"hook": "patient-view"})
        assert r.status_code == 200 and r.json() == {"cards": []}   # no context -> swallowed

async def test_feedback_endpoint():
    app = _app()
    async with _client(app) as c:
        r = await c.post("/cds-services/epicvibe-order-select/feedback",
                         json={"feedback": [{"card": "u1", "outcome": "accepted"}]})
        assert r.status_code == 200
        assert app.state.audit.feedback()[0]["card_uuid"] == "u1"

async def test_patient_view_cached_empty_does_not_reenqueue():
    provider = FakeProvider({"order_sets": [], "confidence": "low"})
    app = create_app(Settings(_env_file=None, audit_db_path=":memory:"), provider=provider)
    async with _client(app) as c:
        await c.post("/cds-services/epicvibe-patient-view", json=_body("patient_view"))
        await app.state.runner.join()
        calls_after_first = len(provider.calls)
        r = await c.post("/cds-services/epicvibe-patient-view", json=_body("patient_view"))
        await app.state.runner.join()
    assert r.json() == {"cards": []}
    assert len(provider.calls) == calls_after_first          # no second LLM call
    assert len(app.state.audit.proposals()) == 1             # no duplicate audit row

async def test_audit_records_the_real_provider_not_the_anthropic_model():
    app = _app()
    async with _client(app) as c:
        await c.post("/cds-services/epicvibe-patient-view", json=_body("patient_view"))
        await app.state.runner.join()
    row = app.state.audit.proposals()[0]
    assert row["model"] == "fake"                        # not settings.anthropic_model
    assert "claude" not in row["model"]

async def test_demo_provider_is_attributed_to_demo():
    app = create_app(Settings(_env_file=None, audit_db_path=":memory:",
                              inference_provider="demo"))
    async with _client(app) as c:
        await c.post("/cds-services/epicvibe-patient-view", json=_body("patient_view"))
        await app.state.runner.join()
    assert app.state.audit.proposals()[0]["model"] == "demo"

async def test_sync_handlers_never_call_the_provider():
    """Every hook must answer inside Epic's latency budget: the LLM call only
    ever happens in the background job."""
    provider = FakeProvider(GOOD)
    app = create_app(Settings(_env_file=None, audit_db_path=":memory:"), provider=provider)
    async with _client(app) as c:
        assert (await c.post("/cds-services/epicvibe-patient-view",
                             json=_body("patient_view"))).json() == {"cards": []}
        assert provider.calls == []
        assert (await c.post("/cds-services/epicvibe-order-select",
                             json=_body("order_select"))).json() == {"cards": []}
        assert provider.calls == []
        body = _body("order_select")
        body["hook"] = "order-sign"
        assert (await c.post("/cds-services/epicvibe-order-sign", json=body)).json() == {"cards": []}
        assert provider.calls == []
        await app.state.runner.join()
    assert provider.calls                                 # the job did run afterwards

async def test_malformed_bodies_return_empty_cards():
    app = _app()
    bad_bodies = [
        {},
        {"hook": "order-select"},                         # no context
        {"context": {}},                                  # no patientId
        {"context": {"patientId": "PAT1"}, "prefetch": "not-a-dict"},
        {"context": {"patientId": "PAT1"}, "prefetch": {"conditions": 7}},
    ]
    async with _client(app) as c:
        for body in bad_bodies:
            for path in ("epicvibe-order-select", "epicvibe-order-sign"):
                r = await c.post(f"/cds-services/{path}", json=body)
                assert r.status_code == 200, (path, body)
                assert r.json() == {"cards": []}, (path, body)

async def test_prompt_carries_no_direct_identifiers():
    """The engine must never ship name / MRN / DOB to the inference provider."""
    from epicvibe.catalog.index import CatalogIndex
    from epicvibe.catalog.loader import load_catalog
    from epicvibe.proposal.engine import ProposalEngine
    from epicvibe.proposal.patient_summary import summarize_prefetch

    body = _body("patient_view")
    provider = FakeProvider(GOOD)
    index = CatalogIndex(load_catalog(Path("fixtures/catalog/sample_catalog.json")))
    summary = summarize_prefetch(body["context"], body["prefetch"])
    await ProposalEngine(index, provider).generate(summary)

    patient = body["prefetch"]["patient"]
    name = patient["name"][0]
    identifiers = [i["value"] for i in patient.get("identifier", [])]
    secrets = [patient["birthDate"], name["family"], *name["given"], *identifiers]
    assert secrets and provider.calls
    sent = provider.calls[0]["system"] + provider.calls[0]["user"]
    for secret in secrets:
        assert secret not in sent, secret


# --------------------------------------------------------------------------- #
# SMART hand-back: a clinician-reviewed set replaces the raw AI proposal
# --------------------------------------------------------------------------- #

def _store_refinement(app, items, *, encounter="ENC1", patient="PAT1", original=None):
    """Stand in for POST /smart/api/submit in handback mode."""
    from epicvibe.cache import refinement_cache
    from epicvibe.proposal.refinement import build_refinement

    refined, failed = build_refinement(app.state.index, items, original=original,
                                       patient_id=patient, encounter_id=encounter)
    refinement_cache(app.state).put_for(encounter, patient, refined)
    return refined, failed


async def _prime(app, c):
    """Run patient-view so the AI proposal is cached for ENC1."""
    await c.post("/cds-services/epicvibe-patient-view", json=_body("patient_view"))
    await app.state.runner.join()
    return app.state.cache.get("ENC1")


async def test_order_select_prefers_the_reviewed_set():
    app = _app()
    async with _client(app) as c:
        original = await _prime(app, c)
        # Clinician dropped the A1c, swapped metformin to the 1000 mg variant and
        # made the lipid panel STAT.
        _store_refinement(app, [
            {"item_id": "ITEM_METFORMIN", "variant_id": "V_MET_1000",
             "fields": {"dose": "1000 mg", "route": "oral", "frequency": "BID"}},
            {"item_id": "ITEM_LIPID", "variant_id": "V_LIPID",
             "fields": {"priority": "STAT"}},
        ], original=original)
        cards = (await c.post("/cds-services/epicvibe-order-select",
                              json=_body("order_select"))).json()["cards"]

    assert len(cards) == 1
    card = cards[0]
    assert card["summary"] == "Reviewed in Order Assistant: 2 selected orders"
    labels = sorted(s["label"] for s in card["suggestions"])
    assert labels == ["Lipid Panel", "metFORMIN 1000 mg tablet BID"]   # no A1c
    assert all(s["isRecommended"] is True for s in card["suggestions"])
    # Clinician-edited values reach both the detail text and the create action.
    assert "dose: 1000 mg" in card["detail"] and "priority: STAT" in card["detail"]
    by_label = {s["label"]: s["actions"][0]["resource"] for s in card["suggestions"]}
    med = by_label["metFORMIN 1000 mg tablet BID"]
    assert med["dosageInstruction"][0]["text"] == "1000 mg oral BID"
    assert by_label["Lipid Panel"]["priority"] == "stat"


async def test_reviewed_set_still_dedups_against_draft_orders():
    app = _app()
    async with _client(app) as c:
        original = await _prime(app, c)
        # PREF_MET_500 is already in the draft-order bundle of the fixture.
        _store_refinement(app, [
            {"item_id": "ITEM_METFORMIN", "variant_id": "V_MET_500", "fields": {}},
            {"item_id": "ITEM_LIPID", "variant_id": "V_LIPID", "fields": {}},
        ], original=original)
        cards = (await c.post("/cds-services/epicvibe-order-select",
                              json=_body("order_select"))).json()["cards"]

    assert [s["label"] for s in cards[0]["suggestions"]] == ["Lipid Panel"]
    assert cards[0]["summary"] == "Reviewed in Order Assistant: 1 selected orders"


async def test_order_sign_prefers_the_reviewed_set():
    app = _app()
    async with _client(app) as c:
        original = await _prime(app, c)
        _store_refinement(app, [
            {"item_id": "ITEM_LIPID", "variant_id": "V_LIPID", "fields": {"priority": "STAT"}},
        ], original=original)
        body = _body("order_select")
        body["hook"] = "order-sign"
        card = (await c.post("/cds-services/epicvibe-order-sign", json=body)).json()["cards"][0]

    assert card["summary"].startswith("Reviewed in Order Assistant:")
    assert "Lipid Panel" in card["detail"] and "priority: STAT" in card["detail"]
    assert "Hemoglobin A1c" not in card["detail"]      # AI-only item is not resurrected


async def test_patient_view_announces_a_waiting_reviewed_set():
    app = _app()
    async with _client(app) as c:
        original = await _prime(app, c)
        _store_refinement(app, [
            {"item_id": "ITEM_LIPID", "variant_id": "V_LIPID", "fields": {}}], original=original)
        card = (await c.post("/cds-services/epicvibe-patient-view",
                             json=_body("patient_view"))).json()["cards"][0]

    assert card["summary"] == "Reviewed in Order Assistant: 1 selected orders"
    assert "waiting" in card["detail"]


async def test_reviewed_set_expires_and_falls_back_to_the_ai_proposal():
    from epicvibe.cache import refinement_cache

    app = _app()
    async with _client(app) as c:
        original = await _prime(app, c)
        _store_refinement(app, [
            {"item_id": "ITEM_LIPID", "variant_id": "V_LIPID", "fields": {}}], original=original)
        cache = refinement_cache(app.state)
        cache._clock = lambda: 10 ** 9              # every entry is now past its TTL
        cards = (await c.post("/cds-services/epicvibe-order-select",
                              json=_body("order_select"))).json()["cards"]

    assert cards[0]["summary"].endswith("suggested orders")           # AI wording
    assert [s["label"] for s in cards[0]["suggestions"]] == ["Hemoglobin A1c"]


async def test_reviewed_set_found_by_patient_when_hook_has_no_encounter():
    app = _app()
    async with _client(app) as c:
        await _prime(app, c)
        _store_refinement(app, [
            {"item_id": "ITEM_LIPID", "variant_id": "V_LIPID", "fields": {}}],
            encounter="", patient="PAT1")
        body = _body("order_select")
        body["context"].pop("encounterId")
        cards = (await c.post("/cds-services/epicvibe-order-select", json=body)).json()["cards"]

    assert cards[0]["summary"] == "Reviewed in Order Assistant: 1 selected orders"
