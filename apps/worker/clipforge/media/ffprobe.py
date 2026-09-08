"""Reading media metadata with ffprobe.

ffprobe is the authority on what a file actually contains, as opposed to what its
extension or its downloader claimed. That distinction matters at ingest: a
`.mp4` that is really a fragmented stream, or a "video" with no video track, both
fail much later and much less clearly if taken on trust here.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = ["MediaInfo", "ProbeError", "probe"]


class ProbeError(RuntimeError):
    """ffprobe could not read the file, or read something unusable."""


@dataclass(frozen=True)
class MediaInfo:
    """The parts of ffprobe's output the pipeline actually uses."""

    duration_sec: float
    width: int | None
    height: int | None
    has_video: bool
    has_audio: bool
    format_name: str
    size_bytes: int
    video_codec: str | None = None
    audio_codec: str | None = None
    fps: float | None = None

    @property
    def is_vertical(self) -> bool:
        return bool(self.width and self.height and self.height > self.width)


def probe(path: Path, *, ffprobe_bin: str = "ffprobe", timeout_s: float = 60.0) -> MediaInfo:
    """Read a media file's streams and format.

    Raises :class:`ProbeError` rather than returning a partial result: every
    caller needs the duration, and a `MediaInfo` with a plausible-looking zero in
    it would propagate silently into clip boundaries.
    """
    if not path.is_file():
        raise ProbeError(f"no such file: {path}")

    argv = [
        ffprobe_bin,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except FileNotFoundError as exc:
        raise ProbeError(
            f"{ffprobe_bin} not found on PATH. ffmpeg is a hard dependency; run tools/doctor.ps1"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe timed out after {timeout_s}s on {path}") from exc

    if completed.returncode != 0:
        raise ProbeError(f"ffprobe failed on {path}: {completed.stderr.strip()[:400]}")

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe returned unparseable JSON for {path}") from exc

    return _parse(payload, path)


def _parse(payload: dict[str, object], path: Path) -> MediaInfo:
    fmt = payload.get("format")
    if not isinstance(fmt, dict):
        raise ProbeError(f"ffprobe reported no format block for {path}")

    streams = payload.get("streams")
    streams = streams if isinstance(streams, list) else []

    video = next(
        (s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"), None
    )
    audio = next(
        (s for s in streams if isinstance(s, dict) and s.get("codec_type") == "audio"), None
    )

    # Duration lives on the format block, but some containers only carry it per
    # stream — so fall back rather than reporting a confident zero.
    duration = _as_float(fmt.get("duration"))
    if duration is None and video is not None:
        duration = _as_float(video.get("duration"))
    if duration is None:
        raise ProbeError(f"ffprobe reported no duration for {path}")

    return MediaInfo(
        duration_sec=duration,
        width=_as_int(video.get("width")) if video else None,
        height=_as_int(video.get("height")) if video else None,
        has_video=video is not None,
        has_audio=audio is not None,
        format_name=str(fmt.get("format_name", "unknown")),
        size_bytes=_as_int(fmt.get("size")) or path.stat().st_size,
        video_codec=str(video.get("codec_name")) if video else None,
        audio_codec=str(audio.get("codec_name")) if audio else None,
        fps=_parse_fps(str(video.get("r_frame_rate", ""))) if video else None,
    )


def _as_float(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int | None:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _parse_fps(rate: str) -> float | None:
    """ffprobe reports frame rate as a rational, e.g. ``30000/1001``."""
    if "/" not in rate:
        return _as_float(rate)
    numerator, _, denominator = rate.partition("/")
    try:
        den = float(denominator)
        return float(numerator) / den if den else None
    except ValueError:
        return None
