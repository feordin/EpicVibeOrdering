from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from epicvibe.audit.store import AuditStore
from epicvibe.cache import ProposalCache
from epicvibe.catalog.index import CatalogIndex
from epicvibe.catalog.loader import load_catalog
from epicvibe.cds.hooks import discovery_router, router
from epicvibe.config import Settings
from epicvibe.inference.base import InferenceProvider
from epicvibe.inference.factory import make_provider
from epicvibe.jobs import JobRunner
from epicvibe.proposal.engine import ProposalEngine
from epicvibe.smart.routes import router as smart_router
from epicvibe.smart.session import SessionStore


def create_app(settings: Settings | None = None, *,
               provider: InferenceProvider | None = None) -> FastAPI:
    settings = settings or Settings()
    index = CatalogIndex(load_catalog(settings.catalog_path))
    app = FastAPI(title="EpicVibe Ordering")
    app.state.settings = settings
    app.state.index = index
    app.state.engine = ProposalEngine(index, provider or make_provider(settings))
    app.state.cache = ProposalCache(settings.cache_ttl_seconds)
    app.state.runner = JobRunner()
    app.state.audit = AuditStore(settings.audit_db_path)
    app.state.smart_sessions = SessionStore()
    app.state.http_client = None          # lazily created; tests inject a fake EHR client
    if settings.cors_allow_origins:
        # Browser CDS Hooks clients (reference sandbox, SMART launcher, mock EHR)
        # call discovery + /cds-services/{id} + /cds-services/{id}/feedback
        # cross-origin with an Authorization header.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_allow_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Accept"],
            expose_headers=["Content-Type", "Location", "ETag"],
            max_age=600,
        )
    app.include_router(discovery_router)
    app.include_router(router)
    app.include_router(smart_router)
    return app
