"""Hiding a fixed part of the picture: channel bugs, scoreboards, burnt-in text.

## Why this exists

A reviewer asked three times, in three different remakes, for the Canal+ bug to
be blurred. Every time the answer was the same: it was read correctly, recorded
as `UnsupportedAsk.REMOVE_WATERMARK`, and the clip came back with the logo still
on it. The refusal was honest and completely useless — the footage a channel
publishes has the channel's mark burnt into it, which is a permanent property of
every clip that will ever be cut from it.

## Two halves that are deliberately separate

**Finding the region** is statistics over decoded frames and produces
percentages. **Hiding it** is a filtergraph and produces a string. Keeping them
apart is what lets a reviewer correct a box the detector got wrong: the drawn
rectangle and the found one are the same kind of thing by the time anything
renders, so a manual box is not a second code path.

## What detection actually keys on

Not "is this a logo" — nothing here knows what a logo looks like. It keys on
**something that holds still while the rest of the frame does not**, which is a
much easier question and, for broadcast footage, very nearly the same one. The
camera pans, players run, the crowd moves; the bug in the corner does not. So a
cell of the frame is a candidate when it is far more static than the frame
typically is *and* it has some structure in it — the second test being what
stops a patch of empty sky qualifying.

The comparison is relative rather than absolute, and that is the part that
makes it work. On a locked-off shot everything is static and nothing stands out,
so detection correctly finds nothing rather than blurring half the picture; the
`_MIN_GLOBAL_MOTION` gate says so out loud instead of returning an empty list
the caller has to interpret.

**What it does not catch:** burnt-in subtitles whose text changes line to line.
Those are fixed in position and not in content, so nothing above sees them. They
are exactly the case `Source.obscure` is for — draw the box once, and every clip
from that channel is cut with it already applied.

## Source coordinates, and why measured in percent

The rectangle is a percentage of the *source* frame. The output is cropped,
panned and scaled, so a box in output coordinates would have to be transformed
by whatever the framing did, and under TRACK it would have to move — dragging a
blurred square across the picture in pursuit of a logo that never moved.
Percentages then survive a source that turns out to be 1280 wide when the box
was drawn against a 1920 poster.

## On integers, where `framing` uses expressions

`clipforge.media.framing` writes its crops in ffmpeg's own `iw`/`ih` so they
stay right if a variable-resolution stream disagrees with ffprobe. This module
resolves to pixels instead, for two reasons. `delogo` will not take a region
that touches the frame edge — it refuses to configure at all, failing the whole
render — so the rectangle has to be clamped against real dimensions rather than
hoped into range, and a clamp needs numbers. And the failure modes differ: a
wrong crop expression renders the wrong third of the pitch, while a blur box a
few pixels out is invisible. The integers also make a mis-placed box legible in
the log, which is the thing anyone debugging this will actually want.
"""

from __future__ import annotations

import subprocess
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from clipforge_contracts import ObscureFound, ObscureMethod, ObscureRegion

from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = [
    "Detection",
    "ObscureError",
    "build_obscure_chains",
    "describe_region",
    "detect_static_regions",
    "merge_regions",
    "overlaps",
]

# ── Rendering ────────────────────────────────────────────────────────────────

OBSCURE_LABEL = "obscured"

# How hard to hide something when nobody said. Chosen so the shape underneath is
# not readable: a blur you can still read the logo through is worse than none,
# because it looks like a mistake rather than a decision.
DEFAULT_STRENGTH = 0.75

# Where reconstruction wins, and where it starts losing. delogo interpolates the
# region from the band of pixels around it, so its quality is a function of how
# far the middle of the box is from an edge of it. On a channel bug it is
# uncanny — the first real render put it over the CANAL+ mark on a crowd
# background and left nothing to see. On the score bar in the same frame, twenty
# percent of the width away from its own edges, it drew a horizontal smear that
# was more conspicuous than the graphic had been.
#
# So the limits are on the SIDES as much as the area, and they are tight. A
# region wider than about a sixth of the frame gets blurred, which is honest
# rather than invisible, and honest is the correct trade once invisible is off
# the table.
_DELOGO_MAX_AREA = 0.016
_DELOGO_MAX_SIDE_PCT = 16.0
_DELOGO_MAX_HEIGHT_PCT = 14.0

