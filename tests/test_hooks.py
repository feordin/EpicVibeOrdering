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
