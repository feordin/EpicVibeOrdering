"""API tests for the downtime capture app: generate -> save -> sign -> submit.

The mock integration engine runs in a background thread with its own event
loop, because `TestClient` blocks the calling thread while the app's loop
handles the request - an engine in the test's own loop would deadlock.
"""

import asyncio
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from epicvibe.downtime.app import create_app
from epicvibe.downtime.config import DowntimeSettings
from epicvibe.downtime.mock_engine import MockIntegrationEngine
from epicvibe.downtime.offline import OfflineSettings
from epicvibe.downtime.transcribe import (
    INSTALL_HINT,
    TranscribeSettings,
    TranscriberUnavailable,
)

REPO = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = REPO / "fixtures" / "downtime" / "templates"
TRANSCRIPT_DIR = REPO / "fixtures" / "downtime" / "transcripts"


class ThreadedMockEngine:
    """Run a MockIntegrationEngine on its own loop in a daemon thread."""

    def __init__(self, **kwargs):
        self.engine = MockIntegrationEngine(port=0, **kwargs)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> MockIntegrationEngine:
        ready = threading.Event()
        self._loop = asyncio.new_event_loop()

        def run() -> None:
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self.engine.start())
            ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        assert ready.wait(10), "mock engine did not start"
        return self.engine

    def __exit__(self, *exc) -> None:
        assert self._loop is not None
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(10)
        self._loop.close()


@pytest.fixture
def settings(tmp_path):
    return DowntimeSettings(
        provider="fake",
        templates_dir=TEMPLATE_DIR,
        transcripts_dir=TRANSCRIPT_DIR,
        db_path=tmp_path / "downtime.db",
        engine_host="127.0.0.1",
        engine_port=1,  # replaced by the mock engine's port where needed
    )


@pytest.fixture
def client(settings):
    # Strict offline mode is pinned off here so a developer's .env cannot turn
    # every API test into a startup refusal.
    with TestClient(create_app(settings, offline=OfflineSettings(strict=False))) as c:
        yield c


def transcript(name: str = "ed-cap-admission") -> str:
    return (TRANSCRIPT_DIR / f"{name}.txt").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# reference endpoints
# --------------------------------------------------------------------------


def test_index_serves_the_capture_page(client):
    body = client.get("/").text
    assert "EHR OFFLINE" in body
    assert "Submit batch to EHR" in body
    assert "Recovery worklist" in body
    # the toggle goes through the server, and failures surface in a banner
    assert "/api/ehr-status" in body
    assert "errBar" in body and "showError" in body
    # checkbox and field state are keyed by id, never by render position
    assert "data-order=\"${esc(spec.order_id)}\"" in body
    assert "function orderById(" in body
    assert "transcript sent to hosted model" in body
    # three provenance chips, plus the locality badge
    assert 'f.source === "transcript"' in body
    assert 'f.source === "default"' in body
    assert "template default" in body and "not in transcript" in body
    assert "LOCAL ONLY" in body and 'id="offline"' in body
    # the guideline the template's defaults came from, under the template name
    assert "Guideline:" in body and "template.guideline" in body


def test_status(client):
    data = client.get("/api/status").json()
    assert data["provider"] == "fake"
    assert data["model"] == "fake:keyword"
    assert data["templates"] == 6
    assert data["counts"] == {}
    assert data["ehr_online"] is False          # the EHR is down; that is why we are here
    assert data["allow_phi_to_model"] is False
    # The locality badge reads these three and nothing else.
    assert set(data["offline"]) == {"strict", "all_local", "not_local"}
    assert data["offline"]["strict"] is False


def test_ehr_status_is_server_side_and_gates_submit_batch(client):
    assert client.get("/api/ehr-status").json() == {"ehr_online": False}

    row = _generate_and_save(client)
    client.post(f"/api/orders/{row['id']}/sign", json={})
    blocked = client.post("/api/submit-batch", json={})
    assert blocked.status_code == 409
    assert "OFFLINE" in blocked.json()["detail"]
    assert client.get(f"/api/orders/{row['id']}").json()["status"] == "queued"

    assert client.post("/api/ehr-status", json={"online": True}).json() == {"ehr_online": True}
    assert client.get("/api/status").json()["ehr_online"] is True
    # now it is allowed through (no engine listening, so it re-queues rather than 409s)
    assert client.post("/api/submit-batch", json={}).status_code == 200

    assert client.post("/api/ehr-status", json={"online": False}).json() == {"ehr_online": False}
    assert client.post("/api/submit-batch", json={}).status_code == 409