# delogo refuses a region touching the frame edge — "Logo area is outside of the
# frame", at filter-configuration time, which fails the entire render rather
# than that one filter. A corner bug is precisely the case that would hit it, so
# the inset is load-bearing rather than defensive. One pixel is enough; two
# keeps the rectangle even-aligned as well.
_DELOGO_INSET = 2

_BOX_COLOUR = "0x11141A"  # the same flat fill FIT uses, so the look is one look


class ObscureError(RuntimeError):
    """The footage could not be analysed for things to hide."""


def build_obscure_chains(
    regions: Sequence[ObscureRegion],
    *,
    width: int,
    height: int,
    default_method: ObscureMethod | None = None,
    default_strength: float | None = None,
    label: str = OBSCURE_LABEL,
) -> list[str]:
    """Filtergraph chains that hide each region, ending in ``[label]``.

    Returned as a list for the caller to join with ``;``, matching
    `clipforge.media.framing.fit_graph`. Empty for no regions, which is the
    common case and must cost nothing — a clip with nothing to hide should
    produce exactly the filtergraph it produced before this module existed.

    The chains go **first**, before any crop, because the coordinates are in the
    source frame. Hiding a logo that the crop then discards is wasted work and
    not an error; hiding one after the crop would need the box moved by however
    much the window moved, which under TRACK is a different amount every frame.
    """
    usable = [r for r in regions if _rect(r, width, height, inset=0) is not None]
    if not usable:
        return []

    chains: list[str] = []
    inlet = ""  # the first chain reads the graph's implicit input

    for index, region in enumerate(usable):
        method = region.method or default_method or _method_for(region)
        strength = _strength(region, default_strength)
        inset = _DELOGO_INSET if method is ObscureMethod.DELOGO else 0
        rect = _rect(region, width, height, inset=inset)
        if rect is None:
            # Only reachable for DELOGO on a region so close to the edge that
            # insetting consumed it. Reconstruction is the wrong tool there
            # anyway, so fall back rather than drop the reviewer's request.
            method = ObscureMethod.BLUR
            rect = _rect(region, width, height, inset=0)
            if rect is None:  # pragma: no cover - filtered by `usable` above
                continue
        x, y, w, h = rect
        out = f"[{label}]" if index == len(usable) - 1 else f"[ob{index}]"

        if method is ObscureMethod.DELOGO:
            chains.append(f"{inlet}delogo=x={x}:y={y}:w={w}:h={h}{_enable(region)}{out}")
        elif method is ObscureMethod.BOX:
            chains.append(
                f"{inlet}drawbox=x={x}:y={y}:w={w}:h={h}"
                f":color={_BOX_COLOUR}:t=fill{_enable(region)}{out}"
            )
        else:
            # crop and scale have no timeline support, so a time-limited region
            # is gated on the overlay instead. A disabled overlay passes its
            # first input through untouched, which puts the original pixels
            # back — the same result as disabling the blur itself.
            keep, cut, patched = f"ob{index}a", f"ob{index}b", f"ob{index}c"
            chains.append(f"{inlet}split=2[{keep}][{cut}]")
            chains.append(
                f"[{cut}]crop={w}:{h}:{x}:{y},{_patch(method, w, h, strength)}[{patched}]"
            )
            chains.append(f"[{keep}][{patched}]overlay={x}:{y}{_enable(region)}{out}")

        inlet = out

    return chains


