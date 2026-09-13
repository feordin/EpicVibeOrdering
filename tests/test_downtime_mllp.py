"""MLLP framing, the mock integration engine, and batch submission."""

import asyncio
import socket
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from epicvibe.downtime import hl7, mllp
from epicvibe.downtime.config import DowntimeSettings
from epicvibe.downtime.mock_engine import MockIntegrationEngine, main
from epicvibe.downtime.store import DowntimeStore
from epicvibe.downtime.templates import load_templates

REPO = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = REPO / "fixtures" / "downtime" / "templates"


@pytest.fixture(scope="module")
def library():
    return load_templates(TEMPLATE_DIR)


@pytest.fixture
def settings(tmp_path):
    return DowntimeSettings(templates_dir=TEMPLATE_DIR, db_path=tmp_path / "d.db")


@pytest.fixture
def store(settings):
    s = DowntimeStore(settings.db_path)
    yield s
    s.close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def seed_order(store, template_id="ed-cap-admission") -> str:
    order_id = store.create_draft(template_id, [
        {"field_id": "patient_name", "value": "Test Patient"},
        {"field_id": "dob", "value": "1970-01-01"},
        {"field_id": "sex", "value": "female"},
        {"field_id": "allergies", "value": "penicillin"},
        {"field_id": "ordering_provider", "value": "Dr. Test"},
        {"field_id": "encounter_location", "value": "ED Bay 1"},
    ], [
        {"order_id": "cbc", "selected": True, "rationale": "seeded",
         "fields": [{"field_id": "priority", "value": "STAT"},
                    {"field_id": "indication", "value": "pneumonia"}]},
        {"order_id": "ceftriaxone", "selected": True, "rationale": "seeded",
         "fields": [{"field_id": "dose", "value": "1"},
                    {"field_id": "dose_units", "value": "g"},
                    {"field_id": "route", "value": "IV"},
                    {"field_id": "frequency", "value": "daily"},
                    {"field_id": "indication", "value": "pneumonia"}]},
    ], "transcript")
    store.sign(order_id, "Dr. Test")
    return order_id


# --------------------------------------------------------------------------
# framing
# --------------------------------------------------------------------------


def test_frame_and_unframe_roundtrip():
    message = "MSH|^~\\&|A|B|C|D|20260101000000||ORM^O01^ORM_O01|X1|T|2.5.1\rPID|1\r"
    framed = mllp.frame(message)
    assert framed.startswith(b"\x0b") and framed.endswith(b"\x1c\x0d")
    assert mllp.unframe(framed[1:-1]) == message.strip("\r")


# --------------------------------------------------------------------------
# mock engine round-trips
# --------------------------------------------------------------------------


async def test_mllp_round_trip_against_mock_engine(tmp_path):
    async with MockIntegrationEngine(port=0, inbox=tmp_path / "inbox") as engine:
        message, control_id = _minimal_orm()
        code, acked, text, raw = await mllp.send_message(
            message, "127.0.0.1", engine.actual_port, timeout=5
        )
        assert code == "AA"
        assert acked == control_id
        assert "Accepted" in text
        assert raw.startswith("MSH")
        assert len(engine.received) == 1
        assert engine.received[0]["control_id"] == control_id
        assert engine.received[0]["orc_count"] == 1
        written = list((tmp_path / "inbox").glob("*.hl7"))
        assert len(written) == 1
        assert control_id in written[0].read_text(encoding="utf-8")


async def test_mock_engine_nacks_every_nth_message(tmp_path):
    async with MockIntegrationEngine(port=0, inbox=tmp_path / "inbox",
                                     reject_every=2) as engine:
        codes = []
        for _ in range(4):
            message, _cid = _minimal_orm()
            code, _acked, _text, _raw = await mllp.send_message(
                message, "127.0.0.1", engine.actual_port, timeout=5
            )
            codes.append(code)
        assert codes == ["AA", "AE", "AA", "AE"]


async def test_send_message_raises_when_engine_is_refusing_connections():
    message, _cid = _minimal_orm()
    with pytest.raises(mllp.MllpError):
        await mllp.send_message(message, "127.0.0.1", free_port(), timeout=2)


async def _serve(handler):
    """Run `handler(reader, writer)` as a one-shot TCP server; yields the port."""
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def test_send_message_raises_when_the_server_closes_without_an_ack():
    async def close_immediately(reader, writer):
        await reader.read(100)          # swallow (part of) the frame, then hang up
        writer.close()

    server, port = await _serve(close_immediately)
    try:
        message, _cid = _minimal_orm()
        with pytest.raises(mllp.MllpError, match="no ACK|closed by"):
            await mllp.send_message(message, "127.0.0.1", port, timeout=5)
    finally:
        server.close()
        await server.wait_closed()


