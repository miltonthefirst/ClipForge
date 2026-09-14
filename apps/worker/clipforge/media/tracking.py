"""Finding where the action is, so the crop can follow it.

The problem this solves is specific. A fixed 9:16 window keeps about a third of
a broadcast frame's width. In football that third is very often not the third
the ball is in, and the clip is of an empty patch of grass while something
happens off-screen. The reviewer's complaint — "it loses the ball" — is exactly
that.

## What it measures, and what it does not

It does not detect a ball. It measures **motion**, and puts the window where the
most of it is. That distinction is worth being honest about, because it predicts
the failure modes: a tracked crop follows the densest movement in frame, which
in football is usually the cluster of players around the ball and occasionally
is a substitute warming up on the touchline.

Two things make that work better than it sounds:

* **The window, not the centroid.** A centroid of motion is pulled to the middle
  by anything symmetrical, and a camera pan makes the entire frame move, so a
  centroid tracker sits uselessly at 50% for most of a match. Instead this
  scores every candidate window position by how much motion it *contains* and
  takes the best — which answers the question the crop actually poses.
* **A speed limit.** The window is not allowed to travel faster than
  `maxPanPctPerSec`, so a momentary jump to the far side of the pitch is
  smoothed into a lag rather than a whip-pan. Lagging the ball by half a second
  looks like a camera operator; arriving before the ball does not look like
  anything.

## Why no new dependency

ffmpeg decodes a small greyscale copy to a pipe and numpy does the arithmetic —
both already present and neither optional. A detector would be better and would
cost a model, a VRAM budget and a place in the broker's queue, which is a real
price for a stage that runs on the CPU lane today. If a tracked crop turns out
to be worth more than it costs, `plan_track` is the seam a detector goes behind:
its output is a list of keyframes, and nothing downstream knows how they were
arrived at.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from clipforge_contracts import PanKeyframe

from clipforge.media.framing import TARGET_RATIO
from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = [
    "TrackError",
    "TrackResult",
    "column_energy",
    "plan_track",
    "smooth_path",
    "window_fraction",
]

# The analysis resolution. 160x90 is enough to locate a cluster of players to
# within a few percent of frame width, and a hundredth of the pixels of the
# source — the whole pass costs less than a second of the render it informs.
_ANALYSIS_WIDTH = 160
_ANALYSIS_HEIGHT = 90

# Frames per second to sample. Eight is well above the rate at which a crop is
# allowed to move, so sampling is not the limiting factor; going higher buys
# nothing and costs decode time.
_SAMPLE_FPS = 8

# Differences below this are compression noise, not movement. Without the floor
# a static shot's mosquito noise produces a motion field with no structure and
# the window wanders.
_NOISE_FLOOR = 12


class TrackError(RuntimeError):
    """The footage could not be analysed."""


@dataclass(frozen=True)
class TrackResult:
    """Where the window should be, and how confident that is."""

    keyframes: list[PanKeyframe]
    # Fraction of sampled instants that had motion above the noise floor. Low
    # values mean the tracker had little to go on and its path is mostly the
    # fallback — worth surfacing rather than presenting a guess as a finding.
    coverage: float
    samples: int

    @property
    def is_confident(self) -> bool:
        return self.coverage >= 0.25 and self.samples >= 4


def window_fraction(source_aspect: float, zoom: float = 1.0) -> float:
    """How much of the frame's WIDTH the rendered crop will keep, as a fraction.

    The number the tracker has to agree with, and the one it used to get wrong.
    `clipforge.media.framing._window` makes the crop `min(iw, ih * 0.5625)`
    wide, so as a fraction of the width that is `min(1, 0.5625 / aspect)` — on
    16:9 about 0.316, which is the third-of-the-frame the integration suite
    measures.

    The bug this replaces multiplied the analysis width by TARGET_RATIO
    directly, which reads plausibly and is dimensionally wrong: TARGET_RATIO is
    a height-to-width ratio, not a fraction of width, and the source aspect was
    missing from the arithmetic entirely. That scored a window 0.5625 wide where
    the crop is 0.316 — 1.78x too big — so the tracker under-panned and could
    never frame the outer eighth of the picture at either edge.
    """
    if source_aspect <= 0:
        return 1.0
    return min(1.0, TARGET_RATIO / source_aspect) / max(1.0, zoom)


def plan_track(
    source: Path,
    *,
    start_sec: float,
    end_sec: float,
    source_aspect: float,
    smoothing_sec: float = 2.0,
    max_pan_pct_per_sec: float = 12.0,
    zoom: float = 1.0,
    ffmpeg_bin: str = "ffmpeg",
    timeout_s: float = 300.0,
) -> TrackResult:
    """Work out a crop path over one window of a source.

    Returns keyframes in **clip-relative** seconds, which is the timebase the
    contract and the reviewer both use — the clip is the only timeline anyone
    watching it can see.

    `source_aspect` is width divided by height, and it is required rather than
    defaulted because the whole measurement is meaningless without it: the
    analysis image is force-scaled to 160x90 whatever shape the source is, so
    nothing downstream can recover the aspect, and a wrong guess silently
    mis-sizes the scoring window rather than failing.
    """
    duration = max(0.0, end_sec - start_sec)
    if duration <= 0:
        raise TrackError(f"nothing to analyse between {start_sec} and {end_sec}")

    frames = _decode_grey(
        source,
        start_sec=start_sec,
        duration_sec=duration,
        ffmpeg_bin=ffmpeg_bin,
        timeout_s=timeout_s,
    )
    if len(frames) < 2:
        # One frame is not motion. A still shot is a legitimate outcome, and the
        # centre is the right answer for it.
        log.info("track.too_short", frames=len(frames))
        return TrackResult(keyframes=[PanKeyframe(at_sec=0.0, x_pct=50.0)], coverage=0.0, samples=0)

    energy = column_energy(frames)  # (samples, width)
    # The scoring window must be the crop's own width, expressed in analysis
    # columns. See `window_fraction` for why this is not simply TARGET_RATIO.
    window_px = max(1, round(_ANALYSIS_WIDTH * window_fraction(source_aspect, zoom)))

    # A frame far quieter than the clip's own norm has nothing to say about
    # where the action is: what motion it has is compression noise, spread
    # evenly, and reading a window position out of it is reading tea leaves.
    # The gate is relative to this clip rather than absolute because "quiet"
    # means something different in a floodlit stadium and a studio.
    totals = energy.sum(axis=1)
    lively = totals[totals > 0]
    gate = float(np.median(lively)) * 0.2 if lively.size else 0.0

    centres: list[float] = []
    live = 0
    for row, total in zip(energy, totals, strict=True):
        if total <= 0 or total < gate:
            # Hold the last known position rather than voting for the centre.
            # Football stops constantly — a throw-in, a substitution, a foul —
            # and a tracker that recentres on every pause spends the match
            # sliding back to the halfway line and then chasing play again.
            centres.append(centres[-1] if centres else 50.0)
            continue
        best, has_motion = _best_window(row, window_px)
        if has_motion:
            live += 1
        centres.append(best if has_motion else (centres[-1] if centres else 50.0))

    coverage = live / len(centres) if centres else 0.0
    if live == 0:
        log.info("track.no_motion", samples=len(centres))
        return TrackResult(
            keyframes=[PanKeyframe(at_sec=0.0, x_pct=50.0)], coverage=0.0, samples=len(centres)
        )

    step = 1.0 / _SAMPLE_FPS
    path = smooth_path(
        centres,
        step_sec=step,
        smoothing_sec=smoothing_sec,
        max_pan_pct_per_sec=max_pan_pct_per_sec,
    )
    keyframes = _to_keyframes(path, step_sec=step, duration_sec=duration)

    log.info(
        "track.planned",
        samples=len(centres),
        coverage=round(coverage, 2),
        keyframes=len(keyframes),
        span_pct=round(max(path) - min(path), 1) if path else 0.0,
    )
    return TrackResult(keyframes=keyframes, coverage=coverage, samples=len(centres))


# ── Measuring ────────────────────────────────────────────────────────────────


def _decode_grey(
    source: Path,
    *,
    start_sec: float,
    duration_sec: float,
    ffmpeg_bin: str,
    timeout_s: float,
) -> np.ndarray:
    """Decode the window as a stack of small greyscale frames.

    `-ss` before `-i` so ffmpeg seeks rather than decoding from zero, which on a
    long source is the difference between a second and a minute.
    """
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
        f"fps={_SAMPLE_FPS},scale={_ANALYSIS_WIDTH}:{_ANALYSIS_HEIGHT},format=gray",
        "-f",
        "rawvideo",
        "-",
    ]
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, capture_output=True, timeout=timeout_s, check=False
        )
    except FileNotFoundError as exc:
        raise TrackError(f"{ffmpeg_bin} not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise TrackError(f"analysing the footage timed out after {timeout_s}s") from exc

    if done.returncode != 0:
        raise TrackError(f"could not decode the footage: {done.stderr.decode()[:400]}")

    per_frame = _ANALYSIS_WIDTH * _ANALYSIS_HEIGHT
    count = len(done.stdout) // per_frame
    if count == 0:
        raise TrackError("the footage decoded to no frames")
    usable = np.frombuffer(done.stdout[: count * per_frame], dtype=np.uint8)
    return usable.reshape(count, _ANALYSIS_HEIGHT, _ANALYSIS_WIDTH)


def column_energy(frames: np.ndarray) -> np.ndarray:
    """How much each column of the frame changed, per sampled instant.

    Absolute difference between consecutive frames, noise-floored, summed down
    each column. The result is one row per instant and one value per column of
    the analysis image — a horizontal profile of "where things are happening".
    """
    diff = np.abs(frames[1:].astype(np.int16) - frames[:-1].astype(np.int16))
    diff[diff < _NOISE_FLOOR] = 0
    return diff.sum(axis=1).astype(np.float64)  # sum over rows -> (n-1, width)


def _best_window(row: np.ndarray, window_px: int) -> tuple[float, bool]:
    """The window position containing the most motion, as a percentage.

    A sliding sum rather than a centroid: the question a crop asks is "how much
    of the action is inside me", and a centroid answers a different one that
    happens to agree only when the motion is unimodal. It is a prefix-sum, so
    the cost does not depend on the window width.

    **Ties decide the picture.** Whenever the action is narrower than the
    window — which is most of the time, since the window is a third of the
    frame — many positions contain all of it and score identically. Taking the
    first of them, as `argmax` does, pins the subject to the right-hand edge of
    the crop for no reason and makes a stationary subject look mis-framed. So
    among the positions that score within a hair of the best, this takes the one
    whose centre is nearest the motion's centre of mass: the action ends up in
    the middle of the shot when the frame allows it, and as near the middle as
    the frame allows when it does not.
    """
    total = float(row.sum())
    if total <= 0:
        return 50.0, False

    width = row.shape[0]
    window = min(window_px, width)
    cumulative = np.concatenate(([0.0], np.cumsum(row)))
    sums = cumulative[window:] - cumulative[: width - window + 1]

    best = float(sums.max())
    # Relative tolerance, so it means the same thing on quiet and busy frames.
    contenders = np.flatnonzero(sums >= best - max(1e-9, abs(best) * 1e-6))
    centroid = float((np.arange(width) * row).sum() / total)
    centres = contenders + window / 2.0
    left = int(contenders[int(np.argmin(np.abs(centres - centroid)))])

    centre_px = left + window / 2.0
    return 100.0 * centre_px / width, True


# ── Shaping ──────────────────────────────────────────────────────────────────


def smooth_path(
    centres: list[float],
    *,
    step_sec: float,
    smoothing_sec: float,
    max_pan_pct_per_sec: float,
) -> list[float]:
    """Turn per-instant positions into something a camera could have done.

    Two passes, and the order matters. The moving average first, because it is
    what removes the frame-to-frame jitter that a speed limit would otherwise
    faithfully preserve at its maximum rate. The speed limit second, because it
    is the constraint that must hold in the output — smoothing afterwards would
    reintroduce violations of it.
    """
    if not centres:
        return []

    window = max(1, round(smoothing_sec / step_sec))
    if window > 1:
        # Edge-padded so the path does not sag toward the middle at its ends,
        # which on a short clip is most of the clip.
        padded = np.pad(
            np.asarray(centres, dtype=np.float64), (window // 2, window // 2), mode="edge"
        )
        kernel = np.ones(window) / window
        averaged = np.convolve(padded, kernel, mode="valid")[: len(centres)]
    else:
        averaged = np.asarray(centres, dtype=np.float64)

    max_step = max_pan_pct_per_sec * step_sec
    limited = [float(averaged[0])]
    for value in averaged[1:]:
        previous = limited[-1]
        delta = float(value) - previous
        if max_step > 0:
            delta = max(-max_step, min(max_step, delta))
        limited.append(previous + delta)
    return limited


def _to_keyframes(
    path: list[float], *, step_sec: float, duration_sec: float, tolerance_pct: float = 1.0
) -> list[PanKeyframe]:
    """Reduce a dense path to the fewest points that still describe it.

    The render interpolates linearly between keyframes, so any point lying on
    the line between its neighbours carries no information. Dropping them keeps
    the stored path readable by a person, which matters because a reviewer who
    disagrees with the tracker edits these numbers by hand.
    """
    if not path:
        return [PanKeyframe(at_sec=0.0, x_pct=50.0)]

    points = [(index * step_sec, value) for index, value in enumerate(path)]
    kept = [points[0]]
    for index in range(1, len(points) - 1):
        at, value = points[index]
        last_at, last_value = kept[-1]
        next_at, next_value = points[index + 1]
        span = next_at - last_at
        if span <= 0:
            continue
        interpolated = last_value + (next_value - last_value) * (at - last_at) / span
        if abs(value - interpolated) > tolerance_pct:
            kept.append((at, value))
    if len(points) > 1:
        kept.append(points[-1])

    return [
        PanKeyframe(
            at_sec=round(min(at, duration_sec), 3),
            x_pct=round(max(0.0, min(100.0, v)), 2),
        )
        for at, v in kept
    ]
