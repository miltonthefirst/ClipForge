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
from urllib.parse import ParseResult, parse_qs, urlparse

from clipforge_contracts import IngestErrorCode, SourceProvider

from clipforge.media.ffprobe import MediaInfo, ProbeError, probe
from clipforge.media.toolchain import resolve_toolchain, yt_dlp_location
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
    "resolve_audio_source",
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


def _parse_url(candidate: str) -> ParseResult | None:
    """`urlparse`, with a malformed URL answered rather than raised.

    `urlparse` throws on a few shapes — `https://[oops` is `ValueError: Invalid
    IPv6 URL` — and both parsers here are documented as pure functions that
    answer a question about a string. A ValueError escaping one of them reaches
    the stage as an unclassified crash instead of the "that is not a link"
    sentence the PWA knows how to show, and the browser-side check disagrees
    because it swallows the same input. Nothing parseable is lost: a string
    urlparse cannot read names no YouTube video and no playlist.
    """
    try:
        return urlparse(candidate if "//" in candidate else f"https://{candidate}")
    except ValueError:
        return None


def parse_youtube_id(submission: str) -> str | None:
    """Extract a video id, or ``None`` if this is not a single-video URL.

    Playlists and channels are explicitly out of scope for Phase 3, and a URL
    that names one should be refused clearly rather than silently ingesting
    whichever video happens to be first.
    """
    candidate = submission.strip()
    if _VIDEO_ID.match(candidate):
        return candidate

    parsed = _parse_url(candidate)
    if parsed is None:
        return None
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


def parse_playlist_id(submission: str) -> str | None:
    """Extract the playlist or mix id a link names, or ``None`` for none.

    Pure and offline, exactly like :func:`parse_youtube_id`: a link that cannot
    be used should be refused in microseconds rather than halfway through a
    download. Nothing looked at `list=` before, and a MUSIC job walked an
    autoplay mix for half an hour — 190 tracks, 905 MB — to fetch one bed.

    Any `list=` counts, whether it names a mix (`RD...`), an ordinary playlist
    (`PL...`), Watch Later or Liked: they are all "more than the one video you
    asked for". Only on a YouTube host, though — some other site's `list=`
    parameter is its own business and must not be reported as a playlist here.
    """
    parsed = _parse_url(submission.strip())
    if parsed is None or (parsed.hostname or "").lower() not in _YOUTUBE_HOSTS:
        return None

    # parse_qs drops a blank value, so `?list=` alone arrives as no value at all;
    # the strip() catches `?list=%20`, which is just as empty.
    for value in parse_qs(parsed.query).get("list", []):
        if value.strip():
            return value.strip()
    return None


def _playlist_refusal(submission: str) -> IngestError:
    """Refuse a playlist link, and say which single-video URL to use instead.

    Refusing on its own would turn one paste into five steps: open the link,
    find the video, copy its URL, come back, paste again. The video id is
    already in the link that was submitted, so the fix can just be handed over.
    A `/playlist?list=...` URL names no video, so there it cannot be.
    """
    video_id = parse_youtube_id(submission)
    if video_id is None:
        return IngestError(
            IngestErrorCode.UNSUPPORTED_URL,
            f"that is a playlist, not a single video: {submission!r}. "
            "Submit the link to the one video you want.",
        )
    return IngestError(
        IngestErrorCode.UNSUPPORTED_URL,
        f"that is a playlist, not a single video: {submission!r}. "
        "To use only the video it names, submit "
        f"https://www.youtube.com/watch?v={video_id}",
    )


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
        # Before parsing, because `watch?v=X&list=Y` yields a perfectly good
        # video id and would otherwise be ingested as one video — making the
        # refusal below a promise this code did not keep.
        if parse_playlist_id(submission) is not None:
            raise _playlist_refusal(submission)

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
    # A playlist link is routed here too, so `identify` is what refuses it and
    # names the video to use instead. `youtube.com/playlist?list=...` has no
    # video id, so without this it reached the http branch below and was told
    # "Only YouTube videos and local files are supported" — true of a random
    # mp3, wrong and unhelpful about a YouTube URL.
    if parse_youtube_id(submission) is not None or parse_playlist_id(submission) is not None:
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


