"""Finding ffmpeg and ffprobe, and never lying to yt-dlp about where they are.

These exist because of a live failure. A MUSIC job died with *"Postprocessing:
ffprobe and ffmpeg not found"* on a machine that had just rendered clips with
ffmpeg seconds earlier. The cause was that `resolve_audio_source` passed
`Settings.ffmpeg_bin` — the bare string `"ffmpeg"` — as yt-dlp's
`ffmpeg_location`, and yt-dlp path-checks that value rather than searching
`PATH`. `os.path.exists("ffmpeg")` is false, so it discarded **both** tools.

The last test in this file is the one that would have caught it: it builds the
options the real code builds and asks the real yt-dlp whether it can find the
real binaries.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, ClassVar

import pytest
from clipforge.media.sources import IngestError, resolve_audio_source
from clipforge.media.toolchain import (
    Toolchain,
    ToolchainError,
    find_executable,
    resolve_toolchain,
    yt_dlp_location,
)
from clipforge_contracts import IngestErrorCode

ffmpeg_required = pytest.mark.skipif(
    find_executable("ffmpeg") is None or find_executable("ffprobe") is None,
    reason="needs ffmpeg and ffprobe on PATH",
)

MUSIC_URL = "https://youtu.be/kdQJnqHGI8c"


@pytest.fixture(autouse=True)
def _forget_resolved_tools() -> Any:
    """`resolve_toolchain` is cached for the process; these tests change PATH."""
    resolve_toolchain.cache_clear()
    yield
    resolve_toolchain.cache_clear()


def _toolchain(ffmpeg: str | None, ffprobe: str | None) -> Toolchain:
    return Toolchain(
        ffmpeg=Path(ffmpeg) if ffmpeg else None,
        ffprobe=Path(ffprobe) if ffprobe else None,
        requested_ffmpeg="ffmpeg",
        requested_ffprobe="ffprobe",
    )


# ─────────────────────────────────────────────────────────────────────────────
# find_executable — a bare name is a PATH question, not a path
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_an_absolute_path_to_a_real_program_resolves_to_itself() -> None:
    assert find_executable(sys.executable) == Path(sys.executable).resolve()


@pytest.mark.unit
def test_a_bare_name_is_searched_for_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The case the bug was about. `"ffmpeg"` is not a file; it is a question."""
    interpreter = Path(sys.executable).resolve()
    monkeypatch.setenv("PATH", str(interpreter.parent))

    assert Path(interpreter.name).exists() is False  # nothing is checking the cwd
    assert find_executable(interpreter.name) == interpreter


@pytest.mark.unit
def test_a_program_that_is_not_installed_resolves_to_nothing() -> None:
    assert find_executable("clipforge-no-such-tool-9f3a") is None


@pytest.mark.unit
def test_an_empty_setting_is_not_a_program(monkeypatch: pytest.MonkeyPatch) -> None:
    """`CLIPFORGE_FFMPEG_BIN=` in a .env must not resolve to the cwd."""
    assert find_executable("") is None


# ─────────────────────────────────────────────────────────────────────────────
# Toolchain — "not installed" is an answer, and it has to be usable
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
@ffmpeg_required
def test_the_default_settings_find_both_tools_on_this_machine() -> None:
    tools = resolve_toolchain("ffmpeg", "ffprobe")

    assert tools.complete
    assert tools.missing == ()
    assert tools.ffmpeg is not None and tools.ffmpeg.is_absolute()
    assert tools.ffprobe is not None and tools.ffprobe.is_absolute()


@pytest.mark.unit
def test_a_missing_tool_is_reported_by_the_name_that_was_asked_for() -> None:
    tools = resolve_toolchain("clipforge-no-ffmpeg", "clipforge-no-ffprobe")

    assert not tools.complete
    assert tools.missing == ("clipforge-no-ffmpeg", "clipforge-no-ffprobe")


@pytest.mark.unit
def test_requiring_an_absent_toolchain_names_it_and_refuses_to_be_retried() -> None:
    tools = resolve_toolchain("clipforge-no-ffmpeg", "clipforge-no-ffprobe")

    with pytest.raises(ToolchainError) as caught:
        tools.require("Fetching audio")

    assert "clipforge-no-ffmpeg" in str(caught.value)
    assert "clipforge-no-ffprobe" in str(caught.value)
    assert "Fetching audio" in str(caught.value)
    # No amount of waiting installs ffprobe.
    assert caught.value.retryable is False
    assert caught.value.code == "TOOLCHAIN_MISSING"


