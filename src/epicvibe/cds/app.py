from fastapi import FastAPI

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
    app.include_router(discovery_router)
    app.include_router(router)
    return app
