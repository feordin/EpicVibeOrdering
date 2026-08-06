from pathlib import Path
from epicvibe.config import Settings

def test_defaults():
    s = Settings(_env_file=None)
    assert s.inference_provider == "fake"
    assert s.verify_jwt is False
    assert s.catalog_path == Path("fixtures/catalog/sample_catalog.json")

def test_env_override(monkeypatch):
    monkeypatch.setenv("EPICVIBE_INFERENCE_PROVIDER", "anthropic")
    assert Settings(_env_file=None).inference_provider == "anthropic"
