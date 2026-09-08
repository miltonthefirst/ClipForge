"""Source adapters: URL parsing, and the ingest failure taxonomy.

Phase 3, exit criterion 4 — every failure mode maps to its own code, verified
against stubbed yt-dlp messages. That matters more than it looks: yt-dlp reports
almost everything as one `DownloadError` carrying a sentence, so without this
classification the PWA would show a stack trace where it needs "this video is
age-restricted".

The classification is substring matching and therefore fragile by nature. These
tests are what make the fragility *visible* — if yt-dlp rewords a message, one of
them fails instead of a user silently getting UNKNOWN.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from clipforge.media.sources import (
    IngestError,
    LocalFileAdapter,
    YouTubeAdapter,
    classify_youtube_error,
    content_hash,
    parse_youtube_id,
    select_adapter,
)
from clipforge_contracts import IngestErrorCode, SourceProvider

FIXTURES = Path(__file__).resolve().parents[2] / "assets" / "fixtures"
SAMPLE_AV = FIXTURES / "sample-av.mp4"
VIDEO_ONLY = FIXTURES / "video-only.mp4"

ffprobe_required = pytest.mark.skipif(not SAMPLE_AV.is_file(), reason="media fixtures are missing")


# ─────────────────────────────────────────────────────────────────────────────
# URL parsing — no network, so a malformed URL fails in microseconds
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com/watch?v=dQw4w9WgXcQ&t=42",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ",
        "https://www.youtube.com/live/dQw4w9WgXcQ",
        "youtube.com/watch?v=dQw4w9WgXcQ",
        "dQw4w9WgXcQ",
    ],
)
def test_every_youtube_url_shape_yields_the_same_id(url: str) -> None:
    assert parse_youtube_id(url) == "dQw4w9WgXcQ"


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/playlist?list=PLabc",
        "https://www.youtube.com/@somechannel",
        "https://www.youtube.com/watch?v=too-short",
        "https://vimeo.com/123456",
        "https://example.com/video.mp4",
        "not a url at all",
        "",
    ],
)
def test_non_single_video_urls_are_rejected(url: str) -> None:
    """Playlists and channels are explicitly out of scope. Silently ingesting
    whichever video happened to be first would be worse than refusing."""
    assert parse_youtube_id(url) is None


@pytest.mark.unit
def test_a_playlist_url_names_the_problem() -> None:
    with pytest.raises(IngestError) as caught:
        YouTubeAdapter().identify("https://www.youtube.com/playlist?list=PLabc")
    assert caught.value.code is IngestErrorCode.UNSUPPORTED_URL
    assert "laylist" in str(caught.value)


@pytest.mark.unit
def test_identify_canonicalises_the_url() -> None:
    identity = YouTubeAdapter().identify("https://youtu.be/dQw4w9WgXcQ?si=tracking")
    assert identity.provider is SourceProvider.YOUTUBE
    assert identity.external_id == "dQw4w9WgXcQ"
    assert identity.canonical_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 4 — the failure taxonomy
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users.",
            IngestErrorCode.AGE_RESTRICTED,
        ),
        (
            "ERROR: [youtube] abc: Private video. Sign in if you've been granted access",
            IngestErrorCode.PRIVATE,
        ),
        (
            "ERROR: [youtube] abc: Join this channel to get access to members-only content",
            IngestErrorCode.PRIVATE,
        ),
        (
            "ERROR: [youtube] abc: The uploader has not made this video available in your country",
            IngestErrorCode.GEO_BLOCKED,
        ),
        (
            "ERROR: [youtube] abc: Video unavailable. This video contains content from X, who has blocked it on copyright grounds",
            IngestErrorCode.GEO_BLOCKED,
        ),
        (
            "ERROR: [youtube] abc: This live event will begin in 3 hours",
            IngestErrorCode.LIVE_STREAM,
        ),
        ("ERROR: [youtube] abc: Premieres in 2 days", IngestErrorCode.LIVE_STREAM),
        ("ERROR: [youtube] abc: Video unavailable", IngestErrorCode.NOT_FOUND),
        (
            "ERROR: [youtube] abc: This video has been removed by the uploader",
            IngestErrorCode.NOT_FOUND,
        ),
        (
            "ERROR: unable to download video data: HTTP Error 429: Too Many Requests",
            IngestErrorCode.RATE_LIMITED,
        ),
        (
            "ERROR: [youtube] abc: Sign in to confirm you're not a bot. Use --cookies-from-browser",
            IngestErrorCode.RATE_LIMITED,
        ),
        (
            "ERROR: [youtube] abc: Requested format is not available",
            IngestErrorCode.NO_SUITABLE_FORMAT,
        ),
        (
            "ERROR: unable to download video data: <urlopen error timed out>",
            IngestErrorCode.NETWORK,
        ),
        (
            "ERROR: unable to write data: [Errno 28] No space left on device",
            IngestErrorCode.DISK_FULL,
        ),
        ("ERROR: something nobody has seen before", IngestErrorCode.UNKNOWN),
    ],
)
def test_yt_dlp_messages_map_to_actionable_codes(message: str, expected: IngestErrorCode) -> None:
    assert classify_youtube_error(message) is expected


@pytest.mark.unit
def test_classification_is_case_insensitive() -> None:
    """yt-dlp's capitalisation varies between extractors and versions."""
    assert classify_youtube_error("PRIVATE VIDEO") is IngestErrorCode.PRIVATE