async def test_an_oversized_ack_is_a_transport_error_not_a_crash(monkeypatch):
    """`readuntil` raises LimitOverrunError past the buffer limit; it must map to
    MllpError so the order goes back on the queue instead of blowing up the batch."""
    async def overrun(reader):
        raise asyncio.LimitOverrunError("separator is not found and the buffer is full", 0)

    monkeypatch.setattr(mllp, "read_frame", overrun)
    async with MockIntegrationEngine(port=0, inbox=None) as engine:
        message, _cid = _minimal_orm()
        with pytest.raises(mllp.MllpError, match="no ACK"):
            await mllp.send_message(message, "127.0.0.1", engine.actual_port, timeout=5)


def test_reader_limit_is_a_megabyte():
    assert mllp.READ_LIMIT == 1024 * 1024


async def test_submit_order_fails_on_an_ack_for_a_different_control_id(
    store, library, settings
):
    """An ACK for someone else's message is not an ACK for ours."""
    async def wrong_ack(reader, writer):
        await mllp.read_frame(reader)
        writer.write(mllp.frame(hl7.build_ack("DT-SOMEONE-ELSE", "AA", "Accepted")))
        await writer.drain()

    server, port = await _serve(wrong_ack)
    order_id = seed_order(store)
    try:
        settings.engine_port = port
        result = await mllp.submit_batch(store, library, settings, timeout=5)
    finally:
        server.close()
        await server.wait_closed()

    assert result["tally"] == {"failed": 1}
    assert result["results"][0]["error"] == "ACK control id mismatch"
    row = store.get(order_id)
    assert row["status"] == "failed"
    assert "DT-SOMEONE-ELSE" in row["last_error"]
    assert row["id"] in [r["id"] for r in store.recovery_worklist()]


# --------------------------------------------------------------------------
# stuck-row recovery and concurrency
# --------------------------------------------------------------------------


def _age_row(store, order_id: str, seconds: int) -> None:
    old = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    with store._lock:
        store._conn.execute("UPDATE downtime_orders SET updated_at = ? WHERE id = ?",
                            (old, order_id))
        store._conn.commit()


def test_recover_stuck_returns_orphaned_sending_rows_to_the_queue(store):
    old_id = seed_order(store)
    fresh_id = seed_order(store)
    store.mark_sending(old_id, "DTOLD")
    store.mark_sending(fresh_id, "DTNEW")
    _age_row(store, old_id, 3600)

    assert store.recover_stuck(300) == [old_id]
    assert store.get(old_id)["status"] == "queued"
    assert "stuck sending" in store.get(old_id)["last_error"]
    assert store.get(fresh_id)["status"] == "sending"       # still in flight
    assert [r["id"] for r in store.export_batch()] == [old_id]

    assert store.recover_stuck(300) == []                   # idempotent


async def test_submit_batch_recovers_a_stuck_row_first(store, library, settings):
    order_id = seed_order(store)
    store.mark_sending(order_id, "DTSTUCK")                 # a crash mid-send
    _age_row(store, order_id, 3600)
    assert store.export_batch() == []                       # invisible to the queue

    async with MockIntegrationEngine(port=0, inbox=None) as engine:
        settings.engine_port = engine.actual_port
        result = await mllp.submit_batch(store, library, settings)
    assert result["tally"] == {"acked": 1}
    assert store.get(order_id)["status"] == "acked"


def test_store_survives_concurrent_creates_and_signs(settings):
    """Every write goes through one lock; threads must not lose or corrupt a row."""
    store = DowntimeStore(settings.db_path)
    errors: list[Exception] = []
    created: list[str] = []
    lock = threading.Lock()

    def worker(n: int) -> None:
        try:
            for i in range(10):
                order_id = store.create_draft(
                    "ed-cap-admission",
                    [{"field_id": "patient_name", "value": f"Patient {n}-{i}"}],
                    [{"order_id": "cbc", "selected": True, "fields": []}],
                    f"transcript {n}-{i}",
                )
                store.update_draft(order_id, orders=[
                    {"order_id": "cbc", "selected": True, "fields": []}])
                store.sign(order_id, f"Dr. {n}")
                with lock:
                    created.append(order_id)
        except Exception as exc:                             # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    try:
        assert errors == []
        assert len(created) == 60
        assert len(set(created)) == 60                       # no id collisions
        assert store.counts() == {"queued": 60}
        assert all(store.get(oid)["signed_by"].startswith("Dr. ") for oid in created)
    finally:
        store.close()


# --------------------------------------------------------------------------
# batch submission
# --------------------------------------------------------------------------


