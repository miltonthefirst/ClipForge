"""A song someone likes, fetched only to be studied (docs/MUSIC-PLAN.md, M7).

A REFERENCE song is an original made in the style of one the owner liked. Style
is not anyone's property; a melody, lyrics and a recording are. So this fetches
the audio and what the video says about itself (title, channel, tags,
description), the caller measures and listens to it, and the file is deleted
before anything is made. The music model never hears it: it is given words
that describe the sound, so it cannot carry the melody across.

Audio only, at the lowest quality that still has a beat in it: nothing here is
ever played back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clipforge_contracts import IngestErrorCode

from clipforge.media.sources import (
    IngestError,
    YouTubeAdapter,
    _humanise,
    classify_youtube_error,
)
from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = ["LONGEST_REFERENCE_S", "Reference", "fetch_reference"]

# A song, not a mix or an album: past this it is refused before downloading.
LONGEST_REFERENCE_S = 15 * 60


@dataclass(frozen=True)
class Reference:
    """What was fetched: the audio, and what the video says about itself."""

    url: str
    audio: Path
    title: str
    channel: str
    duration_s: float | None
    description: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


def fetch_reference(url: str, dest_dir: Path) -> Reference:
    """Download a single video's audio and its details. Raises IngestError."""
    identity = YouTubeAdapter().identify(url)
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover - environment, not logic
        raise IngestError(
            IngestErrorCode.UNKNOWN, "yt-dlp is not installed. Run: uv sync --extra media"
        ) from exc

    dest_dir.mkdir(parents=True, exist_ok=True)
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "format": "worstaudio[abr>=48]/bestaudio/best",
        "outtmpl": str(dest_dir / "reference.%(ext)s"),
        "retries": 3,
    }
    canonical = identity.canonical_url or url
    try:
        with yt_dlp.YoutubeDL({**options, "skip_download": True}) as ydl:
            info = ydl.extract_info(canonical, download=False)
        if not isinstance(info, dict):
            raise IngestError(IngestErrorCode.UNKNOWN, "YouTube returned nothing for that link")
        if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
            raise IngestError(IngestErrorCode.LIVE_STREAM, "That is a live stream, not a song.")
        duration = info.get("duration")
        if isinstance(duration, (int, float)) and duration > LONGEST_REFERENCE_S:
            raise IngestError(
                IngestErrorCode.TOO_LONG,
                f"That video is {duration / 60:.0f} minutes long; a song of at most "
                f"{LONGEST_REFERENCE_S // 60} minutes is studied.",
            )
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.extract_info(canonical, download=True)
    except IngestError:
        raise
    except Exception as exc:
        code = classify_youtube_error(str(exc))
        log.warning("reference.fetch_failed", code=code.value, error=str(exc)[:300])
        raise IngestError(code, _humanise(code, str(exc))) from exc

    audio = next((p for p in dest_dir.glob("reference.*") if not p.name.endswith(".part")), None)
    if audio is None:
        raise IngestError(IngestErrorCode.NO_SUITABLE_FORMAT, "YouTube offered no audio for it")
    tags = info.get("tags") or []
    return Reference(
        url=canonical,
        audio=audio,
        title=str(info.get("title") or "")[:200],
        channel=str(info.get("channel") or info.get("uploader") or "")[:120],
        duration_s=float(duration) if isinstance(duration, (int, float)) else None,
        description=str(info.get("description") or "")[:1500],
        tags=tuple(str(tag)[:40] for tag in tags[:20] if isinstance(tag, str)),
    )
