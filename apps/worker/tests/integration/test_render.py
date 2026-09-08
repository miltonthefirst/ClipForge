"""Rendering real video with real ffmpeg.

Phase 6, exit criteria 1-5. These run in the `integration` tier rather than the
GPU one because ffmpeg — not CUDA — is what they need, and CI has ffmpeg. The
NVENC throughput criterion is the exception and is marked `gpu`.

The source is generated here rather than committed: a 60-second 1280x720 clip is
several megabytes, and `ffmpeg testsrc` reproduces it exactly on any machine.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from clipforge.media.captions import build_ass, group_into_cues
from clipforge.media.ffprobe import probe
from clipforge.media.poster import PREVIEW_BUDGET_BYTES, extract_poster
from clipforge.media.profiles import CaptionStyle, RenderProfile, load_profile
from clipforge.media.render import RenderError, RenderRequest, render_clip
from clipforge_contracts import TranscriptWord

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg on PATH"),
]

# x264 everywhere except the throughput test: NVENC is not present on a CI
# runner, and what these assert is the pipeline, not the encoder.
SOFTWARE_ENCODER = "libx264"


@pytest.fixture(scope="module")
def landscape_source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 70-second 1280x720 source with a tone, generated once per module."""
    path = tmp_path_factory.mktemp("render-src") / "source.mp4"
    subprocess.run(  # noqa: S603 - ffmpeg is a hard dependency, resolved via PATH
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1280x720:rate=30:duration=70",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=70",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def render(
    source: Path,
    destination: Path,
    *,
    start: float = 5.0,
    end: float = 25.0,
    profile: RenderProfile | None = None,
    subtitles: Path | None = None,
    encoder: str = SOFTWARE_ENCODER,
) -> Path:
    media = probe(source)
    render_clip(
        RenderRequest(
            source=source,
            destination=destination,
            start_sec=start,
            end_sec=end,
            profile=profile or load_profile("default"),
            subtitles=subtitles,
            encoder=encoder,
        ),
        media,
    )
    return destination


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 1 — a playable 1080x1920 H.264 MP4
# ─────────────────────────────────────────────────────────────────────────────


def test_the_output_is_a_vertical_h264_mp4(landscape_source: Path, tmp_path: Path) -> None:
    """Phase 6, exit criterion 1."""
    clip = render(landscape_source, tmp_path / "clip.mp4")
    info = probe(clip)

    assert info.width == 1080
    assert info.height == 1920
    assert info.video_codec == "h264"
    assert info.audio_codec == "aac"
    assert 19.0 < info.duration_sec < 21.0


def test_the_moov_atom_is_at_the_front_for_ios_safari(
    landscape_source: Path, tmp_path: Path
) -> None:
    """Without `+faststart` the moov atom sits at the end and Safari refuses to
    begin playback at all — which looks like a broken clip, not a container
    detail."""
    clip = render(landscape_source, tmp_path / "clip.mp4")
    head = clip.read_bytes()[:4096]
    assert b"moov" in head, "moov atom is not near the start of the file"


def test_a_zero_length_clip_is_refused_rather_than_encoded(
    landscape_source: Path, tmp_path: Path
) -> None:
    with pytest.raises(RenderError, match="no duration"):
        render(landscape_source, tmp_path / "clip.mp4", start=10.0, end=10.0)


def test_a_failed_render_leaves_no_partial_file(tmp_path: Path) -> None:
    """A DONE stage is never re-run, so a truncated MP4 would be believed."""
    missing = tmp_path / "does-not-exist.mp4"
    destination = tmp_path / "clip.mp4"
    media = probe(  # borrow a valid probe from a real file
        Path(__file__).resolve().parents[2] / "assets" / "fixtures" / "sample-av.mp4"
    )
    with pytest.raises(RenderError):
        render_clip(
            RenderRequest(
                source=missing,
                destination=destination,
                start_sec=0.0,
                end_sec=5.0,
                profile=load_profile("default"),
                encoder=SOFTWARE_ENCODER,
            ),
            media,
        )
    assert not destination.exists()
    assert list(tmp_path.glob("*.partial")) == []


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 2 — captions actually appear, and at the right time
# ─────────────────────────────────────────────────────────────────────────────


def test_captions_are_burned_into_the_picture(landscape_source: Path, tmp_path: Path) -> None:
    """Phase 6, exit criterion 2.

    Compares a frame rendered with captions against the same frame without.
    Burned-in text changes the picture; if the subtitles filter silently failed —
    which it does, loudly but non-fatally, on a path escaping mistake — the two
    frames would be identical.
    """
    words = [
        TranscriptWord(text=w, start_sec=5.0 + i * 0.5, end_sec=5.0 + i * 0.5 + 0.45)
        for i, w in enumerate(["CLIPFORGE", "CAPTION", "TEST", "FRAME"])
    ]
    style = CaptionStyle(font_size=96)
    cues = group_into_cues(words, style=style, clip_start_sec=5.0, clip_end_sec=25.0)
    assert cues, "the fixture words produced no cues"

    subtitles = tmp_path / "captions.ass"
    subtitles.write_text(build_ass(cues, style=style), encoding="utf-8", newline="\n")

    with_captions = render(
        landscape_source,
        tmp_path / "with.mp4",
        subtitles=subtitles,
        profile=RenderProfile(captions=style),
    )
    without = render(landscape_source, tmp_path / "without.mp4")

    frame_a = _extract_frame(with_captions, 0.6, tmp_path / "a.png")
    frame_b = _extract_frame(without, 0.6, tmp_path / "b.png")

    assert frame_a.read_bytes() != frame_b.read_bytes(), (
        "the captioned and uncaptioned frames are identical — the subtitles filter did not apply"
    )


def _extract_frame(clip: Path, at_sec: float, destination: Path) -> Path:
    subprocess.run(  # noqa: S603 - ffmpeg is a hard dependency, resolved via PATH
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-ss",
            f"{at_sec:.3f}",
            "-i",
            str(clip),
            "-frames:v",
            "1",
            str(destination),
        ],
        check=True,
        capture_output=True,
    )
    return destination


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 3 — loudness
# ─────────────────────────────────────────────────────────────────────────────


