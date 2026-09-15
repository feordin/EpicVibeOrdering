"""Generate multi-speaker sample audio for the downtime transcript fixtures.

Every transcript in ``fixtures/downtime/transcripts/*.txt`` is a labelled
dialogue (``NURSE:``, ``DR. ALVAREZ:``, ``PATIENT:`` ...).  This script turns
each one into a single ``.ogg`` (Opus) ambient-capture clip where every speaker
gets a distinct neural voice, so the demo's "Transcribe sample audio..." menu
sounds like a room with people in it rather than one robot reading a script.

What ends up in the audio
-------------------------
* Bracketed lines (``[Ambient capture - ...]``, ``[End of capture]``) and inline
  stage directions (``[pause]``) are **not** spoken.  They survive in the
  sidecar ``<name>.txt`` so the file still reads like the source transcript.
* Speaker labels are **not** spoken either - the voices carry that information.
  The sidecar keeps them for readability; strip the ``SPEAKER: `` prefix and the
  bracketed lines and what is left is exactly the spoken reference (that is what
  the WER numbers in ``docs/runbook-demo.md`` are measured against).
* Turns are joined with a short pause: ~380 ms inside one speaker's turn,
  ~620 ms when the speaker changes, ~700 ms where the transcript had a
  ``[pause]``.

Requirements
------------
* ``edge-tts`` - a **dev-only** tool, deliberately *not* a runtime dependency of
  ``epicvibe``.  It lives in the ``tools`` extra; install it only to regenerate
  the fixtures::

      .venv/Scripts/python -m pip install -e ".[tools]"

* ``PyAV`` - already present via the ``audio`` extra (faster-whisper ships it).
  Used to decode the MP3 chunks edge-tts returns and to encode Opus.

**Network:** edge-tts calls Microsoft's online speech endpoint, so this script
needs internet *at generation time only*.  The generated fixtures are committed;
nothing at demo time or test time ever reaches the network for audio.

Usage
-----
    .venv/Scripts/python scripts/make_sample_audio.py            # all six
    .venv/Scripts/python scripts/make_sample_audio.py ed-cap-admission
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import av
import numpy as np

GENERATOR_VERSION = "1.0.0"

REPO = Path(__file__).resolve().parents[1]
TRANSCRIPT_DIR = REPO / "fixtures" / "downtime" / "transcripts"
AUDIO_DIR = REPO / "fixtures" / "downtime" / "audio"

SAMPLE_RATE = 24_000          # libopus-native rate; Whisper resamples anyway
BIT_RATE = 24_000             # ~180 KB per minute of mono speech
GAP_SAME_SPEAKER_MS = 380
GAP_SPEAKER_CHANGE_MS = 620
GAP_STAGE_DIRECTION_MS = 700
LEAD_IN_MS = 300
TAIL_MS = 500

#: speaker -> (voice, rate).  One voice per role, and no voice is reused across
#: the six files, so a listener flipping between samples hears six different
#: care teams.  A slower rate is a subtle nod to an elderly or breathless
#: patient - it is not meant to be theatrical.
VOICES: dict[str, dict[str, tuple[str, str]]] = {
    "ed-cap-admission": {
        # Harold Bennett, 73, hypoxic and winded -> slower.
        "NURSE": ("en-US-MichelleNeural", "+0%"),
        "DR. ALVAREZ": ("en-US-ChristopherNeural", "+0%"),
        "PATIENT": ("en-US-RogerNeural", "-10%"),
    },
    "chf-exacerbation-admission": {
        # Walter Brzezinski, 79, orthopnoeic -> slower.
        "NURSE": ("en-US-EricNeural", "+0%"),
        "DR. LINDQVIST": ("en-US-AriaNeural", "+0%"),
        "PATIENT": ("en-GB-ThomasNeural", "-10%"),
    },
    "dka-management": {
        # Priya Raghavan, 23, vomiting -> only slightly slower.
        "RESIDENT": ("en-CA-LiamNeural", "+0%"),
        "DR. NAKAMURA": ("en-US-JennyNeural", "+0%"),
        "PATIENT": ("en-AU-NatashaNeural", "-5%"),
    },
    "ed-chest-pain-acs": {
        # The triage nurse and the bedside nurse are the same person.
        "TRIAGE NURSE": ("en-US-GuyNeural", "+0%"),
        "NURSE": ("en-US-GuyNeural", "+0%"),
        "DR. OKAFOR": ("en-GB-SoniaNeural", "+0%"),
        "PATIENT": ("en-US-EmmaNeural", "+0%"),
    },
    "sepsis-bundle": {
        "CHARGE NURSE": ("en-US-AvaNeural", "+0%"),
        "DR. WHITFIELD": ("en-US-AndrewNeural", "+0%"),
        "PARAMEDIC": ("en-IE-ConnorNeural", "+0%"),
    },
    "ambulatory-new-t2dm": {
        "MA": ("en-NZ-MollyNeural", "+0%"),
        "DR. SANDOVAL": ("en-US-SteffanNeural", "+0%"),
        "PATIENT": ("en-CA-ClaraNeural", "+0%"),
    },
}

SPEAKER_RE = re.compile(r"^([A-Z][A-Z.'\- ]*[A-Z.]):\s+(.*)$")
BRACKET_RE = re.compile(r"\[[^\]]*\]")


@dataclass
class Turn:
    speaker: str
    text: str
    #: True when this fragment follows an inline stage direction like [pause].
    after_stage_direction: bool = False


def parse_transcript(path: Path) -> tuple[list[str], list[Turn]]:
    """Return (sidecar lines, spoken turns).

    Sidecar lines keep the bracketed header/footer and the speaker labels;
    the turns carry only what is actually synthesised.
    """
    sidecar: list[str] = []
    turns: list[Turn] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        sidecar.append(line)
        if line.startswith("[") and line.endswith("]"):
            continue  # header / footer - reference only, never spoken
        m = SPEAKER_RE.match(line)
        if not m:
            raise ValueError(f"{path.name}: unparsable line: {line!r}")
        speaker, body = m.group(1), m.group(2)
        # An inline [pause] splits the turn in two and buys a longer gap.
        parts = [p.strip() for p in BRACKET_RE.split(body)]
        for i, part in enumerate(p for p in parts if p):
            turns.append(Turn(speaker, part, after_stage_direction=i > 0))
    return sidecar, turns


# --- synthesis -------------------------------------------------------------


async def _synth(text: str, voice: str, rate: str) -> bytes:
    import edge_tts

    chunks: list[bytes] = []
    comm = edge_tts.Communicate(text, voice, rate=rate)
    async for item in comm.stream():
        if item["type"] == "audio":
            chunks.append(item["data"])
    if not chunks:
        raise RuntimeError(f"edge-tts returned no audio for {voice}: {text[:60]!r}")
    return b"".join(chunks)


def _decode_to_pcm(mp3: bytes) -> np.ndarray:
    """MP3 bytes -> mono int16 PCM at SAMPLE_RATE."""
    out: list[np.ndarray] = []
    with av.open(io.BytesIO(mp3)) as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        stream = container.streams.audio[0]
        for frame in container.decode(stream):
            for r in resampler.resample(frame):
                out.append(r.to_ndarray().reshape(-1))
        for r in resampler.resample(None):
            out.append(r.to_ndarray().reshape(-1))
    if not out:
        raise RuntimeError("no audio decoded from the edge-tts MP3 stream")
    return np.concatenate(out).astype(np.int16)


def _silence(ms: int) -> np.ndarray:
    return np.zeros(int(SAMPLE_RATE * ms / 1000), dtype=np.int16)


def _encode_opus(pcm: np.ndarray, dest: Path) -> None:
    """Write mono Opus into an Ogg container at BIT_RATE."""
    with av.open(str(dest), mode="w", format="ogg") as container:
        stream = container.add_stream("libopus", rate=SAMPLE_RATE)
        stream.bit_rate = BIT_RATE
        frame_size = stream.codec_context.frame_size or 960
        pts = 0
        padded = np.concatenate(
            [pcm, np.zeros((-len(pcm)) % frame_size, dtype=np.int16)])
        for i in range(0, len(padded), frame_size):
            block = np.ascontiguousarray(padded[i:i + frame_size].reshape(1, -1))
            frame = av.AudioFrame.from_ndarray(block, format="s16", layout="mono")
            frame.sample_rate = SAMPLE_RATE
            frame.pts = pts
            pts += frame_size
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)


# --- driver ----------------------------------------------------------------


async def build(name: str) -> dict:
    src = TRANSCRIPT_DIR / f"{name}.txt"
    voices = VOICES.get(name)
    if voices is None:
        raise SystemExit(f"no voice casting for {name!r}; add it to VOICES")
    sidecar_lines, turns = parse_transcript(src)

    missing = sorted({t.speaker for t in turns} - set(voices))
    if missing:
        raise SystemExit(f"{name}: no voice for speaker(s) {missing}")

    pieces: list[np.ndarray] = [_silence(LEAD_IN_MS)]
    previous: str | None = None
    for turn in turns:
        if previous is not None:
            gap = (GAP_STAGE_DIRECTION_MS if turn.after_stage_direction
                   else GAP_SPEAKER_CHANGE_MS if turn.speaker != previous
                   else GAP_SAME_SPEAKER_MS)
            pieces.append(_silence(gap))
        voice, rate = voices[turn.speaker]
        pieces.append(_decode_to_pcm(await _synth(turn.text, voice, rate)))
        previous = turn.speaker
    pieces.append(_silence(TAIL_MS))

    pcm = np.concatenate(pieces)
    dest = AUDIO_DIR / f"{name}.ogg"
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    _encode_opus(pcm, dest)

    duration_s = round(len(pcm) / SAMPLE_RATE, 2)
    header = [
        f"# Spoken script for {dest.name} (make_sample_audio.py v{GENERATOR_VERSION}).",
        "# Lines in [brackets] are stage directions from the source transcript "
        "and are NOT spoken.",
        "# Speaker labels are NOT spoken either - each speaker has its own voice; "
        "see the .voices.json.",
        "",
    ]
    (AUDIO_DIR / f"{name}.txt").write_text(
        "\n".join(header + sidecar_lines) + "\n", encoding="utf-8")

    spoken = {t.speaker for t in turns}
    meta = {
        "generator": "scripts/make_sample_audio.py",
        "generator_version": GENERATOR_VERSION,
        "source_transcript": f"fixtures/downtime/transcripts/{name}.txt",
        "audio": dest.name,
        "codec": "libopus",
        "container": "ogg",
        "sample_rate_hz": SAMPLE_RATE,
        "bit_rate_bps": BIT_RATE,
        "channels": 1,
        "duration_s": duration_s,
        "bytes": dest.stat().st_size,
        "turns": len(turns),
        "voices": {sp: {"voice": v, "rate": r}
                   for sp, (v, r) in voices.items() if sp in spoken},
    }
    (AUDIO_DIR / f"{name}.voices.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return meta


async def main(argv: list[str]) -> int:
    names = argv or sorted(p.stem for p in TRANSCRIPT_DIR.glob("*.txt"))
    rows = []
    for name in names:
        meta = await build(name)
        rows.append(meta)
        print(f"  wrote {meta['audio']}  {meta['duration_s']:.1f}s  "
              f"{meta['bytes'] / 1024:.0f} KB")

    width = max(len(r["audio"]) for r in rows)
    print(f"\n{'file'.ljust(width)}  {'dur':>7}  {'size':>8}  {'turns':>5}  voices")
    print("-" * (width + 40))
    for r in rows:
        cast = ", ".join(f"{sp.title()}={v['voice'].replace('Neural', '')}"
                         for sp, v in r["voices"].items())
        print(f"{r['audio'].ljust(width)}  {r['duration_s']:6.1f}s  "
              f"{r['bytes'] / 1024:7.0f}K  {r['turns']:5d}  {cast}")
    total = sum(r["bytes"] for r in rows)
    print(f"\ntotal {total / 1024 / 1024:.2f} MB across {len(rows)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