def _patch(method: ObscureMethod, w: int, h: int, strength: float) -> str:
    """What happens to the cut-out rectangle before it goes back.

    Both scale with the region rather than using a fixed radius. A sigma that
    erases a 40-pixel bug leaves a 400-pixel caption perfectly readable, and the
    reviewer has no way to know which they got without watching it.
    """
    smallest = max(2, min(w, h))
    if method is ObscureMethod.PIXELATE:
        cell = max(3, int(smallest * (0.10 + 0.25 * strength)))
        # **The two passes need different filters, and that is the whole thing.**
        # Going down must AVERAGE each cell (`area`); going back up must NOT
        # interpolate (`neighbor`), or the blocks are smeared into a soft blur
        # by another name. Using `neighbor` on the way down instead samples one
        # pixel per cell, so a mosaic over six dark bars on a white plate kept
        # every bar it happened to land on and the logo stayed perfectly
        # legible through it — measured, in test_obscure_renders.
        return (
            f"scale={max(1, w // cell)}:{max(1, h // cell)}:flags=area,scale={w}:{h}:flags=neighbor"
        )
    sigma = max(4.0, min(120.0, smallest * (0.08 + 0.18 * strength)))
    # Two cheap passes rather than one wide one, for the reason FIT's background
    # gives: a single pass at an erasing radius costs more than the encode.
    return f"gblur=sigma={sigma:.1f}:steps=2"


def _method_for(region: ObscureRegion) -> ObscureMethod:
    """The method nobody chose: reconstruct if it is small, blur if it is not."""
    area = (region.w_pct / 100.0) * (region.h_pct / 100.0)
    small_enough = (
        area <= _DELOGO_MAX_AREA
        and region.w_pct <= _DELOGO_MAX_SIDE_PCT
        and region.h_pct <= _DELOGO_MAX_HEIGHT_PCT
    )
    return ObscureMethod.DELOGO if small_enough else ObscureMethod.BLUR


def _strength(region: ObscureRegion, default: float | None) -> float:
    value = region.strength if region.strength is not None else default
    return DEFAULT_STRENGTH if value is None else max(0.0, min(1.0, float(value)))


def _enable(region: ObscureRegion) -> str:
    """A time window, in seconds from the start of the CLIP.

    Clip-relative because `render_clip` seeks with `-ss` before `-i`, so output
    timestamps begin at zero — the same timebase the pan keyframes use.
    """
    start, end = region.from_sec, region.to_sec
    if start is None and end is None:
        return ""
    if end is None:
        return f":enable='gte(t,{max(0.0, start or 0.0):.3f})'"
    return f":enable='between(t,{max(0.0, start or 0.0):.3f},{end:.3f})'"


def _rect(
    region: ObscureRegion, width: int, height: int, *, inset: int
) -> tuple[int, int, int, int] | None:
    """The region as even, in-frame pixels, or None if nothing is left of it.

    Even because a chroma-subsampled format places the crop on a two-pixel grid
    and an odd offset shifts colour against luma; the rounding is invisible and
    the alternative is a class of bug that only shows on some pixel formats.
    """
    if width <= 0 or height <= 0:
        return None

    low = inset
    x = _even(_clamp(round(width * region.x_pct / 100.0), low, width - low - 2))
    y = _even(_clamp(round(height * region.y_pct / 100.0), low, height - low - 2))
    x, y = max(x, _even_up(low)), max(y, _even_up(low))

    w = _even(_clamp(round(width * region.w_pct / 100.0), 2, width - low - x))
    h = _even(_clamp(round(height * region.h_pct / 100.0), 2, height - low - y))
    if w < 2 or h < 2 or x + w > width - low or y + h > height - low:
        return None
    return x, y, w, h


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _even(value: int) -> int:
    return value - (value % 2)


def _even_up(value: int) -> int:
    return value + (value % 2)


def describe_region(region: ObscureRegion) -> str:
    """One phrase naming a rectangle, for a summary a person reads."""
    if region.label:
        return region.label
    return f"{_where(region)} ({region.w_pct:.0f}% x {region.h_pct:.0f}%)"


def _where(region: ObscureRegion) -> str:
    mid_x = region.x_pct + region.w_pct / 2
    mid_y = region.y_pct + region.h_pct / 2
    vertical = "top" if mid_y < 40 else "bottom" if mid_y > 60 else "middle"
    horizontal = "left" if mid_x < 40 else "right" if mid_x > 60 else "centre"
    return f"{vertical} {horizontal}"


# ── Detection ────────────────────────────────────────────────────────────────

# Big enough to locate a channel bug to within a few tenths of a percent of
# frame width, small enough that the whole pass is a fraction of a second.
_ANALYSIS_WIDTH = 480
_ANALYSIS_HEIGHT = 270

