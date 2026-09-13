"""``python -m epicvibe.mockehr`` -- run the mock EHR."""
from __future__ import annotations

import logging

import uvicorn

from epicvibe.mockehr.app import create_app
from epicvibe.mockehr.settings import MockEhrSettings


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = MockEhrSettings.from_env()
    log = logging.getLogger("epicvibe.mockehr")
    log.info("mock EHR on %s (CDS service: %s)", settings.base_url, settings.cds_base_url)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port,
                log_level="info")


if __name__ == "__main__":
    main()
