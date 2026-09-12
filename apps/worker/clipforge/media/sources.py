"""Source adapters: turning a URL or a path into media on disk.

Two implementations behind one interface. `LocalFileAdapter` exists so the entire
downstream pipeline is testable offline and in CI — it is not a test double, it
is a first-class way to use ClipForge, and on the free tier it is how most media
gets in. `YouTubeAdapter` wraps yt-dlp, which is the single most fragile
dependency in the project: it breaks whenever YouTube changes, so every call to
it is confined to this file.

**The failure taxonomy is the real work here.** yt-dlp reports almost everything
as one `DownloadError` with a human-readable message. Passing that through would
give the PWA a stack trace where it needs a sentence. So failures are classified
into :class:`IngestErrorCode`, and only the two that can plausibly succeed later
— rate limiting and network trouble — are marked retryable. Burning three
attempts on an age-restricted video wastes twenty minutes to learn nothing.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

from clipforge_contracts import IngestErrorCode, SourceProvider

from clipforge.media.ffprobe import MediaInfo, ProbeError, probe
from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = [
    "IngestError",
    "LocalFileAdapter",
    "SourceAdapter",
    "SourceIdentity",
    "SourceMetadata",
    "YouTubeAdapter",
    "classify_youtube_error",
    "content_hash",
    "select_adapter",
]

# Only these can plausibly succeed on a retry. Everything else is a property of
# the video itself and will fail identically three times in a row.
RETRYABLE_CODES = frozenset({IngestErrorCode.RATE_LIMITED, IngestErrorCode.NETWORK})


class IngestError(RuntimeError):
    """An ingest failure with a cause the user can act on."""

    def __init__(self, code: IngestErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES


@dataclass(frozen=True)
class SourceIdentity:
    """What a submission refers to, before anything is fetched.

    Derived without network access, so a malformed URL fails instantly rather
    than after a timeout, and so dedupe can happen before a download starts.
    """

    provider: SourceProvider
    external_id: str
    canonical_url: str | None = None


@dataclass(frozen=True)
class SourceMetadata:
    """What is known about a source before it is on disk."""

    title: str | None = None
    channel: str | None = None
    duration_sec: float | None = None
    estimated_bytes: int | None = None


@dataclass(frozen=True)
class FetchedSource:
    """The result of getting media onto the disk."""

    path: Path
    media: MediaInfo
    content_hash: str
    metadata: SourceMetadata


class SourceAdapter(Protocol):
    """Turns a submission into media on disk."""

    provider: SourceProvider

    def identify(self, submission: str) -> SourceIdentity:
        """Parse a submission. Must not touch the network."""
        ...

    def fetch_metadata(self, identity: SourceIdentity) -> SourceMetadata: ...

    def fetch(self, identity: SourceIdentity, dest_dir: Path) -> FetchedSource: ...


# ─────────────────────────────────────────────────────────────────────────────
# Hashing
# ─────────────────────────────────────────────────────────────────────────────


def content_hash(path: Path, *, chunk_bytes: int = 1024 * 1024) -> str:
    """A stable digest of the file's bytes.

    Streamed in chunks rather than read whole: sources run to gigabytes, and the
    machine this runs on also has to hold a model in memory.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_bytes):
            digest.update(chunk)
    return digest.hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Local files
# ─────────────────────────────────────────────────────────────────────────────


