"""Local Whisper transcription: endpoints, settings, and the degraded path.

The unit tests stub `faster_whisper.WhisperModel` so nothing is downloaded and
nothing is decoded - they exercise the wiring, not the acoustic model. The one
integration test at the bottom runs the real thing against the fixture WAV and
is skipped whenever faster-whisper or the weights are not available.
"""

import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from epicvibe.downtime import transcribe as tr
from epicvibe.downtime.app import create_app
from epicvibe.downtime.config import DowntimeSettings

REPO = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = REPO / "fixtures" / "downtime" / "templates"
TRANSCRIPT_DIR = REPO / "fixtures" / "downtime" / "transcripts"
AUDIO_DIR = REPO / "fixtures" / "downtime" / "audio"
SAMPLE_WAV = AUDIO_DIR / "ed-cap-admission-excerpt.wav"


# --- stubs -----------------------------------------------------------------


class _StubSegment:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


class _StubInfo:
    language = "en"
    duration = 12.5


class _StubModel:
    """Stands in for WhisperModel: records how it was constructed and called."""

    instances: list["_StubModel"] = []

    def __init__(self, model, **kwargs):
        self.model, self.kwargs, self.calls = model, kwargs, []
        _StubModel.instances.append(self)

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        # A generator, like the real API - the caller must drain it.
        segs = (s for s in [_StubSegment(0.0, 6.0, " Mr. Harold Bennett has pneumonia."),
                            _StubSegment(6.0, 12.5, " Ceftriaxone one gram IV daily.")])
        return segs, _StubInfo()


@pytest.fixture
def stub_whisper(monkeypatch):
    _StubModel.instances.clear()
    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = _StubModel
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    monkeypatch.setattr(tr, "faster_whisper_installed", lambda: True)
    return _StubModel


def _settings(tmp_path: Path) -> DowntimeSettings:
    return DowntimeSettings(
        provider="fake", templates_dir=TEMPLATE_DIR,
        transcripts_dir=TRANSCRIPT_DIR, db_path=tmp_path / "downtime.db",
    )


def _client(tmp_path, transcriber, audio_dir=AUDIO_DIR) -> TestClient:
    return TestClient(create_app(_settings(tmp_path), transcriber=transcriber,
                                 audio_dir=audio_dir))


# --- settings --------------------------------------------------------------


def test_settings_defaults_and_env_prefix(monkeypatch):
    s = tr.TranscribeSettings()
    assert (s.model, s.language, s.enabled, s.beam_size, s.vad_filter) == (
        "small", "en", True, 1, True)
    assert s.model_dir is None

    monkeypatch.setenv("EPICVIBE_DOWNTIME_WHISPER_MODEL", "tiny.en")
    monkeypatch.setenv("EPICVIBE_DOWNTIME_WHISPER_ENABLED", "false")
    monkeypatch.setenv("EPICVIBE_DOWNTIME_WHISPER_MODEL_DIR", "/models/whisper")
    s2 = tr.TranscribeSettings()
    assert s2.model == "tiny.en" and s2.enabled is False
    assert s2.model_dir == Path("/models/whisper")


# --- Transcriber -----------------------------------------------------------


def test_transcriber_is_lazy_and_reuses_one_model(stub_whisper):
    t = tr.Transcriber(tr.TranscribeSettings(model="tiny.en"))
    assert t._model is None, "model must not load at construction"
    t.transcribe(b"RIFFfake", "audio/wav")
    t.transcribe(b"RIFFfake", "audio/wav")
    assert len(stub_whisper.instances) == 1, "model should load once and be reused"
    assert stub_whisper.instances[0].kwargs["device"] == "cpu"
    assert stub_whisper.instances[0].kwargs["compute_type"] == "int8"


def test_transcribe_shapes_the_result(stub_whisper):
    t = tr.Transcriber(tr.TranscribeSettings(model="tiny.en", beam_size=3, vad_filter=False))
    res = t.transcribe(b"RIFFfake", "audio/wav")
    assert res.text == "Mr. Harold Bennett has pneumonia. Ceftriaxone one gram IV daily."
    assert [s.start for s in res.segments] == [0.0, 6.0]
    assert res.language == "en" and res.duration_s == 12.5
    assert res.model == "tiny.en" and res.elapsed_s >= 0
    call = stub_whisper.instances[0].calls[0]
    assert call == {"language": "en", "beam_size": 3, "vad_filter": False}


def test_model_dir_with_converted_weights_is_used_as_the_model_id(stub_whisper, tmp_path):
    (tmp_path / "model.bin").write_bytes(b"x")
    t = tr.Transcriber(tr.TranscribeSettings(model="small", model_dir=tmp_path))
    t.load()
    assert stub_whisper.instances[0].model == str(tmp_path)
    assert "download_root" not in stub_whisper.instances[0].kwargs


def test_model_dir_without_weights_becomes_download_root(stub_whisper, tmp_path):
    t = tr.Transcriber(tr.TranscribeSettings(model="small", model_dir=tmp_path))
    t.load()
    assert stub_whisper.instances[0].model == "small"
    assert stub_whisper.instances[0].kwargs["download_root"] == str(tmp_path)