def test_templates_endpoints(client):
    summaries = client.get("/api/templates").json()
    assert len(summaries) == 6
    full = client.get("/api/templates/dka-management").json()
    assert full["name"].startswith("Diabetic Ketoacidosis")
    assert client.get("/api/templates/nope").status_code == 404


def test_transcripts_endpoint(client):
    samples = client.get("/api/transcripts").json()
    assert {s["name"] for s in samples} == {
        "ambulatory-new-t2dm", "chf-exacerbation-admission", "dka-management",
        "ed-cap-admission", "ed-chest-pain-acs", "sepsis-bundle",
    }
    assert "Ambient capture" in samples[0]["text"]


# --------------------------------------------------------------------------
# generate
# --------------------------------------------------------------------------


def test_generate_returns_selection_filled_and_template(client):
    data = client.post("/api/generate", json={"transcript": transcript()}).json()
    assert data["selection"]["template_id"] == "ed-cap-admission"
    assert data["template"]["template_id"] == "ed-cap-admission"
    assert len(data["filled"]["orders"]) == len(data["template"]["orders"])
    filled = [f for f in data["filled"]["patient_fields"] if f["value"]]
    assert len(filled) >= 5
    assert any(f["evidence"] for f in filled)


def test_generate_carries_field_provenance_and_the_guideline(client):
    data = client.post("/api/generate", json={"transcript": transcript()}).json()
    # The UI renders one of three chips per field, so every field must say which.
    every = data["filled"]["patient_fields"] + [
        f for o in data["filled"]["orders"] for f in o["fields"]]
    assert every
    assert {f["source"] for f in every} <= {"transcript", "default", "none"}
    for f in every:
        if f["source"] == "transcript":
            assert f["evidence"]
        if f["source"] == "default":
            assert f["value"] and not f["evidence"]
    # Defaults do get applied somewhere in a real fill.
    assert any(f["source"] == "default" for f in every)
    # Guideline provenance travels with the template for the header + chip titles.
    g = data["template"]["guideline"]
    assert g["organization"] == "IDSA/ATS" and g["year"] == 2019
    assert any(o.get("guideline_note") for o in data["template"]["orders"])


def test_generate_with_a_pinned_template(client):
    data = client.post("/api/generate", json={"transcript": transcript(),
                                              "template_id": "sepsis-bundle"}).json()
    assert data["filled"]["template_id"] == "sepsis-bundle"


def test_generate_rejects_empty_and_unknown(client):
    assert client.post("/api/generate", json={"transcript": "   "}).status_code == 400
    r = client.post("/api/generate", json={"transcript": "x", "template_id": "nope"})
    assert r.status_code == 400


# --------------------------------------------------------------------------
# capture -> sign -> submit
# --------------------------------------------------------------------------


def _generate_and_save(client, name="ed-cap-admission") -> dict:
    gen = client.post("/api/generate", json={"transcript": transcript(name)}).json()
    row = client.post("/api/orders", json={
        "template_id": gen["filled"]["template_id"],
        "patient_fields": gen["filled"]["patient_fields"],
        "orders": gen["filled"]["orders"],
        "transcript": transcript(name),
    }).json()
    return row


def test_save_update_and_sign(client):
    row = _generate_and_save(client)
    assert row["status"] == "draft"

    edited = list(row["patient_fields"])
    edited[0]["value"] = "Edited Name"
    updated = client.post("/api/orders", json={
        "template_id": row["template_id"], "patient_fields": edited,
        "orders": row["orders"], "transcript": "t", "order_id": row["id"],
    }).json()
    assert updated["patient_fields"][0]["value"] == "Edited Name"

    signed = client.post(f"/api/orders/{row['id']}/sign",
                         json={"signed_by": "Dr. Alvarez"}).json()
    assert signed["status"] == "queued" and signed["signed_by"] == "Dr. Alvarez"

    # A signed order is no longer a draft.
    conflict = client.post("/api/orders", json={
        "template_id": row["template_id"], "patient_fields": [], "orders": [],
        "order_id": row["id"],
    })
    assert conflict.status_code == 409
    assert client.post("/api/orders/does-not-exist/sign", json={}).status_code == 404


