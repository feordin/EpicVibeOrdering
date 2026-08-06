from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EPICVIBE_", env_file=".env")

    catalog_path: Path = Path("fixtures/catalog/sample_catalog.json")
    inference_provider: Literal["fake", "anthropic"] = "fake"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5-20251001"
    anthropic_base_url: str = "https://api.anthropic.com"
    verify_jwt: bool = False
    jwks_url: str = ""
    jwt_audience: str = ""
    audit_db_path: Path = Path("audit.db")
    cache_ttl_seconds: int = 3600
