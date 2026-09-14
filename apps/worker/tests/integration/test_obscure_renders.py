"""Hiding a mark, against real ffmpeg, measured rather than inspected.

The unit tier asserts what the filtergraph *says*. This tier asserts what it
*does*, and the difference is not academic: `delogo` refuses a region touching
the frame edge at filter-configuration time, which fails the whole render, and
no amount of reading the string reveals that.

The trick that makes it measurable is the source. The background is a gradient
that scrolls, so every pixel of it changes every frame, and the planted mark is
pure white and does not move. That makes "is the mark still there" a question
about the mean brightness of a rectangle, and "did detection find it" a question
about overlap — arithmetic rather than judgement, and the same reason
`test_framing_renders` uses a gradient.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from clipforge.media.obscure import (
    build_obscure_chains,
    detect_static_regions,
    overlaps,
)
from clipforge_contracts import ObscureMethod, ObscureRegion

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg on PATH"),
]

WIDTH, HEIGHT = 640, 360
DURATION = 5

# The planted mark, in pixels and as the percentages everything else speaks in.
MARK_X, MARK_Y, MARK_W, MARK_H = 440, 30, 120, 50
PLANTED = ObscureRegion(
    x_pct=MARK_X / WIDTH * 100,
    y_pct=MARK_Y / HEIGHT * 100,
    w_pct=MARK_W / WIDTH * 100,
    h_pct=MARK_H / HEIGHT * 100,
)


@pytest.fixture(scope="module")
def marked(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A scrolling gradient with one white mark nailed to the top right."""
    path = tmp_path_factory.mktemp("obscure") / "marked.mp4"
    subprocess.run(  # noqa: S603 - ffmpeg is a hard dependency, resolved via PATH
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s={WIDTH}x{HEIGHT}:r=10:d={DURATION}",
            "-vf",
            # Everything moves: brightness is a function of X *and* time, so no
            # part of the background is static and anything that is stands out.
            "geq=lum='mod(X*0.7+T*90,256)':cb=128:cr=128,"
            f"drawbox=x={MARK_X}:y={MARK_Y}:w={MARK_W}:h={MARK_H}:color=white@1:t=fill,"
            # Six dark bars on the plate, not one solid block. The difference
            # decides what the tests below can even detect: a logo is fine
            # structure, and a mosaic or a blur is judged by whether that
            # structure survives. A single large block survives any amount of
            # pixelation, so a fixture built from one measures nothing.
            + ",".join(
                f"drawbox=x={MARK_X + 10 + i * 18}:y={MARK_Y + 12}:w=7:h=26:color=black@1:t=fill"
                for i in range(6)
            ),
            "-c:v",
            "libx264",
            "-crf",
            "12",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def frame_at(source: Path, *, graph: str | None, at_sec: float = 2.5) -> np.ndarray:
    """One greyscale frame, as an array, after an optional filtergraph."""
    argv = ["ffmpeg", "-v", "error", "-ss", f"{at_sec}", "-i", str(source)]
    if graph:
        argv += ["-vf", f"{graph},format=gray"]
    else:
        argv += ["-vf", "format=gray"]
    argv += ["-frames:v", "1", "-f", "rawvideo", "-"]
    done = subprocess.run(argv, capture_output=True, check=True)  # noqa: S603
    return np.frombuffer(done.stdout, dtype=np.uint8).reshape(HEIGHT, WIDTH)


def all_frames(source: Path, *, graph: str) -> np.ndarray:
    """Every frame of the clip, decoded from the start rather than seeked to."""
    done = subprocess.run(  # noqa: S603
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(source),
            "-vf",
            f"{graph},format=gray",
            "-f",
            "rawvideo",
            "-",
        ],
        capture_output=True,
        check=True,
    )
    per_frame = WIDTH * HEIGHT
    count = len(done.stdout) // per_frame
    return np.frombuffer(done.stdout[: count * per_frame], dtype=np.uint8).reshape(
        count, HEIGHT, WIDTH
    )


def inside_mark(frame: np.ndarray) -> np.ndarray:
    return frame[MARK_Y : MARK_Y + MARK_H, MARK_X : MARK_X + MARK_W]


def detail(frame: np.ndarray) -> float:
    """How much structure is left inside the plate.

    Measured well inside the mark's own border, because the border is an edge
    of the *rectangle* rather than of the thing written on it, and every method
    here is allowed to leave that. What must not survive is the bars: they are
    the part a reader would read.
    """
    return float(frame[MARK_Y + 10 : MARK_Y + MARK_H - 10, MARK_X + 8 : MARK_X + MARK_W - 8].std())


def render(source: Path, regions: list[ObscureRegion]) -> np.ndarray:
    chains = build_obscure_chains(regions, width=WIDTH, height=HEIGHT)
    return frame_at(source, graph=";".join([*chains, "[obscured]null"]))


# ── The mark is really there to begin with ───────────────────────────────────


def test_the_fixture_actually_has_a_mark_on_it(marked: Path) -> None:
    """Without this, every test below could pass on a blank frame."""
    plain = frame_at(marked, graph=None)
    assert inside_mark(plain).max() > 230
    assert detail(plain) > 80  # white plate, six dark bars


