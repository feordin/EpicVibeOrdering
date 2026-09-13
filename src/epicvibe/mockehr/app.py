"""App factory for the mock EHR."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI

from epicvibe.mockehr.api import router as api_router
from epicvibe.mockehr.fhir_api import router as fhir_router
from epicvibe.mockehr.hooks_client import HooksClient
from epicvibe.mockehr.oauth import OAuthStub
from epicvibe.mockehr.oauth import router as oauth_router
from epicvibe.mockehr.settings import MockEhrSettings
from epicvibe.mockehr.store import FhirStore
from epicvibe.mockehr.ui import router as ui_router

log = logging.getLogger("epicvibe.mockehr")


def create_app(settings: MockEhrSettings | None = None, *,
               cds_client: httpx.AsyncClient | None = None) -> FastAPI:
    """Build the mock EHR app.

    ``cds_client`` lets tests wire an in-process CDS service via
    ``httpx.ASGITransport`` instead of talking to localhost:8000.
    """
    settings = settings or MockEhrSettings.from_env()
    store = FhirStore(settings.fixtures_dir)
    if not store.all_of("Patient"):
        raise RuntimeError(
            f"mock EHR loaded 0 Patient resources from {Path(settings.fixtures_dir).resolve()} "
            "-- set MOCKEHR_FIXTURES_DIR to the fixtures/mockehr/patients directory")
    oauth = OAuthStub()
    hooks = HooksClient(settings, store, oauth, client=cds_client)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await hooks.refresh()
        yield

    app = FastAPI(title="EpicVibe Mock EHR", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.oauth = oauth
    app.state.hooks = hooks
    app.include_router(ui_router)
    app.include_router(api_router)
    app.include_router(fhir_router)
    app.include_router(oauth_router)
    return app