def test_empty_and_unsupported_audio_are_rejected(stub_whisper):
    t = tr.Transcriber(tr.TranscribeSettings())
    with pytest.raises(ValueError):
        t.transcribe(b"", "audio/wav")
    with pytest.raises(ValueError):
        t.transcribe(b"data", "text/plain")
    # parameters after the media type must not confuse the check
    t.transcribe(b"data", "audio/webm;codecs=opus")


def test_disabled_or_missing_package_raises_unavailable(monkeypatch):
    t = tr.Transcriber(tr.TranscribeSettings(enabled=False))
    with pytest.raises(tr.TranscriberUnavailable):
        t.load()
    monkeypatch.setattr(tr, "faster_whisper_installed", lambda: False)
    t2 = tr.Transcriber(tr.TranscribeSettings())
    assert t2.available() is False
    with pytest.raises(tr.TranscriberUnavailable):
        t2.load()


# --- endpoints -------------------------------------------------------------


def test_status_endpoint_reports_availability(tmp_path, stub_whisper):
    t = tr.Transcriber(tr.TranscribeSettings(model="tiny.en"))
    with _client(tmp_path, t) as c:
        body = c.get("/api/transcribe/status").json()
    assert body["enabled"] is True and body["installed"] is True
    assert body["model"] == "tiny.en" and body["loaded"] is False
    assert "ed-cap-admission-excerpt" in body["samples"]


def test_post_transcribe_returns_text(tmp_path, stub_whisper):
    t = tr.Transcriber(tr.TranscribeSettings(model="tiny.en"))
    with _client(tmp_path, t) as c:
        r = c.post("/api/transcribe", content=b"RIFFfake",
                   headers={"content-type": "audio/wav"})
    assert r.status_code == 200
    assert "pneumonia" in r.json()["text"]
    assert len(r.json()["segments"]) == 2


def test_post_transcribe_rejects_empty_and_bad_media_type(tmp_path, stub_whisper):
    t = tr.Transcriber(tr.TranscribeSettings())
    with _client(tmp_path, t) as c:
        assert c.post("/api/transcribe", content=b"",
                      headers={"content-type": "audio/wav"}).status_code == 400
        assert c.post("/api/transcribe", content=b"hello",
                      headers={"content-type": "text/plain"}).status_code == 400


def test_audio_sample_listing_and_transcription(tmp_path, stub_whisper):
    t = tr.Transcriber(tr.TranscribeSettings())
    with _client(tmp_path, t) as c:
        listing = c.get("/api/transcripts/audio").json()
        names = [a["name"] for a in listing]
        assert "ed-cap-admission-excerpt" in names
        assert all(a["content_type"] == "audio/wav" for a in listing if a["name"] in names)
        r = c.post("/api/transcribe/sample/ed-cap-admission-excerpt")
        assert r.status_code == 200 and "Bennett" in r.json()["text"]
        assert c.post("/api/transcribe/sample/nope").status_code == 404


def test_missing_audio_dir_lists_nothing(tmp_path, stub_whisper):
    t = tr.Transcriber(tr.TranscribeSettings())
    with _client(tmp_path, t, audio_dir=tmp_path / "no-such-dir") as c:
        assert c.get("/api/transcripts/audio").json() == []
        assert c.get("/api/transcribe/status").json()["samples"] == []


def test_501_when_faster_whisper_is_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "faster_whisper_installed", lambda: False)
    t = tr.Transcriber(tr.TranscribeSettings())
    with _client(tmp_path, t) as c:
        r = c.post("/api/transcribe", content=b"RIFFfake",
                   headers={"content-type": "audio/wav"})
        assert r.status_code == 501
        assert "faster-whisper" in r.json()["detail"]
        assert c.post("/api/transcribe/sample/ed-cap-admission-excerpt").status_code == 501
        assert c.get("/api/transcribe/status").json()["installed"] is False


def test_501_when_disabled(tmp_path, stub_whisper):
    t = tr.Transcriber(tr.TranscribeSettings(enabled=False))
    with _client(tmp_path, t) as c:
        r = c.post("/api/transcribe", content=b"RIFFfake",
                   headers={"content-type": "audio/wav"})
        assert r.status_code == 501 and "disabled" in r.json()["detail"]


def test_transcription_endpoints_do_not_disturb_the_rest_of_the_api(tmp_path, stub_whisper):
    """The audio work is additive - the capture flow must be untouched."""
    t = tr.Transcriber(tr.TranscribeSettings())
    with _client(tmp_path, t) as c:
        assert c.get("/api/status").status_code == 200
        assert len(c.get("/api/transcripts").json()) >= 1
        assert c.get("/api/templates").status_code == 200


# --- integration (real model, real audio) ----------------------------------

_real_settings = tr.TranscribeSettings()
_skip_real = pytest.mark.skipif(
    not (SAMPLE_WAV.exists()
         and tr.faster_whisper_installed()
         and tr.model_cached(_real_settings)),
    reason="needs the audio extra, the fixture WAV, and cached Whisper weights",
)


@_skip_real
def test_real_whisper_transcribes_the_sample_wav(tmp_path):
    t = tr.Transcriber(tr.TranscribeSettings())
    with _client(tmp_path, t) as c:
        r = c.post("/api/transcribe/sample/ed-cap-admission-excerpt")
    assert r.status_code == 200
    body = r.json()
    lower = body["text"].lower()
    assert "pneumonia" in lower
    assert "ceftriaxone" in lower or "bennett" in lower
    assert body["language"] == "en"
    assert body["duration_s"] > 30
    assert body["segments"]
