"""Tempo, beat grid, and which part of a track is worth using.

## Why this is not librosa

librosa does all of this and does it better, and it costs scipy, scikit-learn
and numba to install — several hundred megabytes, in a worker whose core
dependencies are deliberately light enough to install on a machine with no GPU
and no CUDA (apps/worker/pyproject.toml). Beat tracking for a thirty-second clip
does not justify a machine-learning stack.

What is here is the classic spectral-flux pipeline, in numpy: onset strength
from the positive change in spectral energy, tempo from the autocorrelation of
that envelope, and a beat grid phase-aligned by trying every offset within one
beat and keeping the one the onsets agree with. It is perhaps eighty lines of
arithmetic and it is accurate on music with a steady pulse, which is what people
put under clips. It will not follow a rubato piano piece, and it says so rather
than inventing a tempo — see :func:`analyse` returning ``tempo_bpm=None``.

## Why ffmpeg does the decoding

Because it already does everywhere else in this codebase, and because it means
no soundfile, no audioread, no second opinion about what an mp3 contains. Raw
mono float32 comes out of a pipe and straight into numpy.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = ["BeatAnalysis", "analyse", "decode_mono", "pick_section"]

# 22.05 kHz is plenty: everything that marks a beat lives well below 11 kHz, and
# halving the rate halves the arithmetic.
SAMPLE_RATE = 22_050

# ~46 ms windows with 75% overlap. Long enough for a stable spectrum, short
# enough to place an onset within a twentieth of a second.
FRAME = 1024
HOP = 256

# The range worth searching. Below 60 the autocorrelation starts agreeing with
# the bar rather than the beat; above 180 it locks onto subdivisions.
MIN_BPM = 60.0
MAX_BPM = 180.0


@dataclass(frozen=True)
class BeatAnalysis:
    """What the arithmetic found in a track."""

    duration_sec: float
    tempo_bpm: float | None
    """None when no tempo was clear enough to act on. Not a guess."""

    beat_times: tuple[float, ...]
    """Beat positions in seconds. Empty when there is no tempo."""

    onset_env: np.ndarray
    """Onset strength per frame — also the energy curve section picking uses."""

    hop_sec: float

    @property
    def beat_sec(self) -> float | None:
        return 60.0 / self.tempo_bpm if self.tempo_bpm else None


def decode_mono(
    path: Path, ffmpeg: str = "ffmpeg", *, sample_rate: int = SAMPLE_RATE
) -> np.ndarray:
    """Decode any audio ffmpeg understands into mono float32.

    Raises on a non-zero exit rather than returning silence: an empty array is
    indistinguishable from a quiet track, and analysing silence produces a
    confident answer about nothing.
    """
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "a:0",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "f32le",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"could not decode audio from {path.name}: {proc.stderr.decode(errors='replace')[:400]}"
        )

    samples = np.frombuffer(proc.stdout, dtype=np.float32)
    if samples.size == 0:
        raise RuntimeError(f"{path.name} contains no audio")
    return samples


def _onset_envelope(samples: np.ndarray) -> np.ndarray:
    """Positive spectral flux: how much the spectrum grew, frame to frame.

    Growth only. A cymbal decaying is not an onset, and counting the fall as
    well as the rise smears every transient into a plateau.
    """
    if samples.size < FRAME:
        return np.zeros(0, dtype=np.float32)

    window = np.hanning(FRAME).astype(np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(samples, FRAME)[::HOP]
    spectra = np.abs(np.fft.rfft(frames * window, axis=1))

    # Log compression, so a loud passage does not drown out the onsets in a
    # quiet one — the beat is where energy *changes*, not where it is greatest.
    spectra = np.log1p(spectra)

    flux = np.diff(spectra, axis=0)
    envelope = np.maximum(flux, 0.0).sum(axis=1)

    # Subtract a local median: removes the slow swell of an arrangement and
    # leaves the spikes.
    if envelope.size > 16:
        smoothed = np.convolve(envelope, np.ones(16) / 16, mode="same")
        envelope = np.maximum(envelope - smoothed, 0.0)

    peak = envelope.max()
    return (envelope / peak).astype(np.float32) if peak > 0 else envelope.astype(np.float32)


def _estimate_tempo(envelope: np.ndarray, hop_sec: float) -> float | None:
    """Tempo from the autocorrelation of the onset envelope.

    Returns None when the best lag is not meaningfully better than the average,
    which is what a track with no steady pulse looks like. Reporting no tempo is
    a useful answer: the caller skips alignment instead of nudging a clip to a
    beat that is not there.
    """
    if envelope.size < 32:
        return None

    centred = envelope - envelope.mean()
    correlation = np.correlate(centred, centred, mode="full")[centred.size - 1 :]
    if correlation[0] <= 0:
        return None
    correlation = correlation / correlation[0]

    min_lag = max(1, round(60.0 / MAX_BPM / hop_sec))
    max_lag = min(correlation.size - 1, round(60.0 / MIN_BPM / hop_sec))
    if max_lag <= min_lag:
        return None

    window = correlation[min_lag : max_lag + 1]
    best = int(np.argmax(window))
    strength = float(window[best])

    # A flat autocorrelation means no periodicity worth the name. The threshold
    # is empirical and deliberately cautious: a wrong tempo moves the clip's
    # start to the wrong place, which is worse than not moving it at all.
    if strength < 0.15 or strength <= float(window.mean()) * 1.5:
        return None

    return 60.0 / ((best + min_lag) * hop_sec)


def _beat_grid(envelope: np.ndarray, hop_sec: float, tempo_bpm: float) -> tuple[float, ...]:
    """Place a click train at the phase the onsets most agree with.

    Every offset within one beat is tried and scored by the onset strength it
    lands on. Brute force over a few hundred candidates, which is nothing, and
    it avoids the failure where a grid at the right tempo sits exactly off-beat.
    """
    beat_sec = 60.0 / tempo_bpm
    beat_frames = beat_sec / hop_sec
    if beat_frames < 1 or envelope.size == 0:
        return ()

    best_offset, best_score = 0.0, -1.0
    for offset in np.linspace(0, beat_frames, num=max(8, int(beat_frames)), endpoint=False):
        indices = np.round(np.arange(offset, envelope.size, beat_frames)).astype(int)
        indices = indices[indices < envelope.size]
        if indices.size == 0:
            continue
        score = float(envelope[indices].mean())
        if score > best_score:
            best_offset, best_score = float(offset), score

    times = np.arange(best_offset, envelope.size, beat_frames) * hop_sec
    return tuple(float(t) for t in times)


def analyse(path: Path, ffmpeg: str = "ffmpeg") -> BeatAnalysis:
    """Decode a track and work out its pulse."""
    samples = decode_mono(path, ffmpeg)
    duration = samples.size / SAMPLE_RATE
    hop_sec = HOP / SAMPLE_RATE

    envelope = _onset_envelope(samples)
    tempo = _estimate_tempo(envelope, hop_sec)
    beats = _beat_grid(envelope, hop_sec, tempo) if tempo else ()

    log.info(
        "music.analysed",
        track=path.name,
        duration_sec=round(duration, 2),
        tempo_bpm=round(tempo, 1) if tempo else None,
        beats=len(beats),
    )
    return BeatAnalysis(
        duration_sec=duration,
        tempo_bpm=tempo,
        beat_times=beats,
        onset_env=envelope,
        hop_sec=hop_sec,
    )


def pick_section(analysis: BeatAnalysis, want_sec: float) -> float:
    """Where in the track to start, to get the best ``want_sec`` of it.

    A track's first bars are usually its least interesting — an intro exists to
    get out of the way. So this slides a window over the onset envelope and
    takes the most energetic position, then moves the result back to the nearest
    beat so the excerpt begins on one rather than mid-bar.

    Returns 0.0 when the track is no longer than the clip, which is the honest
    answer: there is no section to choose.
    """
    if analysis.duration_sec <= want_sec or analysis.onset_env.size == 0:
        return 0.0

    frames = max(1, round(want_sec / analysis.hop_sec))
    if frames >= analysis.onset_env.size:
        return 0.0

    # Cumulative sum makes every window's total a subtraction rather than a sum.
    cumulative = np.concatenate([[0.0], np.cumsum(analysis.onset_env, dtype=np.float64)])
    totals = cumulative[frames:] - cumulative[:-frames]
    start_sec = float(int(np.argmax(totals)) * analysis.hop_sec)

    if analysis.beat_times:
        # Backwards to the previous beat, never forwards: moving forward would
        # clip the transient the window was chosen for.
        earlier = [t for t in analysis.beat_times if t <= start_sec]
        if earlier:
            start_sec = earlier[-1]

    # Never so late that the excerpt runs off the end of the track.
    return min(start_sec, max(0.0, analysis.duration_sec - want_sec))