@pytest.mark.unit
@ffmpeg_required
def test_requiring_a_complete_toolchain_is_silent() -> None:
    resolve_toolchain("ffmpeg", "ffprobe").require("Fetching audio")


# ─────────────────────────────────────────────────────────────────────────────
# yt_dlp_location — what we are allowed to tell yt-dlp
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_siblings_are_reported_as_their_shared_directory() -> None:
    location = yt_dlp_location(_toolchain("/opt/ff/ffmpeg", "/opt/ff/ffprobe"))

    assert location == str(Path("/opt/ff"))


@pytest.mark.unit
def test_a_split_install_is_reported_as_nothing_at_all() -> None:
    """Naming one directory would find ffmpeg at the cost of ffprobe.

    Unset is not a worse answer here, it is the only correct one: `PATH` can
    hold two tools in two places and a single directory cannot.
    """
    assert yt_dlp_location(_toolchain("/opt/ff/ffmpeg", "/usr/bin/ffprobe")) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ffmpeg", "ffprobe"),
    [(None, "/usr/bin/ffprobe"), ("/usr/bin/ffmpeg", None), (None, None)],
)
def test_an_incomplete_toolchain_is_never_described_to_yt_dlp(
    ffmpeg: str | None, ffprobe: str | None
) -> None:
    assert yt_dlp_location(_toolchain(ffmpeg, ffprobe)) is None


@pytest.mark.unit
@ffmpeg_required
def test_whatever_is_reported_is_a_path_that_exists() -> None:
    """The whole failure in one assertion: yt-dlp calls `os.path.exists` on this."""
    location = yt_dlp_location(resolve_toolchain("ffmpeg", "ffprobe"))

    assert location is not None
    # os.path.exists, not Path.exists: this is the literal call yt-dlp makes on
    # the value, and matching it is the whole assertion.
    assert os.path.exists(location)  # noqa: PTH110


# ─────────────────────────────────────────────────────────────────────────────
# resolve_audio_source — the options actually handed over
# ─────────────────────────────────────────────────────────────────────────────


class _FakeYoutubeDL:
    """Captures the options and writes the file the caller will look for."""

    captured: ClassVar[dict[str, Any]] = {}

    def __init__(self, options: dict[str, Any]) -> None:
        type(self).captured = options
        self._dest = Path(options["outtmpl"]).parent

    def __enter__(self) -> _FakeYoutubeDL:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
        (self._dest / "music-kdQJnqHGI8c.m4a").write_bytes(b"\x00")
        return {"id": "kdQJnqHGI8c", "title": "A Track"}


def _capture_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs: str
) -> dict[str, Any]:
    yt_dlp = pytest.importorskip("yt_dlp")
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYoutubeDL)
    resolve_audio_source(MUSIC_URL, tmp_path, **kwargs)
    return _FakeYoutubeDL.captured


@pytest.mark.unit
@ffmpeg_required
def test_a_bare_command_name_is_never_passed_through_as_a_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression. `ffmpeg_location="ffmpeg"` cost both tools, not one."""
    options = _capture_options(tmp_path, monkeypatch, ffmpeg="ffmpeg", ffprobe="ffprobe")

    assert options.get("ffmpeg_location") != "ffmpeg"
    assert os.path.exists(options["ffmpeg_location"])  # noqa: PTH110 - yt-dlp's own check


@pytest.mark.unit
@ffmpeg_required
def test_warnings_from_yt_dlp_are_kept_rather_than_silenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`no_warnings: True` is what hid the sentence explaining the failure."""
    options = _capture_options(tmp_path, monkeypatch)

    assert options.get("no_warnings") is not True
    assert options["logger"] is not None