@pytest.mark.unit
@pytest.mark.parametrize(
    ("code", "retryable"),
    [
        (IngestErrorCode.RATE_LIMITED, True),
        (IngestErrorCode.NETWORK, True),
        (IngestErrorCode.AGE_RESTRICTED, False),
        (IngestErrorCode.PRIVATE, False),
        (IngestErrorCode.GEO_BLOCKED, False),
        (IngestErrorCode.NOT_FOUND, False),
        (IngestErrorCode.TOO_LONG, False),
        (IngestErrorCode.LIVE_STREAM, False),
        (IngestErrorCode.UNSUPPORTED_URL, False),
        (IngestErrorCode.UNKNOWN, False),
    ],
)
def test_only_transient_failures_are_worth_retrying(code: IngestErrorCode, retryable: bool) -> None:
    """Burning three attempts on an age-restricted video wastes twenty minutes to
    learn nothing. Only rate limiting and network trouble can change on their own."""
    assert IngestError(code, "x").retryable is retryable


@pytest.mark.unit
def test_every_error_code_is_covered_by_the_retry_policy() -> None:
    """A new code added to the contract must be classified deliberately, not
    inherit 'not retryable' by accident."""
    for code in IngestErrorCode:
        # Exercises the property for every member; the assertion is that none raise.
        assert isinstance(IngestError(code, "x").retryable, bool)


# ─────────────────────────────────────────────────────────────────────────────
# Local files
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
@ffprobe_required
def test_a_local_file_is_identified_by_its_resolved_path() -> None:
    """The path IS the identity, so the same file submitted twice dedupes."""
    identity = LocalFileAdapter().identify(str(SAMPLE_AV))
    assert identity.provider is SourceProvider.LOCAL
    assert Path(identity.external_id) == SAMPLE_AV.resolve()


@pytest.mark.unit
def test_a_missing_local_file_is_reported_as_not_found() -> None:
    with pytest.raises(IngestError) as caught:
        LocalFileAdapter().identify("./definitely-not-here.mp4")
    assert caught.value.code is IngestErrorCode.NOT_FOUND


@pytest.mark.unit
@ffprobe_required
def test_a_local_file_reports_its_real_duration_and_streams() -> None:
    adapter = LocalFileAdapter()
    fetched = adapter.fetch(adapter.identify(str(SAMPLE_AV)), Path("unused"))

    assert 4.5 < fetched.media.duration_sec < 5.5
    assert fetched.media.has_video
    assert fetched.media.has_audio
    assert fetched.content_hash


@pytest.mark.unit
@ffprobe_required
def test_a_local_file_is_never_copied_into_the_workspace(tmp_path: Path) -> None:
    """The file is the user's and may be very large. Duplicating it would double
    the disk cost of the one thing already straining the disk budget."""
    adapter = LocalFileAdapter()
    fetched = adapter.fetch(adapter.identify(str(SAMPLE_AV)), tmp_path)

    assert fetched.path == SAMPLE_AV.resolve()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.unit
@ffprobe_required
def test_a_video_with_no_audio_is_refused_with_a_reason() -> None:
    """It cannot be transcribed, so the whole pipeline is pointless — better to
    say so at ingest than to fail in TRANSCRIBE twenty minutes later."""
    adapter = LocalFileAdapter()
    with pytest.raises(IngestError) as caught:
        adapter.fetch(adapter.identify(str(VIDEO_ONLY)), Path("unused"))

    assert caught.value.code is IngestErrorCode.NO_SUITABLE_FORMAT
    assert "audio" in str(caught.value)


# ─────────────────────────────────────────────────────────────────────────────
# Hashing and adapter selection
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_content_hash_is_stable_and_distinguishes_files(tmp_path: Path) -> None:
    first = tmp_path / "a.bin"
    second = tmp_path / "b.bin"
    first.write_bytes(b"clipforge" * 1000)
    second.write_bytes(b"clipforgf" * 1000)

    assert content_hash(first) == content_hash(first)
    assert content_hash(first) != content_hash(second)


@pytest.mark.unit
def test_content_hash_spans_chunk_boundaries(tmp_path: Path) -> None:
    """Streamed in chunks because sources run to gigabytes; a bug in the loop
    would only show on a file larger than one chunk."""
    path = tmp_path / "big.bin"
    path.write_bytes(b"\x00" * (3 * 1024 * 1024 + 17))
    assert content_hash(path, chunk_bytes=1024) == content_hash(path, chunk_bytes=1024 * 1024)


@pytest.mark.unit
@ffprobe_required
def test_adapter_selection_routes_by_submission_shape() -> None:
    assert isinstance(select_adapter("https://youtu.be/dQw4w9WgXcQ"), YouTubeAdapter)
    assert isinstance(select_adapter(str(SAMPLE_AV)), LocalFileAdapter)


@pytest.mark.unit
def test_an_arbitrary_http_url_is_refused_rather_than_attempted() -> None:
    with pytest.raises(IngestError) as caught:
        select_adapter("https://example.com/video.mp4")
    assert caught.value.code is IngestErrorCode.UNSUPPORTED_URL