# ── Finding it ───────────────────────────────────────────────────────────────


def test_detection_finds_the_planted_mark(marked: Path) -> None:
    found = detect_static_regions(marked, start_sec=0.0, end_sec=float(DURATION))
    assert found.regions, found.reason
    assert any(overlaps(region, PLANTED) for region in found.regions)


def test_detection_finds_nothing_on_footage_that_never_moves(
    tmp_path: Path,
) -> None:
    """A locked-off shot is static everywhere, so nothing stands out by being so.

    The honest answer is an empty list with a reason, not half the frame under
    a blur — and the reason is what tells the reviewer to draw a box instead.
    """
    still = tmp_path / "still.mp4"
    subprocess.run(  # noqa: S603
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={WIDTH}x{HEIGHT}:rate=10:duration=3",
            "-vf",
            "select='eq(n\\,0)',loop=loop=-1:size=1,trim=duration=3",
            "-c:v",
            "libx264",
            "-crf",
            "12",
            "-pix_fmt",
            "yuv420p",
            str(still),
        ],
        check=True,
        capture_output=True,
    )
    found = detect_static_regions(still, start_sec=0.0, end_sec=3.0)
    assert found.regions == []
    assert "barely moves" in found.reason


def test_a_corner_hint_narrows_the_search(marked: Path) -> None:
    """The mark is top right, so asking for bottom left must find nothing."""
    assert (
        detect_static_regions(
            marked, start_sec=0.0, end_sec=float(DURATION), where="BOTTOM_LEFT"
        ).regions
        == []
    )


def test_detection_skips_what_the_reviewer_already_drew(marked: Path) -> None:
    """Asking for both must never put two filters over the same pixels."""
    found = detect_static_regions(marked, start_sec=0.0, end_sec=float(DURATION), avoid=[PLANTED])
    assert not any(overlaps(region, PLANTED) for region in found.regions)


# ── Hiding it ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method",
    [ObscureMethod.BLUR, ObscureMethod.PIXELATE, ObscureMethod.DELOGO, ObscureMethod.BOX],
)
def test_every_method_leaves_nothing_readable(marked: Path, method: ObscureMethod) -> None:
    """The bars must be gone, whichever way they were removed.

    This is the test that found the pixelation bug. `flags=neighbor` on the
    downscale samples one pixel per cell rather than averaging it, so the
    mosaic kept whichever bars it happened to land on and the mark stayed
    perfectly legible while the graph looked right.
    """
    before = detail(frame_at(marked, graph=None))
    after = detail(render(marked, [PLANTED.model_copy(update={"method": method})]))
    assert after < before / 3


def test_the_rest_of_the_frame_is_untouched(marked: Path) -> None:
    """A filter that quietly blurred the whole picture would pass every test
    above and ruin every clip."""
    plain = frame_at(marked, graph=None)
    hidden = render(marked, [PLANTED])
    elsewhere = (slice(200, 350), slice(0, 300))
    assert np.array_equal(plain[elsewhere], hidden[elsewhere])


def test_a_region_touching_the_corner_still_renders(marked: Path) -> None:
    """delogo refuses a region on the frame edge, and refuses the whole render.

    A corner is where a channel bug lives, so this is the ordinary case rather
    than an edge case, and it failed outright before the inset existed.
    """
    corner = ObscureRegion(
        x_pct=0.0, y_pct=0.0, w_pct=18.0, h_pct=14.0, method=ObscureMethod.DELOGO
    )
    assert render(marked, [corner]).shape == (HEIGHT, WIDTH)


def test_two_regions_compose(marked: Path) -> None:
    other = ObscureRegion(x_pct=5.0, y_pct=70.0, w_pct=20.0, h_pct=15.0)
    hidden = render(marked, [PLANTED, other])
    assert detail(hidden) < 40
    assert hidden.shape == (HEIGHT, WIDTH)


def test_a_time_window_leaves_the_mark_alone_outside_it(marked: Path) -> None:
    """`t` is seconds from the start of the CLIP, which is what `-ss` makes it.

    Decoded from zero rather than seeked to, and that is the point rather than
    an inconvenience: `render_clip` puts `-ss` before `-i`, so ffmpeg resets
    output timestamps and a filter's `t` counts from the cut. Seeking here
    would reset them the same way and the window would never open — which is
    exactly what this test did before it was written this way.
    """
    timed = PLANTED.model_copy(update={"from_sec": 3.0})
    chains = build_obscure_chains([timed], width=WIDTH, height=HEIGHT)
    frames = all_frames(marked, graph=";".join([*chains, "[obscured]null"]))

    assert detail(frames[10]) > 80  # one second in: still there
    assert detail(frames[40]) < detail(frames[10]) / 3  # four seconds in: gone


def test_what_detection_found_is_what_renders(marked: Path) -> None:
    """The whole loop, end to end: find it, hide it, check it is gone."""
    found = detect_static_regions(marked, start_sec=0.0, end_sec=float(DURATION))
    assert found.regions, found.reason
    assert detail(render(marked, list(found.regions))) < 40