# The grid everything is decided on: 80 x 45 cells. Finer than this starts
# fragmenting a logo into its own letters; coarser cannot tell a bug from the
# quarter of the frame around it.
_CELL = 6

# How many instants to look at. The statistic is a median over these, so it
# wants enough samples that a few frames of something parked in front of the
# logo cannot move it.
_SAMPLES = 32

# Below this, the whole frame is holding still — a locked-off shot, a freeze, a
# title card — and nothing in it can stand out by being static.
_MIN_GLOBAL_MOTION = 0.9

# A candidate cell must be this much stiller than the frame's own median, with
# an absolute floor so that footage which barely moves cannot make noise qualify.
_STATIC_RATIO = 0.30
_STATIC_FLOOR = 1.3

# And it must have some structure in it, or flat sky and black bars qualify.
_EDGE_FLOOR = 3.0

# A real graphic fills its own bounding box. Scattered static cells that happen
# to share a bounding box are noise, and this is what tells them apart.
_MIN_FILL = 0.45

_MIN_CONFIDENCE = 0.35
_MAX_REGIONS = 4

# Two found rectangles in the same horizontal band, with only a small gap
# between them, are nearly always one graphic that detection split in half.
# These are the numbers that join them: how much of their heights must overlap,
# and how wide a gap is still the same plate.
_BAND_OVERLAP = 0.55
_BAND_GAP_PCT = 7.0

# Where a note's corner hint allows a region's centre to be, as
# (x_low, x_high, y_low, y_high) in percent.
_CORNERS: dict[str, tuple[float, float, float, float]] = {
    "TOP_LEFT": (0.0, 55.0, 0.0, 55.0),
    "TOP_RIGHT": (45.0, 100.0, 0.0, 55.0),
    "BOTTOM_LEFT": (0.0, 55.0, 45.0, 100.0),
    "BOTTOM_RIGHT": (45.0, 100.0, 45.0, 100.0),
    "TOP": (0.0, 100.0, 0.0, 45.0),
    "BOTTOM": (0.0, 100.0, 55.0, 100.0),
}


@dataclass(frozen=True)
class Detection:
    """What was found, and — when nothing was — why not.

    The reason is the point. "Nothing was found" and "the footage never moves,
    so nothing could be found" call for different next moves from the reviewer,
    and a bare empty list makes them look identical.
    """

    regions: list[ObscureRegion]
    samples: int
    reason: str = ""


def detect_static_regions(
    source: Path,
    *,
    start_sec: float,
    end_sec: float,
    where: str | None = None,
    avoid: Sequence[ObscureRegion] = (),
    max_regions: int = _MAX_REGIONS,
    ffmpeg_bin: str = "ffmpeg",
    timeout_s: float = 120.0,
) -> Detection:
    """Find the parts of the frame that hold still while the rest does not.

    `where` narrows the search to a corner or band when the note named one, and
    `avoid` drops anything overlapping a rectangle the reviewer already drew —
    so asking for detection *and* supplying a box never puts two filters over
    the same pixels.
    """
    duration = max(0.0, end_sec - start_sec)
    if duration < 1.0:
        return Detection([], 0, "the clip is too short to tell what is holding still")

    frames = _decode_grey(
        source,
        start_sec=start_sec,
        duration_sec=duration,
        ffmpeg_bin=ffmpeg_bin,
        timeout_s=timeout_s,
    )
    if len(frames) < 4:
        return Detection([], len(frames), "too few frames decoded to compare")

    planes = frames.astype(np.float32)
    median = np.median(planes, axis=0)
    # Mean absolute deviation from the median, per pixel: how unsettled each
    # part of the frame is over the clip. The median rather than the mean so a
    # few frames of something passing in front do not count as movement.
    instability = np.abs(planes - median).mean(axis=0)

    motion = _cells(instability)
    structure = _cells(_gradient(median))
    typical = float(np.median(motion))
    if typical < _MIN_GLOBAL_MOTION:
        return Detection(
            [],
            len(frames),
            "the footage barely moves, so nothing in it stands out as being fixed",
        )

    still = max(_STATIC_FLOOR, typical * _STATIC_RATIO)
    textured = max(_EDGE_FLOOR, float(np.percentile(structure, 25)))
    candidate = (motion < still) & (structure > textured)
    if not candidate.any():
        return Detection([], len(frames), "nothing in the frame held still for the whole clip")

    found: list[tuple[float, ObscureRegion]] = []
    for blob in _components(candidate):
        region = _region_for(blob, motion, structure, still=still, textured=textured)
        if region is None:
            continue
        confidence, shape = region
        if confidence < _MIN_CONFIDENCE:
            continue
        if where and not _inside(shape, _CORNERS.get(where)):
            continue
        if any(overlaps(shape, existing) for existing in avoid):
            continue
        found.append((confidence, shape))

    found.sort(key=lambda pair: -pair[0])
    found = _join_bands(found)
    kept: list[ObscureRegion] = []
    for confidence, shape in found:
        if any(overlaps(shape, already) for already in kept):
            continue
        kept.append(shape)
        log.info(
            "obscure.found",
            x=round(shape.x_pct, 1),
            y=round(shape.y_pct, 1),
            w=round(shape.w_pct, 1),
            h=round(shape.h_pct, 1),
            confidence=round(confidence, 2),
        )
        if len(kept) >= max_regions:
            break

    if not kept:
        return Detection(
            [],
            len(frames),
            (
                "the still parts of the frame did not look like a logo or a caption"
                if found or candidate.any()
                else "nothing in the frame held still for the whole clip"
            ),
        )
    return Detection(kept, len(frames))