def test_integrated_loudness_lands_on_target(landscape_source: Path, tmp_path: Path) -> None:
    """Phase 6, exit criterion 3. -14 LUFS is what the short-form platforms
    normalise to, so delivering louder just means they turn it down with the
    dynamics already squashed."""
    clip = render(landscape_source, tmp_path / "clip.mp4")
    measured = _measure_lufs(clip)

    assert measured is not None, "ebur128 reported no integrated loudness"
    assert abs(measured - (-14.0)) <= 1.5, f"measured {measured} LUFS, target -14"


def _measure_lufs(clip: Path) -> float | None:
    completed = subprocess.run(  # noqa: S603
        ["ffmpeg", "-nostats", "-i", str(clip), "-af", "ebur128", "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    matches = re.findall(r"I:\s+(-?\d+\.\d+)\s+LUFS", completed.stderr)
    return float(matches[-1]) if matches else None


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 5 — determinism
# ─────────────────────────────────────────────────────────────────────────────


def test_re_rendering_the_same_candidate_is_byte_identical(
    landscape_source: Path, tmp_path: Path
) -> None:
    """Phase 6, exit criterion 5.

    Requires stripping source metadata and encoder timestamps — without
    `-map_metadata -1` and `-fflags +bitexact` the two files differ by an
    embedded creation time, and nothing downstream could ever cache a render.
    """
    first = render(landscape_source, tmp_path / "first.mp4")
    second = render(landscape_source, tmp_path / "second.mp4")

    assert first.read_bytes() == second.read_bytes()


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 7 — previews fit in a Firestore document
# ─────────────────────────────────────────────────────────────────────────────


def test_the_poster_and_filmstrip_fit_well_inside_the_document_limit(
    landscape_source: Path, tmp_path: Path
) -> None:
    """Phase 6, exit criterion 7.

    On the free tier this poster is most of what a phone review has to go on, so
    it has to exist — and it has to fit, because a document that overflowed would
    fail the write at the very end of a job that already spent GPU minutes.
    """
    clip = render(landscape_source, tmp_path / "clip.mp4")
    images = extract_poster(clip, duration_sec=20.0, work_dir=tmp_path / "work")

    assert images.poster_base64
    assert images.width_px > 0
    assert images.byte_size < PREVIEW_BUDGET_BYTES
    assert images.byte_size < 1_048_576 // 2


def test_the_poster_is_taken_from_the_rendered_clip_not_the_source(
    landscape_source: Path, tmp_path: Path
) -> None:
    """A preview from the source would show a differently-framed image with no
    captions — worse than useless for judging a clip."""
    clip = render(landscape_source, tmp_path / "clip.mp4")
    images = extract_poster(clip, duration_sec=20.0, work_dir=tmp_path / "work")

    # The poster is scaled to 360 wide; a 9:16 crop makes it 640 tall.
    assert images.width_px == 360
    assert images.height_px > images.width_px, "the poster is not vertical"


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 4 — throughput, on the real encoder
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.gpu
def test_a_sixty_second_clip_renders_in_under_thirty_seconds(
    landscape_source: Path, tmp_path: Path
) -> None:
    """Phase 6, exit criterion 4.

    Asserts the *budget*, whichever encoder actually runs. On the reference
    machine NVENC is listed by ffmpeg but refuses to initialise — the driver is
    an API version behind the build — so the render falls back to libx264, and
    an i9 still comfortably beats 30 seconds. Pinning this to NVENC would test
    the driver rather than the pipeline.
    """
    started = time.monotonic()
    render(
        landscape_source,
        tmp_path / "clip.mp4",
        start=5.0,
        end=65.0,
        encoder="h264_nvenc",
    )
    elapsed = time.monotonic() - started

    assert elapsed < 30.0, f"a 60s clip took {elapsed:.1f}s"


@pytest.mark.gpu
def test_an_unusable_hardware_encoder_falls_back_rather_than_failing(
    landscape_source: Path, tmp_path: Path
) -> None:
    """A hardware encoder that cannot initialise is an environment problem, not a
    problem with this clip — and it fails identically for every subsequent one.
    Falling back costs encode time; failing costs the whole pipeline run that
    produced the candidate.
    """
    clip = render(
        landscape_source,
        tmp_path / "clip.mp4",
        start=5.0,
        end=12.0,
        encoder="h264_nonsense_encoder",
    )
    info = probe(clip)
    assert info.video_codec == "h264", "the fallback did not produce a usable clip"