class LocalFileAdapter:
    """Media already on this machine.

    Not a test fixture. On the free tier, where nothing is uploaded anyway, this
    is a perfectly ordinary way to use ClipForge — and it is what lets the
    integration tier run with no network at all (docs/PLAN.md §6).
    """

    provider = SourceProvider.LOCAL

    def __init__(self, *, ffprobe_bin: str = "ffprobe") -> None:
        self._ffprobe_bin = ffprobe_bin

    def identify(self, submission: str) -> SourceIdentity:
        raw = submission.removeprefix("file://")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = path.resolve()
        if not path.is_file():
            raise IngestError(IngestErrorCode.NOT_FOUND, f"no such file: {path}")
        return SourceIdentity(
            provider=SourceProvider.LOCAL,
            # The resolved path IS the identity: the same file submitted twice
            # must dedupe to one source.
            external_id=str(path),
            canonical_url=path.as_uri(),
        )

    def fetch_metadata(self, identity: SourceIdentity) -> SourceMetadata:
        path = Path(identity.external_id)
        try:
            media = probe(path, ffprobe_bin=self._ffprobe_bin)
        except ProbeError as exc:
            raise IngestError(IngestErrorCode.NO_SUITABLE_FORMAT, str(exc)) from exc
        return SourceMetadata(
            title=path.stem,
            channel=None,
            duration_sec=media.duration_sec,
            estimated_bytes=media.size_bytes,
        )

    def fetch(self, identity: SourceIdentity, dest_dir: Path) -> FetchedSource:
        """Read the file where it already is.

        Deliberately does **not** copy it into the workspace. The file is the
        user's, it may be very large, and duplicating it would double the disk
        cost of the one thing already straining the disk budget. The workspace
        GC correspondingly never evicts a local source — it does not own it.
        """
        del dest_dir
        path = Path(identity.external_id)
        try:
            media = probe(path, ffprobe_bin=self._ffprobe_bin)
        except ProbeError as exc:
            raise IngestError(IngestErrorCode.NO_SUITABLE_FORMAT, str(exc)) from exc

        if not media.has_video and not media.has_audio:
            raise IngestError(
                IngestErrorCode.NO_SUITABLE_FORMAT,
                f"{path.name} contains neither a video nor an audio track",
            )
        if not media.has_audio:
            raise IngestError(
                IngestErrorCode.NO_SUITABLE_FORMAT,
                f"{path.name} has no audio track, so it cannot be transcribed",
            )

        return FetchedSource(
            path=path,
            media=media,
            content_hash=content_hash(path),
            metadata=SourceMetadata(
                title=path.stem,
                channel=None,
                duration_sec=media.duration_sec,
                estimated_bytes=media.size_bytes,
            ),
        )


# ─────────────────────────────────────────────────────────────────────────────
# YouTube
# ─────────────────────────────────────────────────────────────────────────────

_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def parse_youtube_id(submission: str) -> str | None:
    """Extract a video id, or ``None`` if this is not a single-video URL.

    Playlists and channels are explicitly out of scope for Phase 3, and a URL
    that names one should be refused clearly rather than silently ingesting
    whichever video happens to be first.
    """
    candidate = submission.strip()
    if _VIDEO_ID.match(candidate):
        return candidate

    parsed = urlparse(candidate if "//" in candidate else f"https://{candidate}")
    host = (parsed.hostname or "").lower()
    if host not in _YOUTUBE_HOSTS:
        return None

    if host.endswith("youtu.be"):
        video_id = parsed.path.lstrip("/").split("/")[0]
        return video_id if _VIDEO_ID.match(video_id) else None

    if parsed.path == "/watch":
        values = parse_qs(parsed.query).get("v", [])
        return values[0] if values and _VIDEO_ID.match(values[0]) else None

    for prefix in ("/shorts/", "/embed/", "/v/", "/live/"):
        if parsed.path.startswith(prefix):
            video_id = parsed.path[len(prefix) :].split("/")[0]
            return video_id if _VIDEO_ID.match(video_id) else None

    return None