def _join_bands(found: list[tuple[float, ObscureRegion]]) -> list[tuple[float, ObscureRegion]]:
    """Join rectangles that are two halves of one broadcast graphic.

    This exists because of a clock. A match clock's digits change every second,
    so the only part of it that is static is the competition badge beside them
    — and detection, which knows nothing except what holds still, came back
    with a box over the badge and left "34:37" sitting in the open next to a
    blurred square. Half-covering a graphic is worse than not covering it: it
    looks like a fault rather than a decision.

    The fix keys on how broadcast furniture is actually built. Graphics sit on
    plates, plates sit in a band along one edge, and the changing parts of a
    plate are surrounded by the fixed parts of it. So two finds at the same
    height with a small gap between them are treated as one plate and the gap
    is swallowed, which covers whatever was moving in between without needing to
    detect it.

    The gap is deliberately small. Widen it and the score bar at one end of the
    band joins the competition badge at the other, and the whole top of the
    frame goes under a blur.
    """
    regions = list(found)
    merged = True
    while merged:
        merged = False
        for i in range(len(regions)):
            for j in range(i + 1, len(regions)):
                joined = _one_plate(regions[i], regions[j])
                if joined is None:
                    continue
                regions = [r for k, r in enumerate(regions) if k not in (i, j)] + [joined]
                merged = True
                break
            if merged:
                break
    regions.sort(key=lambda pair: -pair[0])
    return regions


def _one_plate(
    left: tuple[float, ObscureRegion], right: tuple[float, ObscureRegion]
) -> tuple[float, ObscureRegion] | None:
    """Are these the same plate, and if so what covers both?"""
    a, b = left[1], right[1]
    top, bottom = max(a.y_pct, b.y_pct), min(a.y_pct + a.h_pct, b.y_pct + b.h_pct)
    shared = max(0.0, bottom - top)
    if shared / max(1e-6, min(a.h_pct, b.h_pct)) < _BAND_OVERLAP:
        return None

    gap = max(a.x_pct, b.x_pct) - min(a.x_pct + a.w_pct, b.x_pct + b.w_pct)
    if gap > _BAND_GAP_PCT:
        return None

    x = min(a.x_pct, b.x_pct)
    y = min(a.y_pct, b.y_pct)
    w = max(a.x_pct + a.w_pct, b.x_pct + b.w_pct) - x
    h = max(a.y_pct + a.h_pct, b.y_pct + b.h_pct) - y
    if w > 60.0 or w * h / 100.0 > 20.0:
        # The join would make something bigger than any graphic this looks for.
        return None

    return max(left[0], right[0]), a.model_copy(
        update={"x_pct": x, "y_pct": y, "w_pct": w, "h_pct": h}
    )


