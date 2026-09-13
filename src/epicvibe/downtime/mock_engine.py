"""Mock HL7v2 integration engine (Epic Bridges / Rhapsody / Mirth stand-in).

An asyncio MLLP server that accepts ORM^O01 messages, writes each raw message
to an inbox directory, logs a one-line summary, and replies with ACK^O01. It
exists so the whole downtime flow - capture, sign, queue, submit, ACK - demos
with nothing but this repo.

    python -m epicvibe.downtime.mock_engine --port 2575
    python -m epicvibe.downtime.mock_engine --reject-every 3   # exercise NACKs

Received messages are written to `--inbox` (default `./.downtime-inbox`, which
is in .gitignore). They are raw HL7 and therefore contain PHI - point `--inbox`
at a temp directory, or pass `--inbox ""` via the API to disable it entirely.
"""

import argparse
import asyncio
import logging
from pathlib import Path

from epicvibe.downtime import hl7
from epicvibe.downtime.mllp import frame, read_frame

log = logging.getLogger("downtime.mock_engine")

DEFAULT_INBOX = Path(".downtime-inbox")


class MockIntegrationEngine:
    """MLLP server that ACKs (or, every Nth message, NACKs)."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 2575,
        inbox: Path | str | None = DEFAULT_INBOX,
        reject_every: int = 0,
        sending_application: str = "EPIC",
        sending_facility: str = "BRIDGES",
    ):
        self.host = host
        self.port = port
        self.inbox = Path(inbox) if inbox else None
        self.reject_every = reject_every
        self.sending_application = sending_application
        self.sending_facility = sending_facility
        self.received: list[dict] = []
        self._server: asyncio.AbstractServer | None = None
        self._count = 0

    @property
    def actual_port(self) -> int:
        if self._server is None:
            return self.port
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> "MockIntegrationEngine":
        if self.inbox:
            self.inbox.mkdir(parents=True, exist_ok=True)
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        log.info("mock integration engine listening on %s:%s", self.host, self.actual_port)
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def __aenter__(self) -> "MockIntegrationEngine":
        return await self.start()

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    # -- connection handling -------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    message = await read_frame(reader)
                except (asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                        ConnectionResetError):
                    return
                if not message:
                    return
                ack = self._process(message)
                writer.write(frame(ack))
                await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.TimeoutError):
                pass

    def _process(self, message: str) -> str:
        self._count += 1
        summary = hl7.summarize(message)
        reject = bool(self.reject_every) and self._count % self.reject_every == 0
        summary["ack"] = "AE" if reject else "AA"
        summary["seq"] = self._count
        self.received.append({**summary, "message": message})

        if self.inbox:
            name = f"{self._count:05d}-{summary['control_id'] or 'nocontrol'}.hl7"
            (self.inbox / name).write_text(message, encoding="utf-8", newline="")

        # Never log PID-3: the patient identifier is the one field in this message
        # that identifies a human being, and engine logs are rarely PHI-controlled.
        log.info(
            "#%d %s control=%s orcs=%d -> %s",
            self._count, summary["message_type"] or "?", summary["control_id"] or "?",
            summary["orc_count"], summary["ack"],
        )
        if reject:
            return hl7.build_ack(
                summary["control_id"], "AE",
                f"Rejected by mock engine (reject-every={self.reject_every})",
                sending_application=self.sending_application,
                sending_facility=self.sending_facility,
            )
        return hl7.build_ack(
            summary["control_id"], "AA",
            f"Accepted {summary['orc_count']} order(s)",
            sending_application=self.sending_application,
            sending_facility=self.sending_facility,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m epicvibe.downtime.mock_engine",
        description="Mock HL7v2 MLLP integration engine for the downtime demo.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2575)
    parser.add_argument("--inbox", default=str(DEFAULT_INBOX),
                        help="directory for raw received messages (default: ./.downtime-inbox)")
    parser.add_argument("--reject-every", type=int, default=0, metavar="N",
                        help="NACK (AE) every Nth message; 0 disables")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    engine = MockIntegrationEngine(
        host=args.host, port=args.port, inbox=args.inbox, reject_every=args.reject_every
    )
    try:
        asyncio.run(engine.serve_forever())
    except KeyboardInterrupt:
        log.info("stopped after %d message(s)", len(engine.received))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