# yt-dlp reports nearly everything as one DownloadError carrying a sentence, so
# classification is substring matching. It is fragile by nature, which is exactly
# why it lives in one tested function instead of being scattered through the
# stage. Order matters: the first match wins.
_ERROR_PATTERNS: tuple[tuple[str, IngestErrorCode], ...] = (
    ("sign in to confirm your age", IngestErrorCode.AGE_RESTRICTED),
    ("age-restricted", IngestErrorCode.AGE_RESTRICTED),
    ("inappropriate for some users", IngestErrorCode.AGE_RESTRICTED),
    ("private video", IngestErrorCode.PRIVATE),
    ("members-only", IngestErrorCode.PRIVATE),
    ("join this channel", IngestErrorCode.PRIVATE),
    # Deliberately loose. yt-dlp says both "is not available in your country" and
    # "has not made this video available in your country"; matching the narrower
    # phrasing silently downgraded the second to UNKNOWN.
    ("available in your country", IngestErrorCode.GEO_BLOCKED),
    ("blocked it in your country", IngestErrorCode.GEO_BLOCKED),
    ("who has blocked it on copyright", IngestErrorCode.GEO_BLOCKED),
    ("is live", IngestErrorCode.LIVE_STREAM),
    ("live event will begin", IngestErrorCode.LIVE_STREAM),
    ("premieres in", IngestErrorCode.LIVE_STREAM),
    ("video unavailable", IngestErrorCode.NOT_FOUND),
    ("this video has been removed", IngestErrorCode.NOT_FOUND),
    ("does not exist", IngestErrorCode.NOT_FOUND),
    ("http error 429", IngestErrorCode.RATE_LIMITED),
    ("too many requests", IngestErrorCode.RATE_LIMITED),
    ("sign in to confirm you're not a bot", IngestErrorCode.RATE_LIMITED),
    ("requested format is not available", IngestErrorCode.NO_SUITABLE_FORMAT),
    ("no video formats found", IngestErrorCode.NO_SUITABLE_FORMAT),
    ("unable to download", IngestErrorCode.NETWORK),
    ("connection reset", IngestErrorCode.NETWORK),
    ("timed out", IngestErrorCode.NETWORK),
    ("temporary failure in name resolution", IngestErrorCode.NETWORK),
    ("no space left on device", IngestErrorCode.DISK_FULL),
)


def classify_youtube_error(message: str) -> IngestErrorCode:
    """Map a yt-dlp message to a cause a user can act on."""
    lowered = message.lower()
    for needle, code in _ERROR_PATTERNS:
        if needle in lowered:
            return code
    return IngestErrorCode.UNKNOWN