def test_hl7_preview(client):
    row = _generate_and_save(client)
    text = client.get(f"/api/orders/{row['id']}/hl7").text
    assert text.startswith("MSH|^~\\&|EPICVIBE|DOWNTIME|EPIC|BRIDGES|")
    assert "ORM^O01^ORM_O01" in text
    assert "PID|1||DT-" in text
    assert "ORC|NW|" in text and "OBR|1|" in text and "RXO|" in text
    assert client.get("/api/orders/nope/hl7").status_code == 404


def test_full_flow_generate_sign_submit_batch(client, settings):
    row = _generate_and_save(client)
    client.post(f"/api/orders/{row['id']}/sign", json={"signed_by": "Dr. Alvarez"})
    client.post("/api/ehr-status", json={"online": True})

    with ThreadedMockEngine(inbox=None) as engine:
        settings.engine_port = engine.actual_port
        result = client.post("/api/submit-batch", json={"limit": 10}).json()
        assert result["attempted"] == 1
        assert result["tally"] == {"acked": 1}
        assert engine.received[0]["orc_count"] >= 5

    final = client.get(f"/api/orders/{row['id']}").json()
    assert final["status"] == "acked" and final["ack_code"] == "AA"

    log = client.get(f"/api/orders/{row['id']}/log").json()
    assert [e["direction"] for e in log] == ["out", "in"]
    assert "MSA|AA" in log[1]["message"]

    assert client.get("/api/status").json()["counts"] == {"acked": 1}
    assert client.get("/api/recovery").json() == []


def test_submit_batch_nack_populates_the_recovery_worklist(client, settings):
    row = _generate_and_save(client, "chf-exacerbation-admission")
    client.post(f"/api/orders/{row['id']}/sign", json={})
    client.post("/api/ehr-status", json={"online": True})

    with ThreadedMockEngine(inbox=None, reject_every=1) as engine:
        settings.engine_port = engine.actual_port
        result = client.post("/api/submit-batch", json={}).json()
    assert result["tally"] == {"nacked": 1}

    worklist = client.get("/api/recovery").json()
    assert len(worklist) == 1
    entry = worklist[0]
    assert entry["status"] == "nacked"
    assert entry["ack_code"] == "AE"
    assert entry["patient"]["patient_name"] == "Walter Brzezinski"
    assert entry["template"].startswith("Heart Failure")
    assert any("Furosemide" in o["display"] for o in entry["orders"])
    assert all(o["display"] for o in entry["orders"])


def test_submit_batch_with_no_engine_requeues(client, settings):
    row = _generate_and_save(client)
    client.post(f"/api/orders/{row['id']}/sign", json={})
    client.post("/api/ehr-status", json={"online": True})
    result = client.post("/api/submit-batch", json={}).json()
    assert result["tally"] == {"queued": 1}
    assert client.get(f"/api/orders/{row['id']}").json()["status"] == "queued"


def test_reconcile_sets_the_real_mrn(client, settings):
    row = _generate_and_save(client)
    client.post(f"/api/orders/{row['id']}/sign", json={})
    client.post("/api/ehr-status", json={"online": True})
    with ThreadedMockEngine(inbox=None) as engine:
        settings.engine_port = engine.actual_port
        client.post("/api/submit-batch", json={})
    final = client.post(f"/api/orders/{row['id']}/reconcile", json={"mrn": "E9988"}).json()
    assert final["status"] == "reconciled"
    hl7_text = client.get(f"/api/orders/{row['id']}/hl7").text
    assert "PID|1||E9988^^^BRIDGES^MR" in hl7_text


def test_list_orders_filters_by_status(client):
    a = _generate_and_save(client)
    _generate_and_save(client, "dka-management")
    client.post(f"/api/orders/{a['id']}/sign", json={})
    assert len(client.get("/api/orders").json()) == 2
    queued = client.get("/api/orders", params={"status": "queued"}).json()
    assert [r["id"] for r in queued] == [a["id"]]


def test_save_draft_rejects_unknown_template(client):
    r = client.post("/api/orders", json={"template_id": "nope", "patient_fields": [],
                                         "orders": []})
    assert r.status_code == 400


# --------------------------------------------------------------------------
# warm-up
# --------------------------------------------------------------------------


