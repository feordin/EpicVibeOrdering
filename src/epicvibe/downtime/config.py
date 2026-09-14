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
    provider: Literal["fake", "anthropic", "ollama"] = "fake"
    anthropic_api_key: str = ""
    model: str = "claude-opus-5"

    # A downtime transcript is ambient clinical audio: it contains the patient's name,
    # DOB and complaint. Sending it to a *hosted* model is a deliberate, per-deployment
    # decision, so the hosted provider will not even construct without this flag.
    #
    # This gate applies to `provider="anthropic"` only. `provider="ollama"` runs the
    # weights on this box (or on a host inside the hospital network that the operator
    # names in `ollama_base_url`), so no transcript crosses the trust boundary and
    # there is nothing for the flag to authorise. The privacy question for Ollama is
    # therefore "where is ollama_base_url pointing?", not "is PHI allowed out?".
    allow_phi_to_model: bool = False

    # --- Ollama (fully local inference) ---
    # Default points at a daemon on this machine. If you change it to a remote host,
    # you have re-opened the PHI question yourself: point it only at a machine inside
    # the same trust boundary as the downtime box.
    ollama_base_url: str = "http://localhost:11434"
    # Whatever `ollama list` shows locally. See docs/downtime-local-models.md for the
    # measured size-versus-quality trade-off.
    ollama_model: str = "gemma4:26b"
    # Generous: a 20B+ model doing a full template fill on CPU is minutes, not seconds.
    ollama_timeout_seconds: float = 300.0
    # Measured on the largest fixture template: ~6,800 prompt tokens for the spec
    # plus the transcript, and ~3,100 for the emitted fill. 8k truncates that mid-
    # object and Ollama returns an empty string, so 16k is the working floor.
    ollama_num_ctx: int = 16384
    # Keep the weights resident between the select and fill calls of one capture.
    ollama_keep_alive: str = "10m"
    # Reasoning models put their scratchpad in `message.thinking` and their answer
    # in `message.content`. Under a grammar-constrained `format` a reasoning model
    # can spend its whole context thinking and return an empty answer, so thinking
    # is off by default. Set to true only if you have measured that a given model
    # is both better and still finishes. Non-reasoning models ignore it (the
    # provider drops the key if the daemon rejects it).
    ollama_think: bool = False

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