class YouTubeAdapter:
    """Ingest via yt-dlp.

    The format selection caps at 1080p and prefers a single pre-muxed stream, so
    the common case avoids a remux entirely. The fallback merges separate video
    and audio streams, which costs an ffmpeg pass but is the only way to get
    1080p from YouTube at all for most videos.
    """

    provider = SourceProvider.YOUTUBE

    # Pre-muxed first, then best-under-1080p merged, then whatever exists.
    FORMAT_SELECTOR = (
        "best[height<=1080][ext=mp4][acodec!=none][vcodec!=none]"
        "/bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]"
        "/best[height<=1080]"
        "/best"
    )

    def __init__(
        self, *, max_duration_sec: float | None = None, ffprobe_bin: str = "ffprobe"
    ) -> None:
        self._max_duration_sec = max_duration_sec
        self._ffprobe_bin = ffprobe_bin

    def identify(self, submission: str) -> SourceIdentity:
        video_id = parse_youtube_id(submission)
        if video_id is None:
            raise IngestError(
                IngestErrorCode.UNSUPPORTED_URL,
                f"not a single YouTube video URL: {submission!r}. "
                "Playlists and channels are not supported.",
            )
        return SourceIdentity(
            provider=SourceProvider.YOUTUBE,
            external_id=video_id,
            canonical_url=f"https://www.youtube.com/watch?v={video_id}",
        )

    def fetch_metadata(self, identity: SourceIdentity) -> SourceMetadata:
        info = self._extract(identity, download=False)
        return _metadata_from_info(info)

    def fetch(self, identity: SourceIdentity, dest_dir: Path) -> FetchedSource:
        dest_dir.mkdir(parents=True, exist_ok=True)

        info = self._extract(identity, download=False)
        metadata = _metadata_from_info(info)
        self._reject_unsuitable(info, metadata)

        downloaded = self._extract(identity, download=True, dest_dir=dest_dir)
        path = self._resolve_output(downloaded, dest_dir, identity)

        try:
            media = probe(path, ffprobe_bin=self._ffprobe_bin)
        except ProbeError as exc:
            raise IngestError(IngestErrorCode.NO_SUITABLE_FORMAT, str(exc)) from exc

        return FetchedSource(
            path=path,
            media=media,
            content_hash=content_hash(path),
            metadata=metadata,
        )

    # ── yt-dlp plumbing, confined to here ────────────────────────────────────

    def _options(self, *, download: bool, dest_dir: Path | None) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "format": self.FORMAT_SELECTOR,
            "merge_output_format": "mp4",
            "retries": 3,
            "skip_download": not download,
        }
        if dest_dir is not None:
            options["outtmpl"] = str(dest_dir / "%(id)s.%(ext)s")
        return options

    def _extract(
        self, identity: SourceIdentity, *, download: bool, dest_dir: Path | None = None
    ) -> dict[str, Any]:
        # Imported lazily: yt-dlp lives in the `media` extra, and the core install
        # must work without it so CI can run the unit tier on a plain runner.
        try:
            import yt_dlp
        except ImportError as exc:  # pragma: no cover - environment, not logic
            raise IngestError(
                IngestErrorCode.UNKNOWN,
                "yt-dlp is not installed. Run: uv sync --extra media",
            ) from exc

        url = identity.canonical_url or identity.external_id
        try:
            with yt_dlp.YoutubeDL(self._options(download=download, dest_dir=dest_dir)) as ydl:
                info = ydl.extract_info(url, download=download)
        except Exception as exc:
            code = classify_youtube_error(str(exc))
            log.warning("ingest.youtube_failed", code=code.value, error=str(exc)[:300])
            raise IngestError(code, _humanise(code, str(exc))) from exc

        if not isinstance(info, dict):
            raise IngestError(IngestErrorCode.UNKNOWN, "yt-dlp returned no metadata")
        return info

    def _reject_unsuitable(self, info: dict[str, Any], metadata: SourceMetadata) -> None:
        if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
            raise IngestError(
                IngestErrorCode.LIVE_STREAM,
                "This is a live or upcoming stream. Try again once it has finished.",
            )
        if (
            self._max_duration_sec is not None
            and metadata.duration_sec is not None
            and metadata.duration_sec > self._max_duration_sec
        ):
            raise IngestError(
                IngestErrorCode.TOO_LONG,
                f"This video is {metadata.duration_sec / 60:.0f} minutes long; the limit is "
                f"{self._max_duration_sec / 60:.0f}. Raise CLIPFORGE_MAX_SOURCE_DURATION_SEC to allow it.",
            )

    def _resolve_output(
        self, info: dict[str, Any], dest_dir: Path, identity: SourceIdentity
    ) -> Path:
        """Find what yt-dlp actually wrote.

        It reports the final path in `requested_downloads`, but the extension can
        differ from what was requested once a merge happens — so the reported
        path is preferred and a glob is the fallback.
        """
        downloads = info.get("requested_downloads")
        if isinstance(downloads, list) and downloads:
            first = downloads[0]
            if isinstance(first, dict):
                for key in ("filepath", "_filename"):
                    value = first.get(key)
                    if isinstance(value, str) and Path(value).is_file():
                        return Path(value)

        matches = sorted(dest_dir.glob(f"{identity.external_id}.*"))
        real = [m for m in matches if m.is_file() and not m.name.endswith(".part")]
        if not real:
            raise IngestError(
                IngestErrorCode.UNKNOWN,
                f"yt-dlp reported success but no file appeared in {dest_dir}",
            )
        return real[0]


def _metadata_from_info(info: dict[str, Any]) -> SourceMetadata:
    duration = info.get("duration")
    size = info.get("filesize") or info.get("filesize_approx")
    return SourceMetadata(
        title=info.get("title") if isinstance(info.get("title"), str) else None,
        channel=(
            info.get("channel") or info.get("uploader")
            if isinstance(info.get("channel") or info.get("uploader"), str)
            else None
        ),
        duration_sec=float(duration) if isinstance(duration, int | float) else None,
        estimated_bytes=int(size) if isinstance(size, int | float) else None,
    )


