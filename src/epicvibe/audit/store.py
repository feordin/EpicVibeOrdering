import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from epicvibe.proposal.validation import ValidatedProposal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY, encounter_key TEXT NOT NULL, created_at TEXT NOT NULL,
  model TEXT NOT NULL, proposal_json TEXT NOT NULL, violations_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY, service_id TEXT NOT NULL, card_uuid TEXT,
  outcome TEXT, received_at TEXT NOT NULL, raw_json TEXT NOT NULL);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditStore:
    def __init__(self, path: Path | str):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def record_proposal(self, encounter_key: str, vp: ValidatedProposal, model: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO proposals (encounter_key, created_at, model, proposal_json, violations_json)"
                " VALUES (?, ?, ?, ?, ?)",
                (encounter_key, _now(), model, vp.proposal.model_dump_json(),
                 json.dumps([v.model_dump() for v in vp.violations])))
            self._conn.commit()
            return cur.lastrowid

    def record_feedback(self, service_id: str, payload: dict) -> int:
        with self._lock:
            items = payload.get("feedback", [])
            for f in items:
                self._conn.execute(
                    "INSERT INTO feedback (service_id, card_uuid, outcome, received_at, raw_json)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (service_id, f.get("card"), f.get("outcome"), _now(), json.dumps(f)))
            self._conn.commit()
            return len(items)

    def proposals(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM proposals")]

    def feedback(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM feedback")]
