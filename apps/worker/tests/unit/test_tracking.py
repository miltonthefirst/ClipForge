"""The motion tracker's arithmetic, without decoding anything.

`plan_track` needs ffmpeg and real footage, so it lives in the integration tier.
What is here is the part that decides where the window goes given a motion
field — which is where the bugs were, and all three of them were invisible in a
rendered clip:

* ties broken leftward, pinning a stationary subject to the edge of frame;
* a silent instant voting for dead centre, so the crop drifted to the halfway
  line every time play stopped;
* a smoothing pass that sagged toward the middle at both ends of a clip.
"""

from __future__ import annotations

from itertools import pairwise

import numpy as np
import pytest
from clipforge.media.tracking import _best_window as best_window
from clipforge.media.tracking import column_energy, smooth_path, window_fraction


def field(width: int = 160, **spikes: float) -> np.ndarray:
    """One row of column energy, with motion at named positions.

    Keys are `cNN` so they can be keyword arguments: `field(c120=50)` puts 50
    units of motion in column 120.
    """
    row = np.zeros(width, dtype=np.float64)
    for key, value in spikes.items():
        row[int(key[1:])] = value
    return row


# The crop keeps min(1, 0.5625/aspect) of the width; on 16:9 that is 0.316, so
# 51 of 160 analysis columns. This used to read 90 — the same dimensional slip
# the module had — so the test agreed with the bug and passed.
WINDOW = round(160 * window_fraction(16 / 9))


# ── Where the window goes ────────────────────────────────────────────────────


def test_motion_on_the_left_pulls_the_window_left() -> None:
    centre, live = best_window(field(c10=100.0), WINDOW)
    assert live
    assert centre < 40


def test_motion_on_the_right_pulls_the_window_right() -> None:
    centre, live = best_window(field(c150=100.0), WINDOW)
    assert live
    assert centre > 60


def test_a_subject_narrower_than_the_window_is_centred_in_it() -> None:
    """The tie-break that decides most frames.

    Any window position containing all the motion scores identically, so
    `argmax` alone would take the leftmost and hang the subject off the right
    edge of the shot. Among equal scores the one centred on the action wins.
    """
    centre, _ = best_window(field(c80=100.0), WINDOW)
    assert centre == pytest.approx(50.0, abs=1.0)


def test_a_subject_too_near_the_edge_gets_as_close_to_centred_as_it_can() -> None:
    centre, _ = best_window(field(c155=100.0), WINDOW)
    # The window centre cannot exceed (width - window/2) / width.
    assert centre == pytest.approx(100.0 * (160 - WINDOW / 2) / 160, abs=0.5)


def test_the_window_containing_more_motion_beats_the_one_containing_less() -> None:
    """Not the centroid: two clusters of different size should not average.

    A centroid would sit between them, containing neither. The window goes to
    the bigger one.
    """
    row = field(c20=10.0, c140=100.0)
    centre, _ = best_window(row, WINDOW)
    assert centre > 55


def test_a_frame_with_no_motion_reports_that_rather_than_guessing() -> None:
    centre, live = best_window(np.zeros(160), WINDOW)
    assert not live
    assert centre == 50.0


# ── Measuring motion ─────────────────────────────────────────────────────────


def test_column_energy_is_the_difference_between_frames() -> None:
    frames = np.zeros((2, 4, 8), dtype=np.uint8)
    frames[1, :, 5] = 200
    energy = column_energy(frames)
    assert energy.shape == (1, 8)
    assert energy[0, 5] > 0
    assert energy[0, 0] == 0


def test_a_still_frame_has_no_energy() -> None:
    frames = np.full((3, 4, 8), 120, dtype=np.uint8)
    assert column_energy(frames).sum() == 0


def test_compression_noise_is_floored_away() -> None:
    """Small differences everywhere are mosquito noise, not movement.

    Without the floor a static shot produces a structureless motion field and
    the window wanders around it.
    """
    frames = np.full((2, 4, 8), 120, dtype=np.uint8)
    frames[1] = 128  # a difference of 8, below the floor of 12
    assert column_energy(frames).sum() == 0

    frames[1] = 140  # a difference of 20, above it
    assert column_energy(frames).sum() > 0


# ── Shaping the path ─────────────────────────────────────────────────────────


def test_the_speed_limit_is_enforced() -> None:
    jump = [10.0] * 8 + [90.0] * 8
    path = smooth_path(jump, step_sec=0.125, smoothing_sec=0.0, max_pan_pct_per_sec=8.0)
    worst = max(abs(b - a) for a, b in pairwise(path))
    assert worst <= 8.0 * 0.125 + 1e-6


def test_a_limited_path_still_travels_the_whole_way_eventually() -> None:
    """Rate-limiting is a lag, not a ceiling on where the window can end up."""
    jump = [10.0] + [90.0] * 200
    path = smooth_path(jump, step_sec=0.125, smoothing_sec=0.0, max_pan_pct_per_sec=20.0)
    assert path[-1] == pytest.approx(90.0, abs=0.5)


def test_smoothing_removes_jitter() -> None:
    jitter = [50.0, 56.0, 44.0, 55.0, 45.0, 54.0, 46.0, 50.0]
    smoothed = smooth_path(jitter, step_sec=0.125, smoothing_sec=0.5, max_pan_pct_per_sec=100.0)
    assert np.std(smoothed) < np.std(jitter)


def test_smoothing_does_not_sag_at_the_ends() -> None:
    """Edge padding, not zero padding.

    On a fifteen-second clip the smoothing window is a meaningful fraction of
    the whole thing, so a convolution that treats the outside as zero drags the
    first and last seconds toward the left edge of the frame.
    """
    steady = [80.0] * 16
    smoothed = smooth_path(steady, step_sec=0.125, smoothing_sec=1.0, max_pan_pct_per_sec=100.0)
    assert smoothed[0] == pytest.approx(80.0, abs=0.5)
    assert smoothed[-1] == pytest.approx(80.0, abs=0.5)


def test_an_empty_path_stays_empty() -> None:
    assert smooth_path([], step_sec=0.125, smoothing_sec=1.0, max_pan_pct_per_sec=10.0) == []


def test_no_speed_limit_means_no_limiting() -> None:
    jump = [10.0, 90.0]
    path = smooth_path(jump, step_sec=0.125, smoothing_sec=0.0, max_pan_pct_per_sec=0.0)
    assert path[-1] == pytest.approx(90.0)