_MESSAGES: dict[IngestErrorCode, str] = {
    IngestErrorCode.AGE_RESTRICTED: "This video is age-restricted and cannot be downloaded without signing in.",
    IngestErrorCode.PRIVATE: "This video is private or members-only.",
    IngestErrorCode.GEO_BLOCKED: "This video is not available in this region.",
    IngestErrorCode.LIVE_STREAM: "This is a live or upcoming stream. Try again once it has finished.",
    IngestErrorCode.NOT_FOUND: "This video does not exist or has been removed.",
    IngestErrorCode.RATE_LIMITED: "YouTube is rate-limiting this machine. It will be retried.",
    IngestErrorCode.NO_SUITABLE_FORMAT: "No downloadable format was offered for this video.",
    IngestErrorCode.NETWORK: "The download could not complete because of a network problem.",
    IngestErrorCode.DISK_FULL: "The disk is full.",
    IngestErrorCode.UNSUPPORTED_URL: "That is not a single YouTube video URL.",
}


def _humanise(code: IngestErrorCode, raw: str) -> str:
    """A sentence for the PWA, keeping the raw text for the log.

    UNKNOWN deliberately passes the original through: an unclassified failure is
    one the taxonomy has not learned yet, and hiding its text would make that
    impossible to fix.
    """
    return _MESSAGES.get(code, raw.strip()[:400])


def select_adapter(
    submission: str,
    *,
    max_duration_sec: float | None = None,
    ffprobe_bin: str = "ffprobe",
) -> SourceAdapter:
    """Pick an adapter for a submission, without touching the network."""
    if parse_youtube_id(submission) is not None:
        return YouTubeAdapter(max_duration_sec=max_duration_sec, ffprobe_bin=ffprobe_bin)

    stripped = submission.removeprefix("file://")
    parsed = urlparse(submission)
    if parsed.scheme in {"http", "https"}:
        raise IngestError(
            IngestErrorCode.UNSUPPORTED_URL,
            f"Only YouTube videos and local files are supported: {submission!r}",
        )
    if Path(stripped).expanduser().is_file():
        return LocalFileAdapter(ffprobe_bin=ffprobe_bin)

    raise IngestError(
        IngestErrorCode.UNSUPPORTED_URL,
        f"Not a YouTube video URL, and no such local file: {submission!r}",
    )

# ─────────────────────────────────────────────────────────────────────────────
# Audio-only sources, for the MUSIC stage
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FetchedAudio:
    """A track on disk, and whatever the source called it."""

    path: Path
    title: str | None


def resolve_audio_source(
    submission: str, dest_dir: Path, *, ffmpeg: str = "ffmpeg"
) -> FetchedAudio:
    """Get the audio for a music submission — a YouTube URL, or a file here.

    Only ever the audio. Pulling the video of a music source would cost
    bandwidth and disk for something no part of this uses, and on a link that is
    an hour-long mix that is a meaningful amount of both. `bestaudio` and a
    `-vn` extraction are the whole difference.

    Deliberately separate from the ingest adapters above, which exist to
    identify, deduplicate and garbage-collect *source video*. A music track is
    not a Source: it is not clipped, not transcribed, and not tracked — it is
    fetched, used, and left in tmp for the workspace to sweep.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    local = Path(submission).expanduser()
    if local.is_file():
        return FetchedAudio(path=local, title=local.stem)

    if parse_youtube_id(submission) is None:
        raise IngestError(
            IngestErrorCode.UNSUPPORTED_URL,
            f"not a YouTube link or a file on this machine: {submission!r}",
        )

    # Imported here: yt-dlp is the `media` extra, and the worker must import
    # this module without it.
    import yt_dlp

    template = str(dest_dir / "music-%(id)s.%(ext)s")
    options = {
        "format": "bestaudio/best",
        "outtmpl": template,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        # m4a rather than mp3: no transcode when YouTube already serves AAC,
        # which it usually does, and ffmpeg reads it just as happily.
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "192"}
        ],
        "ffmpeg_location": ffmpeg,
    }

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(submission, download=True)
    except Exception as exc:  # yt-dlp raises many shapes; all mean 'no track'
        raise IngestError(
            classify_youtube_error(str(exc)),
            f"could not fetch audio: {exc}",
        ) from exc

    video_id = info.get("id")
    matches = sorted(dest_dir.glob(f"music-{video_id}.*"))
    if not matches:
        raise IngestError(
            IngestErrorCode.NO_SUITABLE_FORMAT,
            "the download reported success but produced no audio file",
        )

    return FetchedAudio(path=matches[0], title=info.get("title"))
