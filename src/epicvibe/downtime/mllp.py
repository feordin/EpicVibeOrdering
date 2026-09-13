"""MLLP client and the batch submitter that drains the downtime queue.

MLLP (Minimal Lower Layer Protocol) is how HL7v2 travels to an integration
engine: <VT> message <FS><CR>. One message, one ACK, one connection per send -
the volumes here are a downtime backlog, not a live feed, and per-message
connections make a partial failure trivially recoverable.
"""

import asyncio

from epicvibe.downtime import hl7
from epicvibe.downtime.store import DowntimeStore

VT = b"\x0b"
FS = b"\x1c"
CR = b"\x0d"

DEFAULT_TIMEOUT = 10.0

#: asyncio's default StreamReader limit is 64 KiB, which an ORM carrying a long
#: order set plus transcript-evidence NTEs can exceed -- `readuntil` then raises
#: LimitOverrunError and the frame is lost mid-stream.
READ_LIMIT = 1024 * 1024

#: A `sending` row older than this was orphaned by a crash or a killed process.
STUCK_AFTER_SECONDS = 300


def frame(message: str) -> bytes:
    return VT + message.encode("utf-8") + FS + CR


def unframe(data: bytes) -> str:
    return data.replace(VT, b"").replace(FS, b"").decode("utf-8", errors="replace").strip("\r\n")


async def read_frame(reader: asyncio.StreamReader) -> str | None:
    """Read one MLLP frame. Returns None on clean EOF before any payload."""
    start = await reader.readexactly(1)
    while start != VT:
        if not start:
            return None
        start = await reader.readexactly(1)
    payload = await reader.readuntil(FS)
    try:
        await reader.readexactly(1)  # trailing CR
    except asyncio.IncompleteReadError:
        pass
    return unframe(payload)


class MllpError(RuntimeError):
    """Transport-level failure - the engine is unreachable or went silent."""


async def send_message(
    message: str, host: str, port: int, timeout: float = DEFAULT_TIMEOUT
) -> tuple[str, str, str, str]:
    """Send one HL7 message over MLLP and wait for the ACK.

    Returns `(ack_code, acked_control_id, ack_text, raw_ack)`.
    Raises `MllpError` for connection/timeout failures - those are retryable,
    unlike a NACK, which is a decision the engine made about our message.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, limit=READ_LIMIT), timeout=timeout
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise MllpError(f"cannot reach integration engine at {host}:{port}: {exc}") from exc

    try:
        writer.write(frame(message))
        await writer.drain()
        try:
            raw = await asyncio.wait_for(read_frame(reader), timeout=timeout)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                asyncio.TimeoutError, OSError) as exc:
            # LimitOverrunError: an ACK bigger than the reader's buffer. Transport-level
            # either way, so it is retryable rather than a decision about our message.
            raise MllpError(f"no ACK from {host}:{port}: {exc!r}") from exc
        if raw is None:
            raise MllpError(f"connection closed by {host}:{port} before ACK")
        code, control, text = hl7.parse_ack(raw)
        return code, control, text, raw
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, asyncio.TimeoutError):
            pass


async def submit_order(
    store: DowntimeStore, order_row: dict, template, settings, timeout: float = DEFAULT_TIMEOUT
) -> dict:
    """Build, send and record one order. Never raises for expected failures."""
    order_id = order_row["id"]
    message, control_id = hl7.build_orm(order_row, template, settings)
    store.mark_sending(order_id, control_id)
    store.log_hl7(order_id, "out", message, control_id)

    try:
        code, acked_control, text, raw = await send_message(
            message, settings.engine_host, settings.engine_port, timeout=timeout
        )
    except MllpError as exc:
        # Transport failure: back to the queue, attempts already incremented.
        store.mark_result(order_id, "queued", last_error=str(exc))
        return {"order_id": order_id, "control_id": control_id, "status": "queued",
                "ack_code": None, "error": str(exc)}

    store.log_hl7(order_id, "in", raw, acked_control or control_id)
    if acked_control and acked_control != control_id:
        store.mark_result(order_id, "failed", ack_code=code, ack_text=text,
                          last_error=f"ACK control id mismatch: {acked_control} != {control_id}")
        return {"order_id": order_id, "control_id": control_id, "status": "failed",
                "ack_code": code, "error": "ACK control id mismatch"}

    if hl7.ack_is_accept(code):
        store.mark_result(order_id, "acked", ack_code=code, ack_text=text)
        status = "acked"
    elif hl7.ack_is_reject(code):
        store.mark_result(order_id, "nacked", ack_code=code, ack_text=text)
        status = "nacked"
    else:
        store.mark_result(order_id, "failed", ack_code=code, ack_text=text,
                          last_error=f"unrecognized ACK code {code!r}")
        status = "failed"
    return {"order_id": order_id, "control_id": control_id, "status": status,
            "ack_code": code, "ack_text": text, "error": None}


async def submit_batch(
    store: DowntimeStore, library, settings, limit: int = 25,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """Drain up to `limit` queued orders to the integration engine."""
    # A previous run may have died mid-send; those rows are ours to re-queue.
    store.recover_stuck(STUCK_AFTER_SECONDS)
    batch = store.export_batch(limit=limit)
    results: list[dict] = []
    for row in batch:
        template = library.get(row["template_id"])
        if template is None:
            store.mark_result(row["id"], "failed",
                              last_error=f"unknown template {row['template_id']!r}")
            results.append({"order_id": row["id"], "status": "failed",
                            "error": f"unknown template {row['template_id']!r}"})
            continue
        results.append(await submit_order(store, row, template, settings, timeout=timeout))

    tally: dict[str, int] = {}
    for r in results:
        tally[r["status"]] = tally.get(r["status"], 0) + 1
    return {"attempted": len(batch), "results": results, "tally": tally}
