"""Run the downtime capture UI: `python -m epicvibe.downtime`."""

import os

import uvicorn

from epicvibe.downtime.app import create_app
from epicvibe.downtime.config import DowntimeSettings


def main() -> None:
    settings = DowntimeSettings()
    host = os.environ.get("EPICVIBE_DOWNTIME_HOST", "127.0.0.1")
    port = int(os.environ.get("EPICVIBE_DOWNTIME_PORT", "8200"))
    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
