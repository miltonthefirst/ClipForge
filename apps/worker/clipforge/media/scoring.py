"""Running a music mix end to end: fetch, analyse, plan, encode.

The one place that turns "put this track under this picture" into a finished
file. It sits beside :mod:`clipforge.media.render` because that is the module
that already owns running ffmpeg, and everything above it — MUSIC scoring a
clip, REMAKE carrying an approved soundtrack across a re-cut — goes through here
rather than assembling the same five calls again.

Two calls, not one, is how the music was lost in the first place. A remake
re-encoded the picture from the original source, the music went with the audio
it replaced, and the clip was still saved claiming a soundtrack the file did not
contain. Sharing the seam is what makes "carry the music" a thing REMAKE can do
at all.

## The order the mix has to happen in

Music goes on **after** the narration, never before. Both `MusicMode.REPLACE`
and `SpeechMode.REPLACE` drop ``[0:a]`` from the graph entirely, so a mix that
scored the clip first would have its music thrown away the moment the reviewer
asked for a replaced voice — which is the default. The other way round works
because it is what both modules were written for: the bed's sidechain key
becomes the finished narration, and the music ducks under the new voice.

## Why the caller passes the duration

``duration_sec`` is the *probed length of the file at* ``picture``, always,
because ``-t`` bounds the copied video stream as well as the looping music: a
plan built from a 12-second window against a 20.000-second picture produced a
12.067-second file and threw the rest of the footage away.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from clipforge_contracts import MusicMode

from clipforge.media.beats import HOP, SAMPLE_RATE, BeatAnalysis, analyse
from clipforge.media.ffprobe import probe
from clipforge.media.music import MusicPlan, build_ffmpeg_args, plan_music
from clipforge.media.sources import resolve_audio_source
from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = ["MIX_TIMEOUT_S", "AppliedMusicResult", "ScoringError", "apply_music"]

# The same bound the render ceiling uses, for the same reason: the worst case is
# one ffmpeg pass over one clip. The mix itself measures under a second, so this
# is not a deadline anyone is expected to meet — it is what stops a wedged
# ffmpeg from becoming an immortal job. Nothing else would stop it, because the
# reaper deliberately leaves alone any job its own worker is still running
# (JobStore.reap), so a process that never exits holds its lease for as long as
# the worker lives.
MIX_TIMEOUT_S = 1800.0


class ScoringError(RuntimeError):
    """The mix ran and refused. Never retryable.

    Every failure that reaches this is a property of the inputs — a track with
    no audio, a picture ffmpeg will not read. The same invocation says the same
    no twice more, and the operator is left watching a job that looks slow.

    A mix that *hung* is deliberately not this: see :func:`_run`.
    """


@dataclass(frozen=True)
class AppliedMusicResult:
    """The scored file, and the numbers needed to describe it afterwards."""

    path: Path
    plan: MusicPlan
    track_title: str | None
    """What the source called the track, or ``None`` when nothing named it."""


def apply_music(
    *,
    picture: Path,
    destination: Path,
    duration_sec: float,
    source: str,
    mode: MusicMode,
    gain_db: float | None,
    align_to_beat: bool,
    forced_start_sec: float | None,
    forced_tempo_bpm: float | None,
    has_original_audio: bool,
    work_dir: Path,
    ffmpeg: str,
    ffprobe: str,
    on_progress: Callable[[str], None] | None = None,
) -> AppliedMusicResult:
    """Score ``picture`` with ``source`` and write the result to ``destination``.

    ``duration_sec`` must be the probed length of ``picture`` — see the module
    docstring for the 20-second clip this cost.

    ``forced_start_sec`` and ``forced_tempo_bpm`` come off an
    :class:`~clipforge_contracts.AppliedMusic` that has already been approved.
    Given **both**, the track is not analysed at all: the two numbers analysis
    would produce are the two numbers already in hand, and re-deriving them is a
    decode of the whole track to arrive back where we started. Given one or
    neither, a fresh analysis is the only honest answer — and the caller should
    say so, because a duration that moved moves the excerpt with it.

    ``has_original_audio`` is a property of ``picture``, and a ``BED`` against a
    picture that has none is downgraded rather than failed; see
    :func:`clipforge.media.music.plan_music`.

    ``on_progress`` is how the waiting is narrated without this module knowing
    what a stage or a job is. The fetch and the analysis are both tens of
    seconds on a cold cache, and a stage that says nothing for tens of seconds
    is indistinguishable from one that has hung.
    """

    def say(sentence: str) -> None:
        if on_progress is not None:
            on_progress(sentence)

    if duration_sec <= 0:
        raise ScoringError("the picture has no duration, so no excerpt can be planned")

    say("Fetching the track")
    track = resolve_audio_source(source, work_dir, ffmpeg=ffmpeg, ffprobe=ffprobe)

    if forced_start_sec is not None and forced_tempo_bpm is not None:
        analysis = _recorded_analysis(track.path, forced_tempo_bpm, ffprobe=ffprobe)
    else:
        say("Analysing the beat grid")
        analysis = analyse(track.path, ffmpeg)

    plan = plan_music(
        analysis,
        clip_duration_sec=duration_sec,
        mode=mode,
        gain_db=gain_db,
        align_to_beat=align_to_beat,
        has_original_audio=has_original_audio,
        start_sec=forced_start_sec,
    )

    destination.parent.mkdir(parents=True, exist_ok=True)

    say("Mixing the music into the clip")
    _run(
        build_ffmpeg_args(
            plan,
            clip_path=str(picture),
            music_path=str(track.path),
            output_path=str(destination),
            # The picture arriving here is always already encoded — by RENDER,
            # by a reframe, or by a caption-free re-cut — so copying the video
            # stream is both correct and free.
            reencode_video=False,
            ffmpeg=ffmpeg,
        ),
        what="mixing the music",
    )

    log.info(
        "music.mixed",
        track=track.path.name,
        mode=plan.mode.value,
        requested_mode=mode.value,
        duration_sec=plan.duration_sec,
        music_start_sec=plan.music_start_sec,
        tempo_bpm=round(plan.tempo_bpm, 1) if plan.tempo_bpm else None,
        analysed=forced_start_sec is None or forced_tempo_bpm is None,
    )
    return AppliedMusicResult(path=destination, plan=plan, track_title=track.title)


def _recorded_analysis(track: Path, tempo_bpm: float, *, ffprobe: str) -> BeatAnalysis:
    """Stand in for :func:`clipforge.media.beats.analyse` from what is on record.

    Only reachable when the start is forced too, which is what makes it safe:
    the beat grid and the onset envelope exist solely to choose and snap a
    start, and neither is consulted once the start is given. The track's length
    is, so it is read from the container rather than by decoding the samples —
    the only thing still riding on it is whether a short track has to loop.
    """
    return BeatAnalysis(
        duration_sec=probe(track, ffprobe_bin=ffprobe).duration_sec,
        tempo_bpm=tempo_bpm,
        beat_times=(),
        onset_env=np.zeros(0, dtype=np.float32),
        hop_sec=HOP / SAMPLE_RATE,
    )


def _run(argv: list[str], *, what: str, timeout_s: float = MIX_TIMEOUT_S) -> None:
    """Run ffmpeg and turn a failure into something a reviewer can act on."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired as exc:
        # A TimeoutError, not a ScoringError, and the difference is the job's
        # remaining attempts. ScoringError means the request itself cannot work,
        # so the runner gives up on the spot; a wedged ffmpeg is nothing of the
        # sort, and the attempt that comes after is the whole value of having
        # noticed. TimeoutError is already what StageRunner._record_failure
        # reads as retryable, so this needs no classification of its own.
        raise TimeoutError(f"{what} timed out after {timeout_s}s") from exc
    if proc.returncode != 0:
        raise ScoringError(f"{what} failed: {proc.stderr[-600:]}")
