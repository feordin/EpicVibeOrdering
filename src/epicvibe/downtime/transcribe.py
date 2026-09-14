"""Fully local speech-to-text for the downtime capture UI.

A declared downtime is exactly the moment you cannot rely on a cloud STT
vendor, so the transcript has to come off the box the tool is running on.
`faster-whisper` (CTranslate2) runs Whisper on CPU at int8 with no GPU and no
network once the weights are on disk.

The `faster_whisper` import is deliberately lazy: the package ships as the
optional ``audio`` extra, and the downtime app must still start on a box that
never installed it - the endpoints degrade to a 501 with an install hint
instead of taking the whole tool down.
"""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

INSTALL_HINT = (
    'local transcription needs the optional audio extra: '
    'pip install -e ".[audio]"  (faster-whisper, CPU int8)'
)

#: Containers PyAV (bundled with faster-whisper) decodes straight from memory.
SUPPORTED_CONTENT_TYPES = {
    "audio/wav", "audio/wave", "audio/x-wav",
    "audio/webm", "video/webm",
    "audio/ogg", "audio/opus",
    "audio/mpeg", "audio/mp3",
    "audio/mp4", "audio/m4a", "audio/x-m4a",
    "audio/flac", "audio/x-flac",
    "application/octet-stream",
}


class TranscribeSettings(BaseSettings):
    """Whisper settings, separate from :class:`DowntimeSettings` on purpose.

    Kept in its own file with its own env prefix so an audio-less deployment
    never has to think about these, and so the core downtime config stays
    importable without the optional extra.
    """

    model_config = SettingsConfigDict(
        env_prefix="EPICVIBE_DOWNTIME_WHISPER_", env_file=".env", extra="ignore"
    )

    enabled: bool = True
    model: str = "small"
    #: Pre-seeded weights for an air-gapped box. When set, faster-whisper is
    #: pointed at this directory and never reaches for Hugging Face.
    model_dir: Path | None = None
    language: str = "en"
    beam_size: int = 1
    vad_filter: bool = True


class Segment(BaseModel):
    start: float
    end: float
    text: str


class TranscriptResult(BaseModel):
    text: str
    segments: list[Segment] = Field(default_factory=list)
    language: str = ""
    duration_s: float = 0.0
    model: str = ""
    elapsed_s: float = 0.0


class TranscriberUnavailable(RuntimeError):
    """faster-whisper is not installed, or transcription is switched off."""


def faster_whisper_installed() -> bool:
    from importlib.util import find_spec

    try:
        return find_spec("faster_whisper") is not None
    except (ImportError, ValueError):  # pragma: no cover - broken install
        return False


def _default_cache_dir() -> Path:
    """Where huggingface_hub parks the CTranslate2 weights by default."""
    return Path.home() / ".cache" / "huggingface" / "hub"


def model_cached(settings: TranscribeSettings) -> bool:
    """True when the weights are already on disk, i.e. no download needed."""
    if settings.model_dir is not None:
        d = Path(settings.model_dir)
        return d.is_dir() and any(d.glob("model.bin"))
    if Path(settings.model).is_dir():  # model= may itself be a local path
        return True
    hub = _default_cache_dir()
    if not hub.is_dir():
        return False
    stem = settings.model.replace("/", "--")
    return any(
        p.is_dir() and any(p.rglob("model.bin"))
        for p in hub.glob(f"models--*{stem}*")
    )


class Transcriber:
    """Lazily-loaded, thread-safe wrapper around ``WhisperModel``.

    Loading the model costs seconds and a few hundred MB, so it happens on the
    first real request and is then shared. CTranslate2 releases the GIL during
    inference, but model construction is not reentrant, so both the load and
    the inference call are serialised behind one lock - the downtime box has
    one clinician at a time, and a queued request beats an OOM.
    """

    def __init__(self, settings: TranscribeSettings | None = None) -> None:
        self.settings = settings or TranscribeSettings()
        self._model: Any = None
        self._lock = threading.Lock()

    # -- availability -------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.settings.enabled)

    def available(self) -> bool:
        return self.enabled and faster_whisper_installed()

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "installed": faster_whisper_installed(),
            "model": self.settings.model,
            "model_dir": str(self.settings.model_dir) if self.settings.model_dir else None,
            "model_cached": model_cached(self.settings),
            "language": self.settings.language,
            "loaded": self._model is not None,
            "install_hint": INSTALL_HINT,
        }

    # -- model --------------------------------------------------------------

    def _build_model(self) -> Any:
        from faster_whisper import WhisperModel  # lazy: optional extra

        kwargs: dict[str, Any] = {"device": "cpu", "compute_type": "int8"}
        target = self.settings.model
        if self.settings.model_dir is not None:
            # A directory holding a converted model is passed as the model id
            # itself; anything else is a hub id resolved under download_root.
            d = Path(self.settings.model_dir)
            if (d / "model.bin").exists():
                target = str(d)
            else:
                kwargs["download_root"] = str(d)
        return WhisperModel(target, **kwargs)

    def load(self) -> Any:
        if not self.enabled:
            raise TranscriberUnavailable("local transcription is disabled")
        if not faster_whisper_installed():
            raise TranscriberUnavailable(INSTALL_HINT)
        with self._lock:
            if self._model is None:
                self._model = self._build_model()
            return self._model

    # -- work ---------------------------------------------------------------

    def transcribe(self, audio: bytes, content_type: str | None = None) -> TranscriptResult:
        """Transcribe raw container bytes (wav/webm/ogg/mp3/m4a/flac).

        faster-whisper decodes through PyAV, which reads every container above
        straight from a file-like object, so there is no ffmpeg shell-out and
        no temp file on the path.
        """
        if not audio:
            raise ValueError("audio body is empty")
        ct = (content_type or "").split(";")[0].strip().lower()
        if ct and ct not in SUPPORTED_CONTENT_TYPES:
            raise ValueError(
                f"unsupported audio content-type {ct!r}; "
                f"expected one of {', '.join(sorted(SUPPORTED_CONTENT_TYPES))}"
            )
        model = self.load()
        started = time.perf_counter()
        segments, info = model.transcribe(
            io.BytesIO(audio),
            language=self.settings.language or None,
            beam_size=self.settings.beam_size,
            vad_filter=self.settings.vad_filter,
        )
        # `segments` is a generator: nothing is decoded until it is drained.
        out = [
            Segment(start=float(s.start), end=float(s.end), text=str(s.text).strip())
            for s in segments
        ]
        elapsed = time.perf_counter() - started
        return TranscriptResult(
            text=" ".join(s.text for s in out if s.text).strip(),
            segments=out,
            language=str(getattr(info, "language", "") or ""),
            duration_s=float(getattr(info, "duration", 0.0) or 0.0),
            model=self.settings.model,
            elapsed_s=round(elapsed, 3),
        )