@pytest.mark.unit
def test_a_machine_without_the_tools_is_told_before_anything_is_downloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing ffprobe should cost a second, not forty megabytes of an hour mix."""
    yt_dlp = pytest.importorskip("yt_dlp")

    def _must_not_run(options: dict[str, Any]) -> None:
        raise AssertionError("the download started before the tools were checked")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _must_not_run)

    with pytest.raises(ToolchainError) as caught:
        resolve_audio_source(
            MUSIC_URL, tmp_path, ffmpeg="clipforge-no-ffmpeg", ffprobe="clipforge-no-ffprobe"
        )

    assert "clipforge-no-ffprobe" in str(caught.value)


@pytest.mark.unit
def test_a_local_track_needs_no_toolchain_at_all(tmp_path: Path) -> None:
    """Nothing is downloaded and nothing is extracted, so nothing is required."""
    track = tmp_path / "bed.m4a"
    track.write_bytes(b"\x00")

    fetched = resolve_audio_source(
        str(track), tmp_path, ffmpeg="clipforge-no-ffmpeg", ffprobe="clipforge-no-ffprobe"
    )

    assert fetched.path == track
    assert fetched.title == "bed"


@pytest.mark.unit
def test_a_submission_that_is_neither_a_youtube_link_nor_a_file_is_refused(
    tmp_path: Path,
) -> None:
    with pytest.raises(IngestError):
        resolve_audio_source("https://example.com/track.mp3", tmp_path)


# ─────────────────────────────────────────────────────────────────────────────
# The one that would have caught it: ask yt-dlp itself
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
@ffmpeg_required
def test_yt_dlp_can_find_both_tools_given_the_options_we_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Against the real dependency, because the bug was in what it does with us.

    Every assertion above is about our own reasoning. This one hands the real
    options to the real `FFmpegPostProcessor` and asks the question the failing
    job asked: can you find ffmpeg, and can you find ffprobe?
    """
    yt_dlp = pytest.importorskip("yt_dlp")
    from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor

    options = _capture_options(tmp_path, monkeypatch)
    monkeypatch.undo()  # the real YoutubeDL from here on

    post = FFmpegPostProcessor(yt_dlp.YoutubeDL({**options, "logger": None}))

    assert post.available, "yt-dlp could not find ffmpeg"
    assert post.probe_available, "yt-dlp could not find ffprobe"


# ─────────────────────────────────────────────────────────────────────────────
# The mix walk: one track was asked for, 190 were fetched
# ─────────────────────────────────────────────────────────────────────────────

# A share link copied out of the YouTube app, playing inside an autoplay mix.
MIX_URL = "https://youtu.be/kdQJnqHGI8c?list=RDD2XUoPg3-KY"


@pytest.mark.unit
def test_a_link_carrying_a_mix_is_refused_and_the_one_video_is_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression, and it cost half an hour and a gigabyte of disk.

    `?list=RD...` is an autoplay mix — effectively endless. yt-dlp's default is
    to read it as an instruction to download every entry, and nothing here said
    otherwise, so a MUSIC job walked it one track at a time while the PWA showed
    "MUSIC started" and nothing else.

    Taking the one video silently would be a guess about which of the two things
    in the link was meant. The refusal says which it was, and hands back the URL
    that would have worked, so the correction is still one paste.
    """
    yt_dlp = pytest.importorskip("yt_dlp")

    def _must_not_run(options: dict[str, Any]) -> None:
        raise AssertionError("the mix link started a download instead of being refused")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _must_not_run)

    with pytest.raises(IngestError) as caught:
        resolve_audio_source(MIX_URL, tmp_path)

    assert caught.value.code is IngestErrorCode.UNSUPPORTED_URL
    assert "playlist" in str(caught.value)
    assert "https://www.youtube.com/watch?v=kdQJnqHGI8c" in str(caught.value)
    # Refusing after the download would be a guard in name only.
    assert list(tmp_path.iterdir()) == []


@pytest.mark.unit
@ffmpeg_required
def test_the_options_still_forbid_a_playlist_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt and braces behind the guard: no `list=` submission reaches yt-dlp any
    more, but the default on one is to download every entry, and the ingest
    adapter has always set this."""
    options = _capture_options(tmp_path, monkeypatch)

    assert options["noplaylist"] is True