async def test_submit_batch_acks_and_logs(store, library, settings, tmp_path):
    order_id = seed_order(store)
    async with MockIntegrationEngine(port=0, inbox=tmp_path / "inbox") as engine:
        settings.engine_port = engine.actual_port
        result = await mllp.submit_batch(store, library, settings)

    assert result["attempted"] == 1
    assert result["tally"] == {"acked": 1}
    row = store.get(order_id)
    assert row["status"] == "acked"
    assert row["ack_code"] == "AA"
    assert row["attempts"] == 1
    assert row["hl7_control_id"]

    log = store.hl7_log(order_id)
    assert [e["direction"] for e in log] == ["out", "in"]
    assert log[0]["message"].startswith("MSH")
    assert "ORC|NW" in log[0]["message"]
    assert "MSA|AA" in log[1]["message"]
    # Nothing is left queued.
    assert store.export_batch() == []


async def test_submit_batch_records_nack_and_builds_recovery_worklist(
    store, library, settings, tmp_path
):
    order_id = seed_order(store)
    async with MockIntegrationEngine(port=0, inbox=tmp_path / "inbox",
                                     reject_every=1) as engine:
        settings.engine_port = engine.actual_port
        result = await mllp.submit_batch(store, library, settings)

    assert result["tally"] == {"nacked": 1}
    row = store.get(order_id)
    assert row["status"] == "nacked"
    assert row["ack_code"] == "AE"
    assert "Rejected" in row["ack_text"]
    assert [r["id"] for r in store.recovery_worklist()] == [order_id]


async def test_submit_batch_requeues_on_connection_refused(store, library, settings):
    order_id = seed_order(store)
    settings.engine_port = free_port()          # nothing is listening
    result = await mllp.submit_batch(store, library, settings, timeout=2)

    assert result["tally"] == {"queued": 1}
    row = store.get(order_id)
    assert row["status"] == "queued"            # retryable, so back in the queue
    assert row["attempts"] == 1
    assert "cannot reach integration engine" in row["last_error"]
    assert [r["id"] for r in store.export_batch()] == [order_id]

    # A retry against a live engine succeeds and increments attempts.
    async with MockIntegrationEngine(port=0, inbox=None) as engine:
        settings.engine_port = engine.actual_port
        retry = await mllp.submit_batch(store, library, settings)
    assert retry["tally"] == {"acked": 1}
    assert store.get(order_id)["attempts"] == 2


async def test_submit_batch_fails_an_order_whose_template_vanished(store, library, settings):
    order_id = store.create_draft("retired-template", [], [])
    store.sign(order_id, "x")
    result = await mllp.submit_batch(store, library, settings)
    assert result["tally"] == {"failed": 1}
    assert "unknown template" in store.get(order_id)["last_error"]


async def test_submit_batch_respects_the_limit(store, library, settings, tmp_path):
    for _ in range(3):
        seed_order(store)
    async with MockIntegrationEngine(port=0, inbox=None) as engine:
        settings.engine_port = engine.actual_port
        result = await mllp.submit_batch(store, library, settings, limit=2)
    assert result["attempted"] == 2
    assert len(store.export_batch()) == 1


async def test_mock_engine_handles_several_messages_on_one_connection(tmp_path):
    async with MockIntegrationEngine(port=0, inbox=None) as engine:
        reader, writer = await asyncio.open_connection("127.0.0.1", engine.actual_port)
        control_ids = []
        for _ in range(3):
            message, cid = _minimal_orm()
            control_ids.append(cid)
            writer.write(mllp.frame(message))
            await writer.drain()
            ack = await mllp.read_frame(reader)
            assert hl7.parse_ack(ack)[1] == cid
        writer.close()
        await writer.wait_closed()
        assert [r["control_id"] for r in engine.received] == control_ids


def test_mock_engine_cli_parses_arguments(monkeypatch):
    started: dict = {}

    def fake_run(coro):
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr("epicvibe.downtime.mock_engine.asyncio.run", fake_run)
    original_init = MockIntegrationEngine.__init__

    def spy(self, **kwargs):
        started.update(kwargs)
        original_init(self, **kwargs)

    monkeypatch.setattr(MockIntegrationEngine, "__init__", spy)
    assert main(["--port", "9999", "--reject-every", "4", "--inbox", "x"]) == 0
    assert started["port"] == 9999 and started["reject_every"] == 4 and started["inbox"] == "x"


# --------------------------------------------------------------------------


class _Settings:
    sending_application = "EPICVIBE"
    sending_facility = "DOWNTIME"
    receiving_application = "EPIC"
    receiving_facility = "BRIDGES"


class _Template:
    setting = "ED"

    class _Order:
        order_id = "cbc"
        category = "lab"
        display = "CBC with differential"
        code = "57021-8"
        code_system = "http://loinc.org"

    def order(self, order_id):
        return self._Order() if order_id == "cbc" else None


def _minimal_orm() -> tuple[str, str]:
    row = {
        "patient_fields": [{"field_id": "patient_name", "value": "Test Patient"},
                           {"field_id": "sex", "value": "female"}],
        "orders": [{"order_id": "cbc", "selected": True, "rationale": "test",
                    "fields": [{"field_id": "indication", "value": "test"}]}],
    }
    return hl7.build_orm(row, _Template(), _Settings())
