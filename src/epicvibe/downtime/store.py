"""Durable store for captured downtime orders.

These rows are clinical records in flight: the EHR is down, so this sqlite file
is the only place the order exists between signature and successful ACK from
the integration engine. Writes are serialized behind a lock, the same
discipline `audit/store.py` uses.

Lifecycle: draft -> signed -> queued -> sending -> acked | nacked | failed,
with `reconciled` set once a human has matched the downtime patient identity to
the real Epic patient. `failed` returns to `queued` on retry.
"""

from __future__ import annotations  # `list` is a method name below; keep annotations lazy

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

Status = Literal[
    "draft", "signed", "queued", "sending", "sent",
    "acked", "nacked", "failed", "reconciled",
]

TERMINAL_OK = {"acked", "reconciled"}
NEEDS_RECOVERY = {"nacked", "failed"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS downtime_orders (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  status TEXT NOT NULL,
  template_id TEXT NOT NULL,
  patient_fields TEXT NOT NULL,
  orders TEXT NOT NULL,
  transcript TEXT NOT NULL DEFAULT '',
  transcript_hash TEXT NOT NULL DEFAULT '',
  signed_by TEXT,
  signed_at TEXT,
  hl7_control_id TEXT,
  ack_code TEXT,
  ack_text TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT
);
CREATE TABLE IF NOT EXISTS hl7_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id TEXT NOT NULL,
  direction TEXT NOT NULL,
  control_id TEXT,
  message TEXT NOT NULL,
  ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_downtime_orders_status ON downtime_orders(status);
CREATE INDEX IF NOT EXISTS idx_hl7_log_order ON hl7_log(order_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def transcript_hash(transcript: str) -> str:
    return hashlib.sha256(transcript.encode("utf-8")).hexdigest()


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["patient_fields"] = json.loads(d["patient_fields"])
    d["orders"] = json.loads(d["orders"])
    return d


class DowntimeStore:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- capture ------------------------------------------------------------

    def create_draft(
        self,
        template_id: str,
        patient_fields: list[dict] | dict,
        orders: list[dict],
        transcript: str = "",
    ) -> str:
        order_id = str(uuid.uuid4())
        ts = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO downtime_orders (id, created_at, updated_at, status, template_id,"
                " patient_fields, orders, transcript, transcript_hash)"
                " VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, ?)",
                (order_id, ts, ts, template_id, json.dumps(patient_fields),
                 json.dumps(orders), transcript, transcript_hash(transcript)),
            )
            self._conn.commit()
        return order_id

    def update_draft(
        self,
        order_id: str,
        *,
        patient_fields: list[dict] | dict | None = None,
        orders: list[dict] | None = None,
        template_id: str | None = None,
        transcript: str | None = None,
    ) -> dict:
        current = self.get(order_id)
        if current is None:
            raise KeyError(order_id)
        if current["status"] != "draft":
            raise ValueError(f"order {order_id} is {current['status']}, only drafts are editable")
        sets, params = ["updated_at = ?"], [_now()]
        if patient_fields is not None:
            sets.append("patient_fields = ?")
            params.append(json.dumps(patient_fields))
        if orders is not None:
            sets.append("orders = ?")
            params.append(json.dumps(orders))
        if template_id is not None:
            sets.append("template_id = ?")
            params.append(template_id)
        if transcript is not None:
            sets += ["transcript = ?", "transcript_hash = ?"]
            params += [transcript, transcript_hash(transcript)]
        params.append(order_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE downtime_orders SET {', '.join(sets)} WHERE id = ?", params
            )
            self._conn.commit()
        return self.get(order_id)  # type: ignore[return-value]

    def sign(self, order_id: str, signed_by: str) -> dict:
        """Sign a draft. Signing is what makes the order real and queues it."""
        current = self.get(order_id)
        if current is None:
            raise KeyError(order_id)
        if current["status"] not in ("draft", "signed"):
            raise ValueError(f"order {order_id} is {current['status']} and cannot be signed")
        ts = _now()
        with self._lock:
            self._conn.execute(
                "UPDATE downtime_orders SET status = 'queued', signed_by = ?, signed_at = ?,"
                " updated_at = ? WHERE id = ?",
                (signed_by, ts, ts, order_id),
            )
            self._conn.commit()
        return self.get(order_id)  # type: ignore[return-value]

    # -- reads --------------------------------------------------------------

    def get(self, order_id: str) -> dict | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM downtime_orders WHERE id = ?", (order_id,)
            ).fetchone()
        return _row(r) if r else None

    def list(self, status: str | list[str] | None = None, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM downtime_orders"
        params: list[Any] = []
        if isinstance(status, str):
            sql += " WHERE status = ?"
            params.append(status)
        elif status:
            sql += f" WHERE status IN ({','.join('?' * len(status))})"
            params += list(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row(r) for r in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM downtime_orders GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def recovery_worklist(self) -> list[dict]:
        """Orders that did not make it in - the day-one paper-free fallback."""
        return self.list(status=sorted(NEEDS_RECOVERY))

    # -- transmission -------------------------------------------------------

    def export_batch(self, limit: int = 25) -> list[dict]:
        """The next `limit` queued orders, oldest first - the submit-batch unit."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM downtime_orders WHERE status = 'queued'"
                " ORDER BY created_at ASC LIMIT ?", (limit,)
            ).fetchall()
        return [_row(r) for r in rows]

    def recover_stuck(self, max_age_seconds: float = 300) -> list[str]:
        """Return `sending` rows older than `max_age_seconds` to `queued`.

        A process killed between `mark_sending` and `mark_result` leaves an order
        stranded in `sending`, where no batch will ever pick it up again - the one
        state a clinical record in flight must not get stuck in.
        """
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(seconds=max_age_seconds)).isoformat()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM downtime_orders WHERE status = 'sending'"
                " AND updated_at < ?", (cutoff,)
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                self._conn.execute(
                    "UPDATE downtime_orders SET status = 'queued', updated_at = ?,"
                    " last_error = 'recovered from a stuck sending state'"
                    f" WHERE id IN ({','.join('?' * len(ids))})",
                    [_now(), *ids],
                )
                self._conn.commit()
        return ids

    def mark_queued(self, order_id: str) -> None:
        self._set_status(order_id, "queued")

    def mark_sending(self, order_id: str, control_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE downtime_orders SET status = 'sending', hl7_control_id = ?,"
                " attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (control_id, _now(), order_id),
            )
            self._conn.commit()

    def mark_result(
        self,
        order_id: str,
        status: Status,
        *,
        ack_code: str | None = None,
        ack_text: str | None = None,
        last_error: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE downtime_orders SET status = ?, ack_code = ?, ack_text = ?,"
                " last_error = ?, updated_at = ? WHERE id = ?",
                (status, ack_code, ack_text, last_error, _now(), order_id),
            )
            self._conn.commit()

    def mark_reconciled(self, order_id: str, mrn: str | None = None) -> dict:
        current = self.get(order_id)
        if current is None:
            raise KeyError(order_id)
        if mrn:
            fields = current["patient_fields"]
            if isinstance(fields, list):
                for f in fields:
                    if f.get("field_id") == "mrn":
                        f["value"] = mrn
                        break
                else:
                    fields.append({"field_id": "mrn", "value": mrn,
                                   "evidence": None, "confidence": "high"})
            elif isinstance(fields, dict):
                fields["mrn"] = mrn
            with self._lock:
                self._conn.execute(
                    "UPDATE downtime_orders SET patient_fields = ? WHERE id = ?",
                    (json.dumps(fields), order_id),
                )
                self._conn.commit()
        self._set_status(order_id, "reconciled")
        return self.get(order_id)  # type: ignore[return-value]

    def _set_status(self, order_id: str, status: Status) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE downtime_orders SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), order_id),
            )
            self._conn.commit()

    # -- HL7 log ------------------------------------------------------------

    def log_hl7(
        self, order_id: str, direction: Literal["out", "in"], message: str,
        control_id: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO hl7_log (order_id, direction, control_id, message, ts)"
                " VALUES (?, ?, ?, ?, ?)",
                (order_id, direction, control_id, message, _now()),
            )
            self._conn.commit()

    def hl7_log(self, order_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM hl7_log WHERE order_id = ? ORDER BY id ASC", (order_id,)
            ).fetchall()
        return [dict(r) for r in rows]
