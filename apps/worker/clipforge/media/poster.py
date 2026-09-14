"""Poster frames and filmstrips — what a phone can actually see.

This module exists because of the free tier. With no Cloud Storage there is no
URL a phone can play, so the poster and filmstrip are not decoration: they are
*most of what a remote review has to go on*, alongside the hook, the score and
the transcript excerpt. See docs/adr/0009-spark-tier-local-artefacts.md.

They are stored base64-encoded in a Firestore subcollection, which imposes a hard
constraint: a document cannot exceed 1 MiB, and base64 inflates by a third. So
the images are deliberately small, and the encoder *verifies* the result fits
rather than assuming it will — a poster that overflowed would fail the write at
the very end of a job that had already spent GPU minutes.

The filmstrip is four frames tiled into one image. A single still cannot
distinguish a clip where someone is talking from one where nothing happens; four
frames across the clip's span can.
"""

from __future__ import annotations

import base64
import subprocess
from dataclasses import dataclass
from pathlib import Path

from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = ["FIRESTORE_DOCUMENT_LIMIT", "PosterError", "PosterImages", "extract_poster"]

# Firestore's hard per-document ceiling.
FIRESTORE_DOCUMENT_LIMIT = 1_048_576
# The budget the render stage must hit. Half the ceiling, so the rest of the
# document — ids, timestamps, dimensions — can never push it over.
PREVIEW_BUDGET_BYTES = FIRESTORE_DOCUMENT_LIMIT // 2

POSTER_WIDTH = 360
FILMSTRIP_TILE_WIDTH = 240
FILMSTRIP_FRAMES = 4


class PosterError(RuntimeError):
    """A preview image could not be produced, or would not fit."""


@dataclass(frozen=True)
class PosterImages:
    poster_base64: str
    filmstrip_base64: str | None
    width_px: int
    height_px: int

    @property
    def byte_size(self) -> int:
        return len(self.poster_base64) + len(self.filmstrip_base64 or "")


def extract_poster(
    clip: Path,
    *,
    duration_sec: float,
    work_dir: Path,
    ffmpeg_bin: str = "ffmpeg",
    quality: int = 6,
) -> PosterImages:
    """Produce a poster frame and a four-frame filmstrip from a rendered clip.

    Read from the *rendered* clip rather than the source, so the preview shows
    what the reviewer would actually watch — correct crop, correct captions
    burned in. A preview taken from the source would show a differently-framed
    image without captions, which is worse than useless for judging a clip.
    """
    work_dir.mkdir(parents=True, exist_ok=True)

    # A third of the way in. The very first frame is often a cut point, a fade,
    # or someone mid-blink.
    poster_at = max(0.0, min(duration_sec / 3.0, max(duration_sec - 0.1, 0.0)))
    poster_path = work_dir / "poster.jpg"
    _run(
        [
            ffmpeg_bin,
            "-y",
            "-v",
            "error",
            "-ss",
            f"{poster_at:.3f}",
            "-i",
            str(clip),
            "-frames:v",
            "1",
            "-vf",
            f"scale={POSTER_WIDTH}:-2",
            "-q:v",
            str(quality),
            str(poster_path),
        ],
        what="poster frame",
    )

    filmstrip_path = work_dir / "filmstrip.jpg"
    filmstrip_b64: str | None = None
    if duration_sec > 2.0:
        try:
            _run(
                [
                    ffmpeg_bin,
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    str(clip),
                    "-vf",
                    (
                        # Evenly spaced across the clip, tiled in one row.
                        f"select='not(mod(n,{max(1, int(duration_sec * 25 / FILMSTRIP_FRAMES))}))',"
                        f"scale={FILMSTRIP_TILE_WIDTH}:-2,tile={FILMSTRIP_FRAMES}x1"
                    ),
                    "-frames:v",
                    "1",
                    "-q:v",
                    str(quality + 2),
                    str(filmstrip_path),
                ],
                what="filmstrip",
            )
            filmstrip_b64 = _encode(filmstrip_path)
        except PosterError:
            # A filmstrip is a nicety; a poster is not. A short or oddly-encoded
            # clip that defeats the tile filter must not fail the whole render.
            log.warning("poster.filmstrip_failed", clip=clip.name)

    poster_b64 = _encode(poster_path)
    width, height = _dimensions(poster_path, ffmpeg_bin)

    images = PosterImages(
        poster_base64=poster_b64,
        filmstrip_base64=filmstrip_b64,
        width_px=width,
        height_px=height,
    )

    if images.byte_size > PREVIEW_BUDGET_BYTES:
        # Drop the filmstrip first — it is the optional half — and only then give
        # up. Failing here would waste an entire completed render.
        log.warning("poster.over_budget", bytes=images.byte_size)
        images = PosterImages(
            poster_base64=poster_b64,
            filmstrip_base64=None,
            width_px=width,
            height_px=height,
        )
        if images.byte_size > PREVIEW_BUDGET_BYTES:
            raise PosterError(
                f"poster is {images.byte_size} bytes, over the "
                f"{PREVIEW_BUDGET_BYTES} budget for a Firestore document"
            )

    return images


def _run(argv: list[str], *, what: str) -> None:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, capture_output=True, text=True, timeout=120, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise PosterError(f"could not extract the {what}: {exc}") from exc
    if completed.returncode != 0:
        raise PosterError(f"could not extract the {what}: {completed.stderr.strip()[:300]}")


def _encode(path: Path) -> str:
    if not path.is_file() or path.stat().st_size == 0:
        raise PosterError(f"{path.name} was not produced")
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _dimensions(path: Path, ffmpeg_bin: str) -> tuple[int, int]:
    """Read the poster's real dimensions rather than assuming the scale held.

    Asks ffprobe for the stream's width and height and nothing else, rather than
    going through :func:`clipforge.media.ffprobe.probe`. That function is built
    for *media* and deliberately refuses a file with no duration, because every
    one of its other callers slices time out of what it describes — and a still
    image has no duration to report.

    Which sounds academic and is not. ffmpeg chooses a demuxer per file: a
    detailed JPEG is read by `image2`, which reports a nominal 0.04s, while a
    simpler one is read by `jpeg_pipe`, which reports `N/A`. So the strict probe
    succeeded or failed depending on *how busy the poster frame happened to be*,
    and on failure this returned a height of 0 — which then failed `ClipPreview`
    validation and took down a render that had already completed. A flat green
    pitch was enough to trigger it; a test pattern was not, which is why it
    survived the test suite.

    The dimensions were never in doubt in either case: they are on the stream,
    and this asks for exactly them.
    """
    ffprobe_bin = ffmpeg_bin.replace("ffmpeg", "ffprobe")
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                ffprobe_bin,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0:s=x",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return POSTER_WIDTH, 0

    width, _, height = completed.stdout.strip().partition("x")
    try:
        return int(width) or POSTER_WIDTH, int(height)
    except ValueError:
        # Nothing usable came back. The caller treats a zero height as "no
        # preview" rather than writing one that cannot be rendered.
        return POSTER_WIDTH, 0
