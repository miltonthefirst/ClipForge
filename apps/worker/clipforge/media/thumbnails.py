"""A small picture for each thing in the library.

## Why base64 in Firestore and not a file path

Because the Sources page has to work on a phone, and the phone cannot reach the
worker's disk. `Source.posterPath` records where the file is for the machine
that holds it; this is what everyone else sees. It is the same trade
`ClipPreview` already makes, for the same reason and at the same size — a
subcollection document, so listing forty sources does not drag forty images with
it (docs/adr/0009-spark-tier-local-artefacts.md).

## Why a track's picture is its cover art and not a waveform

A waveform is a picture of a signal. Nobody recognises a bed they chose last
week from its envelope, and a row of forty grey scribbles is a list you have to
read rather than look at. Cover art is what the reviewer saw on YouTube when
they picked it. When a file carries none there is simply no preview, which the
list renders as a lettered tile rather than as a broken image — an honest blank
beats a generated one that pretends to identify something.
"""

from __future__ import annotations

import base64
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from clipforge_contracts import SourceKind, SourcePreview

from clipforge.media.poster import PosterError
from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = ["THUMBNAIL_TIMEOUT_S", "build_source_preview"]

# One frame out of a file that is already on this disk. Generous enough for a
# cold cache on a spinning disk, short enough that a wedged ffmpeg cannot hold
# an ingest open — the bound every other subprocess in this package carries.
THUMBNAIL_TIMEOUT_S = 60.0

# Wide enough to read a scoreboard in, small enough that forty of them are a
# page rather than a download. ~15-25 KB each at quality 6.
_WIDTH_PX = 320


@dataclass(frozen=True)
class _Encoded:
    base64_jpeg: str
    width_px: int
    height_px: int
    byte_size: int


def build_source_preview(
    source_id: str,
    *,
    kind: SourceKind,
    path: Path,
    duration_sec: float | None,
    work_dir: Path,
    ffmpeg_bin: str = "ffmpeg",
) -> SourcePreview | None:
    """A thumbnail for one source, or None when the file offers no picture.

    Returns rather than raises on every failure. A thumbnail is the nicest part
    of the Sources page and none of an ingest's job: a source that downloaded
    correctly must not be recorded as failed because one extra ffmpeg call did
    not produce a JPEG.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    staged = work_dir / f"thumb-{source_id}.jpg"
    staged.unlink(missing_ok=True)

    try:
        if kind is SourceKind.MUSIC:
            made = _cover_art(path, staged, ffmpeg_bin)
        else:
            made = _frame(path, staged, duration_sec or 0.0, ffmpeg_bin)
        if not made:
            return None

        encoded = _encode(staged, ffmpeg_bin)
        return SourcePreview(
            source_id=source_id,
            poster_base64=encoded.base64_jpeg,
            width_px=encoded.width_px,
            height_px=encoded.height_px,
            byte_size=encoded.byte_size,
            created_at=datetime.now(UTC),
        )
    except (OSError, PosterError, subprocess.SubprocessError) as exc:
        log.info("thumbnail.skipped", source=source_id, error=str(exc)[:200])
        return None
    finally:
        staged.unlink(missing_ok=True)


def _frame(video: Path, destination: Path, duration_sec: float, ffmpeg_bin: str) -> bool:
    """One frame, a third of the way in.

    The same offset `extract_poster` uses and for the same reason: the first
    frame of a broadcast is a cut, a fade, or a caption card, and none of those
    tell you which match you are looking at.
    """
    at = max(0.0, min(duration_sec / 3.0, max(duration_sec - 0.1, 0.0)))
    return _run(
        [
            ffmpeg_bin,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-ss",
            str(at),
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            f"scale={_WIDTH_PX}:-2",
            "-q:v",
            "6",
            str(destination),
        ],
        destination,
    )


def _cover_art(track: Path, destination: Path, ffmpeg_bin: str) -> bool:
    """The picture embedded in the file, if it has one.

    `-map 0:v` rather than a frame extraction: in an m4a the artwork *is* a
    video stream of one frame, and asking for it by stream is the difference
    between reading the picture and asking ffmpeg to invent one from audio.
    A track with no artwork fails this call, which is the answer, not an error.
    """
    return _run(
        [
            ffmpeg_bin,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(track),
            "-map",
            "0:v",
            "-frames:v",
            "1",
            "-vf",
            f"scale={_WIDTH_PX}:-2",
            "-q:v",
            "6",
            str(destination),
        ],
        destination,
    )


def _run(argv: list[str], destination: Path) -> bool:
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, capture_output=True, text=True, timeout=THUMBNAIL_TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired:
        log.info("thumbnail.timed_out", after_s=THUMBNAIL_TIMEOUT_S)
        return False
    if done.returncode != 0:
        return False
    return destination.is_file() and destination.stat().st_size > 0


def _encode(path: Path, ffmpeg_bin: str) -> _Encoded:
    from clipforge.media.poster import _dimensions

    raw = path.read_bytes()
    width, height = _dimensions(path, ffmpeg_bin)
    return _Encoded(
        base64_jpeg=base64.b64encode(raw).decode("ascii"),
        width_px=width,
        height_px=height,
        byte_size=len(raw),
    )