class _YtDlpLog:
    """Send yt-dlp's own diagnostics to the worker log.

    Not decoration. The bug this replaces announced itself in a yt-dlp warning
    — *"ffmpeg-location ffmpeg does not exist! Continuing without ffmpeg"* —
    and `no_warnings: True` threw it away, so what reached the operator was
    `ffprobe and ffmpeg not found` with nothing to say why. Warnings and errors
    from a dependency this fragile are exactly the ones worth keeping.

    `debug` is where yt-dlp sends everything it would otherwise print, so it is
    forwarded rather than dropped — with the per-chunk `[download]` chatter
    filtered out, which is the only part that was ever actually noise. Dropping
    the lot cost us the sentence *"Downloading playlist RDD2XUoPg3-KY — add
    --no-playlist to download just the video"*, which yt-dlp emitted once a
    minute for half an hour while a MUSIC job quietly fetched 190 tracks.
    """

    def debug(self, message: str) -> None:
        text = message.strip()
        if not text or text.startswith(("[download]", "[debug]")):
            return None
        log.debug("ytdlp.debug", message=text)
        return None

    def info(self, message: str) -> None:
        return None

    def warning(self, message: str) -> None:
        log.warning("ytdlp.warning", message=message.strip())

    def error(self, message: str) -> None:
        log.error("ytdlp.error", message=message.strip())


# What a finished track is called once it is this machine's to keep, and what
# yt-dlp is told to write while it is still working. Two names rather than one:
# see :func:`_cached_audio`.
_CACHED = "music-"
_FETCHING = "fetching-"


def _cached_audio(dest_dir: Path, video_id: str, *, prefix: str = _CACHED) -> Path | None:
    """A track this machine already fetched, or ``None`` to go and get it.

    yt-dlp has a skip of its own for a file it has already downloaded and it
    never fires here, because of how the m4a extraction is put together.
    `YoutubeDL.existing_video_file` looks for the output under the extension of
    the *downloaded format* — `<id>.webm`, usually — since `final_ext` is only
    ever set by yt-dlp's command-line front end and not by the library API this
    module calls. But `FFmpegExtractAudioPP.run` returns that pre-conversion
    file in its files-to-delete list, and `YoutubeDL.run_pp` deletes everything
    in that list unless `keepvideo` is set. So the only file left after a
    successful fetch is the `.m4a`, and the skip is looking for a name nothing
    will ever have. (Read against yt-dlp 2026.08.19.)

    That cost tens of seconds per re-mix, which is the whole of the wait a
    reviewer sits through when a remake carries its music forward.

    **The cache only ever looks at a name this module finished writing.**
    Filtering `.part` and `.ytdl` does not cover the window that matters:
    `FFmpegExtractAudioPP.run` transcodes straight into the output name whenever
    the downloaded extension differs, with no `.part` marker over that stretch,
    so a worker killed mid-transcode left a truncated but non-empty file under
    exactly the name a cache hit looks for — and that hit is what stops the
    fetch that would replace it, so the machine was wedged on half a track
    forever. yt-dlp therefore downloads under `_FETCHING` and the finished file
    is renamed into `_CACHED`, which makes "finished" a fact this module
    establishes rather than one it infers afterwards. Probing the candidate
    instead would only ask whether the bytes decode, and would put ffprobe back
    on the one path that deliberately runs before the toolchain lookup.
    """
    for match in sorted(dest_dir.glob(f"{prefix}{video_id}.*")):
        if match.suffix in {".part", ".ytdl"} or not match.is_file():
            continue
        if match.stat().st_size > 0:
            return match
    return None