def _region_for(
    blob: list[tuple[int, int]],
    motion: np.ndarray,
    structure: np.ndarray,
    *,
    still: float,
    textured: float,
) -> tuple[float, ObscureRegion] | None:
    """One connected clump of static cells, judged and measured.

    Every rejection here corresponds to something that is static and structured
    and is not a logo: a letterbox boundary, a scoreboard-sized patch of
    stationary crowd, a handful of scattered cells that share a bounding box by
    accident.
    """
    if len(blob) < 3:
        return None

    rows = [r for r, _ in blob]
    cols = [c for _, c in blob]
    top, bottom = min(rows), max(rows) + 1
    left, right = min(cols), max(cols) + 1

    grid_h, grid_w = motion.shape
    x_pct = left / grid_w * 100.0
    y_pct = top / grid_h * 100.0
    w_pct = (right - left) / grid_w * 100.0
    h_pct = (bottom - top) / grid_h * 100.0

    if w_pct < 1.5 or h_pct < 1.2:
        return None
    if w_pct > 60.0 or h_pct > 40.0 or w_pct * h_pct / 100.0 > 20.0:
        return None
    # A letterbox seam: the full width of the frame and almost no height. Static
    # and edged, and blurring it would draw a line across the picture.
    if w_pct > 85.0 and h_pct < 4.0:
        return None
    if len(blob) / max(1, (right - left) * (bottom - top)) < _MIN_FILL:
        return None

    window = (slice(top, bottom), slice(left, right))
    stillness = _clamp01(1.0 - float(motion[window].mean()) / max(still, 1e-6))
    distinct = _clamp01(float(structure[window].mean()) / max(textured * 3.0, 1e-6))
    # Broadcast marks sit against an edge of the frame far more often than in
    # the middle of it. A nudge rather than a rule: a centre-screen watermark is
    # unusual, not impossible, and a rule here would make it undetectable.
    margin = min(x_pct, y_pct, 100.0 - x_pct - w_pct, 100.0 - y_pct - h_pct)
    confidence = _clamp01(0.55 * stillness + 0.30 * distinct + (0.15 if margin < 12.0 else 0.0))

    # A cell of padding all round. Logos have soft edges, drop shadows and
    # anti-aliasing that do not meet the structure test but are visible when
    # everything around them has gone.
    pad_x = 100.0 / grid_w
    pad_y = 100.0 / grid_h
    return confidence, ObscureRegion(
        x_pct=max(0.0, x_pct - pad_x),
        y_pct=max(0.0, y_pct - pad_y),
        w_pct=min(100.0, w_pct + 2 * pad_x),
        h_pct=min(100.0, h_pct + 2 * pad_y),
        found=ObscureFound.AUTO,
        confidence=round(confidence, 3),
        label=None,
    )


def _components(mask: np.ndarray) -> list[list[tuple[int, int]]]:
    """Eight-connected clumps of True cells.

    Written out rather than imported: scipy is the only thing in reach that has
    this, and it is a hundred megabytes to label an 80x45 grid.
    """
    seen = np.zeros_like(mask, dtype=bool)
    height, width = mask.shape
    blobs: list[list[tuple[int, int]]] = []

    for row in range(height):
        for col in range(width):
            if not mask[row, col] or seen[row, col]:
                continue
            seen[row, col] = True
            queue = deque([(row, col)])
            blob: list[tuple[int, int]] = []
            while queue:
                r, c = queue.popleft()
                blob.append((r, c))
                for dr in (-1, 0, 1):
                    for dc in (-1, 0, 1):
                        nr, nc = r + dr, c + dc
                        if (
                            0 <= nr < height
                            and 0 <= nc < width
                            and mask[nr, nc]
                            and not seen[nr, nc]
                        ):
                            seen[nr, nc] = True
                            queue.append((nr, nc))
            blobs.append(blob)
    return blobs


