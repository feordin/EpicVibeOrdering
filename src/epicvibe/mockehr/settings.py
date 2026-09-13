"""Configuration for the mock EHR.

Deliberately independent of ``epicvibe.config.Settings`` (which is owned by the CDS
service) -- the mock EHR is a standalone demo harness and reads its own ``MOCKEHR_*``
environment variables.
"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field

FIXTURES_RELATIVE = Path("fixtures") / "mockehr" / "patients"


def default_fixtures_dir() -> Path:
    """Locate ``fixtures/mockehr/patients`` relative to the repo, not to the CWD.

    ``python -m epicvibe.mockehr`` is routinely started from somewhere other than the
    repo root; a CWD-relative default silently produced an empty store.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / FIXTURES_RELATIVE
        if candidate.is_dir():
            return candidate
    return FIXTURES_RELATIVE


class MockEhrSettings(BaseModel):
    """Runtime settings for the mock EHR app."""

    host: str = "127.0.0.1"
    port: int = 8100
    base_url: str = "http://localhost:8100"
    cds_base_url: str = "http://localhost:8000"
    fixtures_dir: Path = Field(default_factory=default_fixtures_dir)
    hook_timeout_seconds: float = 3.0
    user_id: str = "Practitioner/pr-1"

    @classmethod
    def from_env(cls, **overrides) -> "MockEhrSettings":
        env: dict = {}
        if os.environ.get("MOCKEHR_HOST"):
            env["host"] = os.environ["MOCKEHR_HOST"]
        if os.environ.get("MOCKEHR_PORT"):
            env["port"] = int(os.environ["MOCKEHR_PORT"])
        if os.environ.get("MOCKEHR_BASE_URL"):
            env["base_url"] = os.environ["MOCKEHR_BASE_URL"]
        if os.environ.get("MOCKEHR_CDS_BASE_URL"):
            env["cds_base_url"] = os.environ["MOCKEHR_CDS_BASE_URL"]
        if os.environ.get("MOCKEHR_FIXTURES_DIR"):
            env["fixtures_dir"] = Path(os.environ["MOCKEHR_FIXTURES_DIR"])
        if os.environ.get("MOCKEHR_HOOK_TIMEOUT_SECONDS"):
            env["hook_timeout_seconds"] = float(os.environ["MOCKEHR_HOOK_TIMEOUT_SECONDS"])
        env.update(overrides)
        # MOCKEHR_PORT without an explicit MOCKEHR_BASE_URL should move the base URL too.
        if "port" in env and "base_url" not in env:
            env["base_url"] = f"http://localhost:{env['port']}"
        return cls(**env)

    @property
    def fhir_base(self) -> str:
        return f"{self.base_url.rstrip('/')}/fhir"