@pytest.mark.unit
def test_yt_dlp_would_walk_the_mix_without_noplaylist(tmp_path: Path) -> None:
    """Against the real dependency: prove the default is what we think it is.

    Every assertion above is about our own options dict. This one asks yt-dlp
    which extractor claims the URL — with `list=` present `YoutubeIE` declines
    and the *tab* extractor takes it, which is the whole mechanism.
    """
    pytest.importorskip("yt_dlp")
    from yt_dlp.extractor.youtube import YoutubeIE

    assert YoutubeIE.suitable(MIX_URL) is False, "a mix link is not claimed by the video extractor"
    assert YoutubeIE.suitable(MUSIC_URL), "a bare video link is"


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=kdQJnqHGI8c&list=PLZbXA4lyCtqoDpVGe-eBnpnBHrqEsQ4zR",
        "https://www.youtube.com/watch?v=kdQJnqHGI8c&list=WL",
        "https://www.youtube.com/watch?v=kdQJnqHGI8c&list=LL",
    ],
)
def test_every_kind_of_list_is_refused_not_just_an_autoplay_mix(
    url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand-made `PL...` playlist, Watch Later and Liked all name more than one
    video, and none of them is what a MUSIC stage was handed a link for."""
    yt_dlp = pytest.importorskip("yt_dlp")

    def _must_not_run(options: dict[str, Any]) -> None:
        raise AssertionError("a playlist link started a download instead of being refused")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _must_not_run)

    with pytest.raises(IngestError) as caught:
        resolve_audio_source(url, tmp_path)

    assert caught.value.code is IngestErrorCode.UNSUPPORTED_URL
    assert "https://www.youtube.com/watch?v=kdQJnqHGI8c" in str(caught.value)


@pytest.mark.unit
def test_a_playlist_url_with_no_video_in_it_is_refused_without_a_suggestion(
    tmp_path: Path,
) -> None:
    """There is no single video in `/playlist?list=...` to offer, so the message
    asks for one instead of inventing one. It must still say *playlist*: this
    URL has no video id, and the refusal it used to reach called it "not a
    YouTube link", which it plainly is."""
    with pytest.raises(IngestError) as caught:
        resolve_audio_source("https://www.youtube.com/playlist?list=PLabc", tmp_path)

    assert caught.value.code is IngestErrorCode.UNSUPPORTED_URL
    assert "playlist" in str(caught.value)
    assert "not a YouTube link" not in str(caught.value)
    assert "watch?v=" not in str(caught.value)


@pytest.mark.unit
@ffmpeg_required
@pytest.mark.parametrize(
    "submission",
    [
        "https://www.youtube.com/watch?v=kdQJnqHGI8c",
        "https://youtu.be/kdQJnqHGI8c",
        "https://youtu.be/kdQJnqHGI8c?si=sharetracking",
        "kdQJnqHGI8c",
        # `?list=` with nothing after it names no playlist, so there is nothing
        # to refuse.
        "https://youtu.be/kdQJnqHGI8c?list=",
    ],
)
def test_a_link_naming_one_video_is_still_fetched(
    submission: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yt_dlp = pytest.importorskip("yt_dlp")
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYoutubeDL)

    fetched = resolve_audio_source(submission, tmp_path)

    assert fetched.path.name == "music-kdQJnqHGI8c.m4a"
    assert fetched.title == "A Track"


@pytest.mark.unit
def test_a_local_track_is_read_even_from_a_directory_whose_name_looks_like_a_list(
    tmp_path: Path,
) -> None:
    """The file on disk is checked first, and stays first: a local track is not a
    URL and must never be parsed as one."""
    track = tmp_path / "bed list=RDabc.m4a"
    track.write_bytes(b"\x00")

    fetched = resolve_audio_source(str(track), tmp_path)

    assert fetched.path == track


@pytest.mark.unit
def test_another_sites_list_parameter_is_not_a_youtube_playlist(tmp_path: Path) -> None:
    """`?list=` means whatever that host says it means. Calling it a YouTube
    playlist would send the user looking for a video that was never there."""
    with pytest.raises(IngestError) as caught:
        resolve_audio_source("https://example.com/track.mp3?list=PLabc", tmp_path)

    assert caught.value.code is IngestErrorCode.UNSUPPORTED_URL
    assert "playlist" not in str(caught.value)