def _cells(plane: np.ndarray) -> np.ndarray:
    """Average a full-resolution plane down onto the decision grid."""
    height = plane.shape[0] // _CELL * _CELL
    width = plane.shape[1] // _CELL * _CELL
    trimmed = plane[:height, :width]
    return trimmed.reshape(height // _CELL, _CELL, width // _CELL, _CELL).mean(axis=(1, 3))


def _gradient(plane: np.ndarray) -> np.ndarray:
    """How much structure each pixel sits in: |d/dx| + |d/dy| of the still image.

    On the median rather than on any single frame, so a compression artefact in
    one instant does not read as an edge.
    """
    horizontal = np.abs(np.diff(plane, axis=1, prepend=plane[:, :1]))
    vertical = np.abs(np.diff(plane, axis=0, prepend=plane[:1, :]))
    return horizontal + vertical


def _decode_grey(
    source: Path,
    *,
    start_sec: float,
    duration_sec: float,
    ffmpeg_bin: str,
    timeout_s: float,
) -> np.ndarray:
    """The cut as a stack of small greyscale frames, evenly spaced.

    `-ss` before `-i` so ffmpeg seeks rather than decoding from zero, which on a
    long source is the difference between a second and a minute.
    """
    rate = max(0.25, min(8.0, _SAMPLES / max(duration_sec, 0.5)))
    argv = [
        ffmpeg_bin,
        "-v",
        "error",
        "-ss",
        f"{start_sec:.3f}",
        "-t",
        f"{duration_sec:.3f}",
        "-i",
        str(source),
        "-an",
        "-vf",
        f"fps={rate:.4f},scale={_ANALYSIS_WIDTH}:{_ANALYSIS_HEIGHT},format=gray",
        "-f",
        "rawvideo",
        "-",
    ]
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, capture_output=True, timeout=timeout_s, check=False
        )
    except FileNotFoundError as exc:
        raise ObscureError(f"{ffmpeg_bin} not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ObscureError(f"looking for fixed marks timed out after {timeout_s}s") from exc

    if done.returncode != 0:
        raise ObscureError(f"could not decode the footage: {done.stderr.decode()[:400]}")

    per_frame = _ANALYSIS_WIDTH * _ANALYSIS_HEIGHT
    count = len(done.stdout) // per_frame
    if count == 0:
        return np.zeros((0, _ANALYSIS_HEIGHT, _ANALYSIS_WIDTH), dtype=np.uint8)
    usable = np.frombuffer(done.stdout[: count * per_frame], dtype=np.uint8)
    return usable.reshape(count, _ANALYSIS_HEIGHT, _ANALYSIS_WIDTH)


# ── Putting boxes together ───────────────────────────────────────────────────


def overlaps(a: ObscureRegion, b: ObscureRegion, *, threshold: float = 0.30) -> bool:
    """Do two rectangles cover enough of each other to be the same request?

    Measured against the *smaller* of the two rather than against their union,
    so a found box sitting inside a generously drawn one counts as covered. The
    reviewer's larger rectangle is then the one that survives, which is what
    they asked for.
    """
    wide = max(0.0, min(a.x_pct + a.w_pct, b.x_pct + b.w_pct) - max(a.x_pct, b.x_pct))
    tall = max(0.0, min(a.y_pct + a.h_pct, b.y_pct + b.h_pct) - max(a.y_pct, b.y_pct))
    shared = wide * tall
    if shared <= 0:
        return False
    smallest = min(a.w_pct * a.h_pct, b.w_pct * b.h_pct)
    return smallest > 0 and shared / smallest >= threshold


def merge_regions(*groups: Sequence[ObscureRegion]) -> list[ObscureRegion]:
    """Concatenate rectangles from several places, keeping the first of any pair
    that covers the same pixels.

    Order is precedence, and the caller sets it: a rectangle the reviewer drew
    for this clip beats one remembered from the channel, which beats one the
    detector found. All three are the same kind of thing by the time they get
    here, which is the whole reason a corrected box is not a special case.
    """
    kept: list[ObscureRegion] = []
    for group in groups:
        for region in group:
            if any(overlaps(region, already) for already in kept):
                continue
            kept.append(region)
    return kept


def _inside(region: ObscureRegion, box: tuple[float, float, float, float] | None) -> bool:
    if box is None:
        return True
    x_low, x_high, y_low, y_high = box
    mid_x = region.x_pct + region.w_pct / 2
    mid_y = region.y_pct + region.h_pct / 2
    return x_low <= mid_x <= x_high and y_low <= mid_y <= y_high


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))
