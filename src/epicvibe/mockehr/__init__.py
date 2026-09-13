"""A runnable mock EHR: in-memory FHIR R4 store, SMART launch stub, CDS Hooks client
and a chart UI, so the EpicVibe CDS service can be demoed end-to-end on a laptop.
"""
from epicvibe.mockehr.app import create_app
from epicvibe.mockehr.settings import MockEhrSettings

__all__ = ["create_app", "MockEhrSettings"]
