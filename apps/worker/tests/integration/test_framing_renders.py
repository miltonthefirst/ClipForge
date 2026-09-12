"""Framing, against real ffmpeg, measured rather than inspected.

The unit tier asserts what the filtergraph *says*. This tier asserts what it
*does*, which is a different question and the one that matters: a crop
expression that is wrong does not fail, it renders a clip of the wrong third of
the pitch, and nobody notices until they watch it.

The trick that makes it measurable is the source. It is a pure horizontal
gradient, so brightness encodes horizontal position: a window over the left of
the frame comes back dark, one over the right comes back bright, and a pan from
left to right comes back getting brighter. That turns "is the crop in the right
place" into arithmetic instead of judgement.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from clipforge.media.ffprobe import probe
from clipforge.media.framing import build_video_chain
from clipforge.media.profiles import RenderProfile
from clipforge.media.render import RenderRequest, render_clip
from clipforge.media.tracking import plan_track
from clipforge_contracts import FitFill, Framing, FramingMode, PanKeyframe

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg on PATH"),
]

SOFTWARE_ENCODER = "libx264"
DURATION = 6


@pytest.fixture(scope="module")
def gradient(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A source whose brightness is its horizontal position, exactly."""
    path = tmp_path_factory.mktemp("framing") / "gradient.mp4"
    subprocess.run(  # noqa: S603 - ffmpeg is a hard dependency, resolved via PATH
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s=1920x1080:r=10:d={DURATION}",
            "-vf",
            "geq=lum='X/1920*255':cb=128:cr=128",
            "-c:v",
            "libx264",
            "-crf",
            "12",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


@pytest.fixture(scope="module")
def marker(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A bright marker crossing an otherwise still frame, left to right.

    `overlay` rather than `drawbox`: in drawbox `t` is the *thickness*
    variable, so `t=fill` turns a `t`-as-time expression into a gigantic number
    and the box lands off-screen with no error at all.
    """
    path = tmp_path_factory.mktemp("framing") / "marker.mp4"
    subprocess.run(  # noqa: S603 - ffmpeg is a hard dependency, resolved via PATH
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x204020:s=1920x1080:r=30:d={DURATION}",
            "-f",
            "lavfi",
            "-i",
            f"color=c=white:s=90x90:r=30:d={DURATION}",
            "-filter_complex",
            f"[0][1]overlay=x='60+(t/{DURATION})*1760':y=460",
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def render(
    source: Path,
    destination: Path,
    framing: Framing | None,
    keyframes: list[PanKeyframe] | None = None,
) -> Path:
    media = probe(source)
    graph = build_video_chain(
        media=media,
        profile=RenderProfile(),
        framing=framing,
        keyframes=keyframes or [],
    )
    subprocess.run(  # noqa: S603 - ffmpeg is a hard dependency, resolved via PATH
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(source),
            "-vf",
            graph,
            "-c:v",
            SOFTWARE_ENCODER,
            "-preset",
            "ultrafast",
            "-crf",
            "12",
            "-pix_fmt",
            "yuv420p",
            "-t",
            str(DURATION),
            str(destination),
        ],
        check=True,
        capture_output=True,
    )
    return destination


def frame(path: Path, at_sec: float) -> np.ndarray:
    """One frame as a greyscale array.

    Decoded to rawvideo rather than measured with `signalstats`: the latter
    needs `movie=`, whose filtergraph path escaping a Windows path defeats.
    """
    done = subprocess.run(  # noqa: S603 - ffmpeg is a hard dependency, resolved via PATH
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            f"{at_sec}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            "format=gray",
            "-f",
            "rawvideo",
            "-",
        ],
        check=True,
        capture_output=True,
    )
    return np.frombuffer(done.stdout, dtype=np.uint8).reshape(1920, 1080)


def brightness(path: Path, at_sec: float) -> float:
    return float(frame(path, at_sec).mean())


# ── The fixed window ─────────────────────────────────────────────────────────


def test_the_anchors_take_different_parts_of_the_frame(gradient: Path, tmp_path: Path) -> None:
    values = {
        anchor: brightness(
            render(
                gradient,
                tmp_path / f"{anchor}.mp4",
                Framing(mode=FramingMode.AS_RENDERED, crop=anchor),
            ),
            1.0,
        )
        for anchor in ("left", "centre", "right")
    }
    assert values["left"] < values["centre"] < values["right"]
    # And meaningfully so, not by a rounding error.
    assert values["right"] - values["left"] > 100


def test_a_fixed_crop_keeps_about_a_third_of_the_width(gradient: Path, tmp_path: Path) -> None:
    """The measurement behind the whole feature.

    A 9:16 window over a 16:9 frame keeps 1080 of 1920 pixels' worth of
    gradient — 31.6% — and that is why a ball that moves leaves it.
    """
    clip = render(gradient, tmp_path / "centre.mp4", None)
    row = frame(clip, 1.0)[960].astype(int)
    span = (row.max() - row.min()) / 255.0
    assert span == pytest.approx(0.316, abs=0.06)


# ── FIT ──────────────────────────────────────────────────────────────────────


def test_fit_keeps_the_entire_width(gradient: Path, tmp_path: Path) -> None:
    """Nothing is discarded, so nothing can be lost — the mode's whole claim.

    Measured as the middle scanline spanning the full gradient: if any of the
    source's width had been cropped, the row could not reach both extremes.
    """
    clip = render(gradient, tmp_path / "fit.mp4", Framing(mode=FramingMode.FIT, fill=FitFill.SOLID))
    row = frame(clip, 1.0)[960].astype(int)
    assert row.min() < 10
    assert row.max() > 245
    # Monotonic across the row: the gradient arrives intact rather than as two
    # halves of a mirrored fill.
    assert np.all(np.diff(row) >= -2)


def test_fit_still_produces_a_vertical_clip(gradient: Path, tmp_path: Path) -> None:
    clip = render(gradient, tmp_path / "fit-size.mp4", Framing(mode=FramingMode.FIT))
    media = probe(clip)
    assert (media.width, media.height) == (1080, 1920)


def test_a_blurred_fill_is_not_the_same_picture_as_a_solid_one(
    gradient: Path, tmp_path: Path
) -> None:
    blurred = frame(
        render(gradient, tmp_path / "blur.mp4", Framing(mode=FramingMode.FIT, fill=FitFill.BLUR)),
        1.0,
    )
    solid = frame(
        render(gradient, tmp_path / "solid.mp4", Framing(mode=FramingMode.FIT, fill=FitFill.SOLID)),
        1.0,
    )
    # The top of the canvas is fill in both cases, and they differ there.
    assert abs(float(blurred[100].mean()) - float(solid[100].mean())) > 10


# ── PAN ──────────────────────────────────────────────────────────────────────


def test_a_pan_travels_and_arrives(gradient: Path, tmp_path: Path) -> None:
    clip = render(
        gradient,
        tmp_path / "pan.mp4",
        Framing(mode=FramingMode.PAN),
        [PanKeyframe(at_sec=0, x_pct=10), PanKeyframe(at_sec=5, x_pct=90)],
    )
    start, middle, end = (brightness(clip, t) for t in (0.2, 2.5, 4.8))
    assert start < middle < end
    assert end - start > 80


def test_a_pan_can_go_the_other_way(gradient: Path, tmp_path: Path) -> None:
    clip = render(
        gradient,
        tmp_path / "panback.mp4",
        Framing(mode=FramingMode.PAN),
        [PanKeyframe(at_sec=0, x_pct=90), PanKeyframe(at_sec=5, x_pct=10)],
    )
    assert brightness(clip, 0.2) > brightness(clip, 4.8) + 80


def test_one_keyframe_holds_the_window_still(gradient: Path, tmp_path: Path) -> None:
    clip = render(
        gradient,
        tmp_path / "held.mp4",
        Framing(mode=FramingMode.PAN),
        [PanKeyframe(at_sec=0, x_pct=25)],
    )
    assert brightness(clip, 0.2) == pytest.approx(brightness(clip, 4.8), abs=2)


def test_a_keyframe_at_the_edge_clamps_instead_of_failing(gradient: Path, tmp_path: Path) -> None:
    """ "As far right as it goes" is a request, not an error."""
    edge = render(
        gradient,
        tmp_path / "edge.mp4",
        Framing(mode=FramingMode.PAN),
        [PanKeyframe(at_sec=0, x_pct=100)],
    )
    anchored = render(
        gradient, tmp_path / "right.mp4", Framing(mode=FramingMode.AS_RENDERED, crop="right")
    )
    assert brightness(edge, 1.0) == pytest.approx(brightness(anchored, 1.0), abs=2)


# ── TRACK, end to end ────────────────────────────────────────────────────────


def test_tracking_follows_a_moving_subject_into_the_render(marker: Path, tmp_path: Path) -> None:
    """The football case, in miniature: plan a path, then render it.

    Asserted on the rendered clip rather than on the keyframes, because the
    keyframes being right and the render ignoring them is exactly the failure
    this pair of modules could have.
    """
    result = plan_track(
        marker,
        start_sec=0,
        end_sec=DURATION,
        source_aspect=1920 / 1080,
        smoothing_sec=1.0,
        max_pan_pct_per_sec=30,
    )
    assert result.is_confident
    assert result.keyframes[0].x_pct < result.keyframes[-1].x_pct

    clip = render(
        marker, tmp_path / "tracked.mp4", Framing(mode=FramingMode.TRACK), result.keyframes
    )

    # The marker is the brightest thing in frame; find which column it is in.
    def marker_column(at_sec: float) -> float:
        column_max = frame(clip, at_sec).max(axis=0).astype(int)
        return float(np.argmax(column_max))

    assert frame(clip, 0.5).max() > 200, "the marker should be in shot at the start"
    assert frame(clip, DURATION - 1.2).max() > 200, "and still in shot at the end"
    # Within the shot it stays broadly central rather than pinned to an edge.
    assert 100 < marker_column(DURATION / 2) < 980


def test_a_still_scene_is_reported_as_nothing_to_follow(gradient: Path, tmp_path: Path) -> None:
    result = plan_track(gradient, start_sec=0, end_sec=DURATION, source_aspect=1920 / 1080)
    assert not result.is_confident
    assert [k.x_pct for k in result.keyframes] == [50.0]


# ── Through the render stage's own entry point ───────────────────────────────


def test_a_prepared_graph_reaches_ffmpeg(gradient: Path, tmp_path: Path) -> None:
    """`RenderRequest.video_filter` is the seam REMAKE renders through.

    If it were ignored, every remake would silently produce the profile's fixed
    centre crop — which looks like a working feature that changes nothing.
    """
    media = probe(gradient)
    graph = build_video_chain(
        media=media,
        profile=RenderProfile(),
        framing=Framing(mode=FramingMode.AS_RENDERED, crop="right"),
    )
    destination = tmp_path / "prepared.mp4"
    render_clip(
        RenderRequest(
            source=gradient,
            destination=destination,
            start_sec=0.0,
            end_sec=4.0,
            profile=RenderProfile(),
            encoder=SOFTWARE_ENCODER,
            video_filter=graph,
        ),
        media,
    )
    plain = render(gradient, tmp_path / "plain.mp4", None)
    assert brightness(destination, 1.0) > brightness(plain, 1.0) + 50