def resolve_audio_source(
    submission: str, dest_dir: Path, *, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe"
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

    The extraction runs through ffmpeg, so the tools are located **before** the
    download rather than discovered missing by a postprocessor afterwards. A
    machine with no ffprobe should learn that in a second, not after pulling
    forty megabytes of an hour-long mix.

    A track already in ``dest_dir`` is returned without going near the network
    or yt-dlp at all — see :func:`_cached_audio` for why yt-dlp's own skip does
    not cover this.
    """
    local = Path(submission).expanduser()
    if local.is_file():
        return FetchedAudio(path=local, title=local.stem)

    # Ahead of the refusal below, which would otherwise catch a
    # `youtube.com/playlist?list=...` URL — parse_youtube_id rightly finds no
    # video id in one — and tell the user it is not a YouTube link, which it is.
    if parse_playlist_id(submission) is not None:
        raise _playlist_refusal(submission)

    video_id = parse_youtube_id(submission)
    if video_id is None:
        raise IngestError(
            IngestErrorCode.UNSUPPORTED_URL,
            f"not a YouTube link or a file on this machine: {submission!r}",
        )

    # Before the toolchain check and the yt-dlp import, both of which are only
    # needed to *fetch*: a re-mix of a track that is already here should not
    # depend on either.
    cached = _cached_audio(dest_dir, video_id)
    if cached is not None:
        log.info("music.track_cached", track=cached.name)
        # **No title, rather than the stem.** The file is named after the video
        # id, so the stem is `music-dQw4w9WgXcQ` — which is not what anything
        # called the track. `RemakeStage._score` falls back to the title already
        # on the clip only when this is None, so returning the stem overwrote a
        # title the reviewer had seen as "Slow Burn" with the video id on three
        # screens, on the first remake that carried the music. None is already
        # what `trackTitle` means for "the source did not say". The local-file
        # branch above keeps its stem for the opposite reason: that name is one
        # the reviewer typed.
        return FetchedAudio(path=cached, title=None)

    tools = resolve_toolchain(ffmpeg, ffprobe)
    tools.require("Fetching audio for a music track")

    # Last, so that a link refused above leaves nothing behind. A submission
    # that is never downloaded should not create the directory it would have
    # been downloaded into.
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Imported here: yt-dlp is the `media` extra, and the worker must import
    # this module without it.
    import yt_dlp

    template = str(dest_dir / f"{_FETCHING}%(id)s.%(ext)s")
    options: dict[str, Any] = {
        "format": "bestaudio/best",
        "outtmpl": template,
        "quiet": True,
        "noprogress": True,
        # Belt and braces behind the guard above: a `list=` submission never
        # reaches this call any more, but yt-dlp's default on one is to download
        # every entry, and that default cost 190 tracks once. The ingest adapter
        # has always set it; the music path is the one that went without.
        "noplaylist": True,
        "logger": _YtDlpLog(),
        # m4a rather than mp3: no transcode when YouTube already serves AAC,
        # which it usually does, and ffmpeg reads it just as happily.
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "192"}
        ],
    }
    # Set only when it says something PATH does not already say. yt-dlp
    # path-checks this value and, on a miss, abandons ffmpeg *and* ffprobe
    # rather than falling back — so the absent key is the safe one.
    location = yt_dlp_location(tools)
    if location is not None:
        options["ffmpeg_location"] = location

    # The canonical single-video URL, not the submission: a submission may be a
    # bare id or carry tracking parameters, and the glob below looks for a file
    # named by the video id, so the URL handed over has to be the one yt-dlp
    # will name the output after.
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
    except Exception as exc:  # yt-dlp raises many shapes; all mean 'no track'
        raise IngestError(
            classify_youtube_error(str(exc)),
            f"could not fetch audio: {exc}",
        ) from exc

    # The same rule the cache check uses, so "a finished track" means one thing
    # here and not two: a `.part` left by an earlier killed download must not be
    # picked up as this one's output either. Under the staging prefix, so
    # nothing that reaches this line can already have been served as a cached
    # track. The postprocessor's `.m4a` sorts ahead of every container YouTube
    # serves audio in, so a pre-conversion leftover cannot be preferred to it.
    fetched = _cached_audio(dest_dir, video_id, prefix=_FETCHING)
    if fetched is None:
        raise IngestError(
            IngestErrorCode.NO_SUITABLE_FORMAT,
            "the download reported success but produced no audio file",
        )

    # The rename is the commit, and it is the whole of the fix: same directory,
    # so it is atomic, and the name it lands under is the only one a later job
    # will look for.
    finished = dest_dir / f"{_CACHED}{video_id}{fetched.suffix}"
    fetched.replace(finished)
    return FetchedAudio(path=finished, title=info.get("title"))
