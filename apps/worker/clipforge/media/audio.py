"""Audio extraction for transcription.

Whisper wants 16 kHz mono PCM. Handing it anything else means it resamples
internally anyway, so doing it once here — with ffmpeg, which is already a hard
dependency — is both faster and reproducible.

The extraction is deliberately a separate artefact rather than a pipe. It is
small (roughly 2 MB per minute), it makes a re-run after a crash free rather than
re-decoding a 2 GB source, and when a transcription looks wrong it is the first
thing worth listening to.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = ["AudioExtractionError", "ExtractedAudio", "extract_audio"]

SAMPLE_RATE = 16_000


class AudioExtractionError(RuntimeError):
    """ffmpeg could not produce usable audio."""


@dataclass(frozen=True)
class ExtractedAudio:
    path: Path
    sample_rate: int
    channels: int


def extract_audio(
    source: Path,
    destination: Path,
    *,
    ffmpeg_bin: str = "ffmpeg",
    timeout_s: float = 3600.0,
) -> ExtractedAudio:
    """Decode ``source`` to 16 kHz mono WAV at ``destination``.

    Written to a temporary file and renamed, so a crash mid-decode cannot leave a
    truncated WAV that a resumed run would happily transcribe — producing a
    transcript that silently stops halfway through the video.
    """
    if not source.is_file():
        raise AudioExtractionError(f"no such source file: {source}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_suffix(destination.suffix + ".partial")

    argv = [
        ffmpeg_bin,
        "-y",
        "-v",
        "error",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        # The container is stated explicitly because the staging file is named
        # `.wav.partial`, and ffmpeg infers the muxer from the extension. Without
        # this it fails with "unable to choose an output format", which reads as
        # a codec problem and is nothing of the sort.
        "-f",
        "wav",
        str(staging),
    ]

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except FileNotFoundError as exc:
        raise AudioExtractionError(
            f"{ffmpeg_bin} not found on PATH. ffmpeg is a hard dependency; run tools/doctor.ps1"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        staging.unlink(missing_ok=True)
        raise AudioExtractionError(f"ffmpeg timed out after {timeout_s}s on {source}") from exc

    if completed.returncode != 0:
        staging.unlink(missing_ok=True)
        raise AudioExtractionError(
            f"ffmpeg failed to extract audio from {source.name}: {completed.stderr.strip()[:400]}"
        )

    if not staging.is_file() or staging.stat().st_size == 0:
        staging.unlink(missing_ok=True)
        raise AudioExtractionError(f"{source.name} produced no audio — is there an audio track?")

    staging.replace(destination)
    log.info(
        "audio.extracted",
        source=source.name,
        size_mb=round(destination.stat().st_size / (1024 * 1024), 1),
    )
    return ExtractedAudio(path=destination, sample_rate=SAMPLE_RATE, channels=1)
