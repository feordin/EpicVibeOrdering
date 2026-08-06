import httpx

from epicvibe.cds.app import create_app
from epicvibe.config import Settings
from epicvibe.replay.epic_fixtures import (epic_order_select_request,
                                           epic_order_sign_request,
                                           epic_patient_view_request)
from epicvibe.replay.validator import validate_epic_response


def _app():
    return create_app(Settings(_env_file=None, audit_db_path=":memory:", inference_provider="demo"))


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_epic_dialect_replay_full_flow():
    app = _app()
    async with _client(app) as c:
        r1 = await c.post("/cds-services/epicvibe-patient-view", json=epic_patient_view_request())
        assert validate_epic_response(r1.json(), "patient-view") == []
        await app.state.runner.join()

        r2 = await c.post("/cds-services/epicvibe-order-select", json=epic_order_select_request())
        body2 = r2.json()
        assert validate_epic_response(body2, "order-select") == []
        labels = {s["label"] for card in body2["cards"] for s in card["suggestions"]}
        # The metformin item is already drafted (as a contained-Medication reference) ->
        # excluded from suggestions. Proves _draft_codes resolves contained Medications.
        assert "metFORMIN 500 mg tablet BID" not in labels
        assert labels == {
            "Hemoglobin A1c",
            "Lipid Panel",
            "Urine Microalbumin/Creatinine Ratio",
            "Referral to Ophthalmology",
        }

        r3 = await c.post("/cds-services/epicvibe-order-sign", json=epic_order_sign_request())
        body3 = r3.json()
        assert validate_epic_response(body3, "order-sign") == []
        detail = body3["cards"][0]["detail"]
        for missing in ("Hemoglobin A1c", "Lipid Panel",
                        "Urine Microalbumin/Creatinine Ratio", "Referral to Ophthalmology"):
            assert missing in detail
        assert "metFORMIN" not in detail
