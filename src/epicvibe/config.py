from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EPICVIBE_", env_file=".env")

    catalog_path: Path = Path("fixtures/catalog/sample_catalog.json")
    inference_provider: Literal["fake", "demo", "anthropic"] = "fake"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5"
    anthropic_base_url: str = "https://api.anthropic.com"
    verify_jwt: bool = False
    jwks_url: str = ""
    jwt_audience: str = ""
    audit_db_path: Path = Path("audit.db")
    cache_ttl_seconds: int = 3600
    # --- FHIR pull (hook background job) -------------------------------------
    fhir_pull_enabled: bool = True
    fhir_timeout_seconds: float = 1.5
    # --- SMART on FHIR app ---------------------------------------------------
    smart_launch_url: str = "http://localhost:8000/smart/launch"
    smart_redirect_uri: str = "http://localhost:8000/smart/callback"
    smart_client_id: str = "epicvibe-order-assistant"
    smart_scope: str = ("launch openid fhirUser patient/*.read "
                        "patient/ServiceRequest.write patient/MedicationRequest.write")
    smart_dev_mode: bool = True
    smart_dev_iss: str = "http://localhost:8100/fhir"
    # Issuers (FHIR base URLs) this app is willing to launch against.  Anything
    # else is rejected: `iss` is attacker-controllable and drives both an
    # outbound discovery fetch (SSRF) and a browser redirect (open redirect).
    # Set via JSON list, e.g. EPICVIBE_SMART_ALLOWED_ISSUERS='["https://ehr/fhir"]'
    smart_allowed_issuers: list[str] = [
        "http://localhost:8100/fhir",
        "http://127.0.0.1:8100/fhir",
    ]
    # --- SMART -> CDS hand-back ---------------------------------------------
    # Inside Epic a SMART app cannot file orders: ServiceRequest/MedicationRequest
    # create is refused outside a CDS Hooks interaction and there is no SMART Web
    # Messaging.  The only write channel is an accepted CDS card `create`
    # suggestion.  So the default is "handback": the SMART app stores the
    # clinician's refined selections, and the next order-select / order-sign hook
    # for that encounter emits them as create actions.  "fhir" keeps the direct
    # POST behaviour, which works against the mock EHR / HAPI.
    smart_submit_mode: Literal["handback", "fhir"] = "handback"
    smart_handback_ttl_seconds: int = 3600
    # --- CORS ----------------------------------------------------------------
    # Browser-based CDS Hooks clients (the reference CDS Hooks Sandbox, the SMART
    # App Launcher, our own mock EHR) POST to /cds-services/* from their own
    # origin, so the service needs CORS.  Kept to an explicit allow-list rather
    # than "*" because requests carry an Authorization bearer token.
    # Override with EPICVIBE_CORS_ALLOW_ORIGINS='["https://…"]'.
    cors_allow_origins: list[str] = [
        "http://localhost:8095",   # CDS Hooks Sandbox (reference-sandbox demo)
        "http://localhost:8090",   # SMART App Launcher v2
        "http://localhost:8100",   # in-repo mock EHR
    ]
