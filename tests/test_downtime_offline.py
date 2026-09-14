"""Offline-strict mode: the locality checklist and the startup refusal.

Every check here is deliberately made to fail somewhere, because the value of
the feature is entirely in what it refuses - a checklist that only ever says
"all green" proves nothing.
"""

import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from epicvibe.downtime.app import create_app
from epicvibe.downtime.config import DowntimeSettings
from epicvibe.downtime.offline import (
    OfflineSettings,
    enforce_strict,
    is_local_host,
    offline_report,
    tcp_reachable,
)

REPO = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = REPO / "fixtures" / "downtime" / "templates"
TRANSCRIPT_DIR = REPO / "fixtures" / "downtime" / "transcripts"


class StubWhisperSettings:
    """TranscribeSettings-shaped, with weights that are definitely not on disk."""

    def __init__(self, model_dir: Path | None = None, model: str = "no-such-model-xyz"):
        self.model_dir = model_dir
        self.model = model
        self.enabled = True


def closed_port() -> int:
    """A port nothing is listening on: bind it, read it back, let it go."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def settings(tmp_path):
    return DowntimeSettings(
        provider="fake",
        templates_dir=TEMPLATE_DIR,
        transcripts_dir=TRANSCRIPT_DIR,
        db_path=tmp_path / "downtime.db",
        engine_host="127.0.0.1",
        engine_port=closed_port(),
    )


# --------------------------------------------------------------------------
# host classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "10.1.2.3",
                                  "192.168.4.5", "172.16.0.9", "169.254.1.1"])
def test_loopback_and_private_hosts_are_local(host):
    assert is_local_host(host) is True


@pytest.mark.parametrize("host", ["8.8.8.8", "1.1.1.1", "", "   "])
def test_public_and_empty_hosts_are_not_local(host):
    assert is_local_host(host) is False


def test_unresolvable_name_is_not_local():
    # Cannot prove it is inside the trust boundary, so it does not get the badge.
    assert is_local_host("engine.invalid") is False


def test_tcp_reachable_is_false_for_a_closed_port():
    assert tcp_reachable("127.0.0.1", closed_port()) is False
    assert tcp_reachable("127.0.0.1", 0) is False


# --------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------


def test_report_with_local_provider_and_dead_engine(settings):
    report = offline_report(settings, StubWhisperSettings())
    assert report["provider_local"] is True          # provider="fake" runs here
    assert report["whisper_model_cached"] is False   # stub model is not on disk
    assert report["engine_reachable"] is False       # port is closed
    assert report["all_local"] is False
    assert "engine_reachable" in report["not_local"]
    assert "whisper_model_cached" in report["not_local"]
    assert "provider_local" not in report["not_local"]
    # Every failing item explains itself; the badge tooltip is made of these.
    for name in report["not_local"]:
        assert report["detail"][name]


def test_report_flags_a_hosted_provider(settings):
    report = offline_report(settings.model_copy(update={"provider": "anthropic"}),
                            StubWhisperSettings())
    assert report["provider_local"] is False
    assert "provider_local" in report["not_local"]
    assert "leaves the box" in report["detail"]["provider_local"]


def test_report_accepts_a_local_ollama(settings):
    local = settings.model_copy(update={"provider": "ollama",
                                        "ollama_base_url": "http://localhost:11434"})
    assert offline_report(local, StubWhisperSettings())["provider_local"] is True


def test_report_rejects_a_remote_ollama(settings):
    remote = settings.model_copy(update={"provider": "ollama",
                                         "ollama_base_url": "https://ollama.example.com"})
    report = offline_report(remote, StubWhisperSettings())
    assert report["provider_local"] is False
    assert "trust boundary" in report["detail"]["provider_local"]


def test_report_rejects_an_engine_on_a_public_address(settings):
    remote = settings.model_copy(update={"engine_host": "8.8.8.8", "engine_port": 2575})
    report = offline_report(remote, StubWhisperSettings())
    assert report["engine_reachable"] is False
    assert "private address" in report["detail"]["engine_reachable"]


def test_report_sees_cached_whisper_weights(settings, tmp_path):
    weights = tmp_path / "whisper-small"
    weights.mkdir()
    (weights / "model.bin").write_bytes(b"not really a model")
    report = offline_report(settings, StubWhisperSettings(model_dir=weights))
    # Only meaningful when the package is installed; otherwise the install check
    # is the one that fails and the cache check follows it.
    assert report["whisper_model_cached"] is report["whisper_installed"]


def test_report_reads_settings_defensively(settings):
    """A settings object missing the newer fields must not blow the report up."""

    class Bare:
        provider = "fake"

    report = offline_report(Bare(), None)
    assert report["provider_local"] is True
    assert report["engine_reachable"] is False
    assert report["whisper_model_cached"] is False


def test_report_carries_the_strict_flag(settings):
    assert offline_report(settings, None, strict=True)["strict"] is True
    assert offline_report(settings, None)["strict"] is False


def test_enforce_strict_names_every_non_local_item():
    report = {"all_local": False, "not_local": ["provider_local", "engine_reachable"],
              "detail": {"provider_local": "hosted model", "engine_reachable": "nothing listening"}}
    with pytest.raises(RuntimeError) as exc:
        enforce_strict(report)
    assert "provider_local" in str(exc.value) and "hosted model" in str(exc.value)
    assert "engine_reachable" in str(exc.value)


def test_enforce_strict_passes_when_all_local():
    enforce_strict({"all_local": True})  # no raise


# --------------------------------------------------------------------------
# app wiring
# --------------------------------------------------------------------------


def test_strict_startup_refuses_a_non_local_deployment(settings):
    with pytest.raises(RuntimeError) as exc:
        create_app(settings.model_copy(update={"provider": "anthropic"}),
                   offline=OfflineSettings(strict=True))
    assert "EPICVIBE_DOWNTIME_OFFLINE_STRICT" in str(exc.value)
    assert "provider_local" in str(exc.value)


def test_status_and_offline_endpoints_expose_the_badge_fields(settings):
    with TestClient(create_app(settings, offline=OfflineSettings(strict=False))) as c:
        status = c.get("/api/status").json()
        assert set(status["offline"]) == {"strict", "all_local", "not_local"}
        assert status["offline"]["strict"] is False
        assert status["offline"]["all_local"] is False   # dead engine in this fixture
        assert "engine_reachable" in status["offline"]["not_local"]

        report = c.get("/api/offline").json()
        assert report["provider"] == "fake"
        assert report["all_local"] is False
        assert report["detail"]["engine_reachable"]
        assert status["offline"]["not_local"] == report["not_local"]


def test_badge_is_rendered_from_the_status_payload(settings):
    with TestClient(create_app(settings, offline=OfflineSettings(strict=False))) as c:
        body = c.get("/").text
    assert "LOCAL ONLY" in body
    assert "renderOffline(status.offline)" in body
    assert 'id="offline"' in body
