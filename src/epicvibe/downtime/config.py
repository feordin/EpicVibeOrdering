"""Settings for the downtime ordering subsystem.

Deliberately self-contained (own BaseSettings, own env prefix) so the downtime
package can run standalone during a declared downtime without depending on the
live-mode configuration.
"""

import os
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[3]


class DowntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EPICVIBE_DOWNTIME_", env_file=".env", extra="ignore"
    )

    # --- inference ---
    provider: Literal["fake", "anthropic"] = "fake"
    anthropic_api_key: str = ""
    model: str = "claude-opus-5"

    # A downtime transcript is ambient clinical audio: it contains the patient's name,
    # DOB and complaint. Sending it to a hosted model is a deliberate, per-deployment
    # decision, so the hosted provider will not even construct without this flag.
    allow_phi_to_model: bool = False

    # --- data locations ---
    templates_dir: Path = Field(default=_REPO_ROOT / "fixtures" / "downtime" / "templates")
    transcripts_dir: Path = Field(default=_REPO_ROOT / "fixtures" / "downtime" / "transcripts")
    db_path: Path = Path("downtime.db")

    # --- integration engine (Epic Bridges / Rhapsody / Mirth) ---
    engine_host: str = "127.0.0.1"
    engine_port: int = 2575
    sending_application: str = "EPICVIBE"
    sending_facility: str = "DOWNTIME"
    receiving_application: str = "EPIC"
    receiving_facility: str = "BRIDGES"

    def resolved_api_key(self) -> str:
        """API key with fallbacks to the shared env var names."""
        return (
            self.anthropic_api_key
            or os.environ.get("ANTHROPIC_API_KEY", "")
            or os.environ.get("EPICVIBE_ANTHROPIC_API_KEY", "")
        )