class StubTranscriber:
    """A Transcriber-shaped double that never builds a real Whisper model.

    Loading `medium` costs hundreds of MB and seconds even when the weights are
    cached, which is exactly the cost `/api/warmup` exists to pay *once, before
    the room* - and exactly the cost a unit test must not pay at all.
    """

    def __init__(self, *, enabled: bool = True, installed: bool = True,
                 fail: Exception | None = None) -> None:
        self.settings = TranscribeSettings(model="stub-whisper", enabled=enabled)
        self.loads = 0
        self.loaded = False
        self._installed = installed
        self._fail = fail

    @property
    def enabled(self) -> bool:
        return bool(self.settings.enabled)

    def available(self) -> bool:
        return self.enabled and self._installed

    def load(self):
        if not self.enabled:
            raise TranscriberUnavailable("local transcription is disabled")
        if not self._installed:
            raise TranscriberUnavailable(INSTALL_HINT)
        if self._fail is not None:
            raise self._fail
        if not self.loaded:      # the real one is idempotent; so is this
            self.loads += 1
            self.loaded = True
        return object()

    def status(self) -> dict:
        return {"enabled": self.enabled, "installed": self._installed,
                "model": self.settings.model, "model_dir": None,
                "model_cached": True, "language": "en", "loaded": self.loaded,
                "install_hint": INSTALL_HINT}


def warm_client(settings, transcriber):
    return TestClient(create_app(settings, transcriber=transcriber,
                                 offline=OfflineSettings(strict=False)))


def test_warmup_loads_both_models_and_is_idempotent(settings):
    stub = StubTranscriber()
    with warm_client(settings, stub) as c:
        body = c.post("/api/warmup").json()

        assert body["whisper"]["loaded"] is True
        assert body["whisper"]["model"] == "stub-whisper"
        assert body["whisper"]["elapsed_s"] >= 0
        # The keyword provider has no weights, so it warms trivially - but it
        # still answers the protocol, which is what the script prints.
        assert body["provider"]["provider"] == "fake:keyword"
        assert body["provider"]["loaded"] is True
        # The same checklist the start script prints, so one call proves the
        # box is both warm and local.
        assert set(body["offline"]) >= {"all_local", "not_local", "provider_local"}
        assert stub.loads == 1

        # Safe to call again while the app is up: a second warm-up must not
        # rebuild the model or fail.
        assert c.post("/api/warmup").json()["whisper"]["loaded"] is True
        assert stub.loads == 1


def test_warmup_skips_whisper_gracefully_when_it_is_disabled(settings):
    stub = StubTranscriber(enabled=False)
    with warm_client(settings, stub) as c:
        body = c.post("/api/warmup").json()
    # Degraded, not broken: extraction still warms, and the reason is on screen.
    assert body["whisper"] == {"loaded": False, "model": "stub-whisper",
                               "reason": body["whisper"]["reason"]}
    assert "disabled" in body["whisper"]["reason"]
    assert body["provider"]["loaded"] is True


def test_warmup_skips_whisper_when_faster_whisper_is_missing(settings):
    with warm_client(settings, StubTranscriber(installed=False)) as c:
        body = c.post("/api/warmup").json()
    assert body["whisper"]["loaded"] is False
    assert body["whisper"]["reason"] == INSTALL_HINT


def test_warmup_reports_a_load_failure_instead_of_a_500(settings):
    stub = StubTranscriber(fail=RuntimeError("no space left on device"))
    with warm_client(settings, stub) as c:
        response = c.post("/api/warmup")
    assert response.status_code == 200
    assert response.json()["whisper"]["loaded"] is False
    assert "no space left on device" in response.json()["whisper"]["reason"]


def test_get_warmup_reports_current_state_without_loading_anything(settings):
    stub = StubTranscriber()
    with warm_client(settings, stub) as c:
        before = c.get("/api/warmup").json()
        assert before["whisper"]["loaded"] is False
        assert before["whisper"]["model"] == "stub-whisper"
        # Nothing to keep resident for the keyword provider, so it reads loaded.
        assert before["provider"] == {"provider": "fake:keyword", "loaded": True}
        assert stub.loads == 0      # a status read must not load a model

        c.post("/api/warmup")
        assert c.get("/api/warmup").json()["whisper"]["loaded"] is True
