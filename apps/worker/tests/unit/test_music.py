"""Beat analysis and the mix plan.

Two things are worth pinning here, and neither is "does ffmpeg work".

**The analysis says when it does not know.** A tempo it is not sure about is
worse than no tempo, because the caller acts on it — a confident wrong answer
starts the music off-beat, and off-beat is more noticeable than unaligned.

**The filtergraph is the right shape.** It is a string handed to ffmpeg, and a
wrong one costs a full encode to discover. `build_audio_filter` is pure so the
distinction that matters — REPLACE must not reference the clip's audio at all —
can be asserted in microseconds.

**Neither ffmpeg call can run forever.** One is the decode in the media layer
and one is the mix in `clipforge.media.scoring`, which is how both came to be
missed, and an unbounded ffmpeg is how a job becomes immortal: the reaper
deliberately leaves alone any job its own worker is still running, so nothing
else would ever cut it off.

**And a mix that hung is not the same failure as a mix that refused.** What the
two types cost a job is asserted here, from the scheduler's end; which type the
mix raises is asserted in test_scoring.py, beside the mix itself.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from clipforge.config import Settings
from clipforge.media import scoring
from clipforge.media.beats import BeatAnalysis, analyse, decode_mono, pick_section
from clipforge.media.music import build_audio_filter, build_ffmpeg_args, plan_music
from clipforge.models.broker import ModelBroker
from clipforge.scheduler.lease import Transition
from clipforge.scheduler.runner import StageRunner
from clipforge.stages.base import StageContext, StageOutcome, StageRegistry
from clipforge_contracts import (
    Job,
    JobStatus,
    JobType,
    Lane,
    MusicMode,
    Stage,
    StageName,
    StageStatus,
)


def click_track(path: Path, bpm: float, seconds: float = 20.0) -> Path:
    """A steady pulse at a known tempo, for asserting against."""
    beat = 60.0 / bpm
    one = path.with_name("one.wav")
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1200:duration=0.03",
            "-af",
            f"apad=whole_dur={beat}",
            str(one),
        ],
        check=True,
    )
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-stream_loop",
            str(int(seconds / beat) + 4),
            "-i",
            str(one),
            "-t",
            str(seconds),
            str(path),
        ],
        check=True,
    )
    return path


def synthetic(tempo: float | None, duration: float = 30.0) -> BeatAnalysis:
    """An analysis without decoding anything, for the planning tests."""
    hop = 256 / 22_050
    frames = int(duration / hop)
    envelope = np.zeros(frames, dtype=np.float32)
    # A ramp, so the most energetic window is unambiguously at the end.
    envelope[:] = np.linspace(0, 1, frames)
    beats = tuple(np.arange(0, duration, 60.0 / tempo)) if tempo else ()
    return BeatAnalysis(
        duration_sec=duration,
        tempo_bpm=tempo,
        beat_times=tuple(float(b) for b in beats),
        onset_env=envelope,
        hop_sec=hop,
    )


# ── Analysis ─────────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("bpm", [90.0, 120.0, 140.0])
def test_a_steady_pulse_is_measured_within_two_percent(tmp_path: Path, bpm: float) -> None:
    result = analyse(click_track(tmp_path / f"{bpm}.wav", bpm))

    assert result.tempo_bpm is not None
    assert abs(result.tempo_bpm - bpm) / bpm < 0.02
    assert len(result.beat_times) > 0


@pytest.mark.unit
def test_noise_reports_no_tempo_rather_than_guessing(tmp_path: Path) -> None:
    """The answer that keeps a wrong alignment from happening at all."""
    noise = tmp_path / "noise.wav"
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anoisesrc=d=15:c=pink",
            "-ar",
            "22050",
            str(noise),
        ],
        check=True,
    )

    result = analyse(noise)

    assert result.tempo_bpm is None
    assert result.beat_times == ()


@pytest.mark.unit
def test_a_file_with_no_audio_is_an_error_not_an_empty_analysis(tmp_path: Path) -> None:
    """Silence and 'no audio' analyse identically, and only one is a mistake."""
    silent = tmp_path / "silent.mp4"
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x64:rate=10:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(silent),
        ],
        check=True,
    )

    with pytest.raises(RuntimeError, match="audio"):
        analyse(silent)


# ── Section picking ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_a_track_no_longer_than_the_clip_starts_at_zero() -> None:
    """There is no section to choose, and pretending otherwise would truncate."""
    assert pick_section(synthetic(120.0, duration=10.0), want_sec=12.0) == 0.0


@pytest.mark.unit
def test_the_excerpt_never_runs_off_the_end_of_the_track() -> None:
    analysis = synthetic(120.0, duration=30.0)
    start = pick_section(analysis, want_sec=8.0)
    assert 0.0 <= start <= 30.0 - 8.0


@pytest.mark.unit
def test_the_opening_bars_are_not_the_default_choice() -> None:
    """A track's intro exists to get out of the way; the energy ramp here means
    anything but the start is the better answer."""
    assert pick_section(synthetic(120.0, duration=40.0), want_sec=6.0) > 0.0


# ── Planning ─────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_alignment_puts_the_excerpt_on_a_beat() -> None:
    analysis = synthetic(120.0, duration=40.0)  # a beat every 0.5s
    plan = plan_music(analysis, clip_duration_sec=8.0, mode=MusicMode.BED)

    nearest = min(analysis.beat_times, key=lambda b: abs(b - plan.music_start_sec))
    assert plan.music_start_sec == pytest.approx(nearest, abs=0.001)


@pytest.mark.unit
def test_alignment_is_skipped_when_there_is_no_tempo_to_align_to() -> None:
    plan = plan_music(synthetic(None, duration=40.0), clip_duration_sec=8.0, mode=MusicMode.BED)

    assert plan.tempo_bpm is None
    assert plan.music_start_sec >= 0.0


@pytest.mark.unit
def test_a_bed_is_attenuated_and_a_replacement_is_not() -> None:
    """A bed sits under speech; a replacement *is* the soundtrack."""
    analysis = synthetic(120.0)
    bed = plan_music(analysis, clip_duration_sec=8.0, mode=MusicMode.BED)
    replace = plan_music(analysis, clip_duration_sec=8.0, mode=MusicMode.REPLACE)

    assert bed.gain_db < 0
    assert replace.gain_db == 0


@pytest.mark.unit
def test_an_explicit_gain_overrides_the_stages_judgement() -> None:
    plan = plan_music(synthetic(120.0), clip_duration_sec=8.0, mode=MusicMode.BED, gain_db=-3.0)
    assert plan.gain_db == -3.0


@pytest.mark.unit
def test_a_bed_under_a_picture_with_no_audio_becomes_a_replacement() -> None:
    """Not a cosmetic fallback. The BED graph names `[0:a]`, and against a
    video-only input ffmpeg refuses the whole invocation — *"Stream specifier
    ':a' ... matches no streams"*, exit -22, no file written. Measured against
    assets/fixtures/video-only.mp4. A silent source is something the reviewer
    could not have known about when they asked for a bed."""
    plan = plan_music(
        synthetic(120.0),
        clip_duration_sec=8.0,
        mode=MusicMode.BED,
        has_original_audio=False,
    )

    assert plan.mode is MusicMode.REPLACE
    # And at replacement level: the track is the whole soundtrack now, and a
    # bed's -14 dB of it would be close to inaudible.
    assert plan.gain_db == 0
    assert "[0:a]" not in build_audio_filter(plan)


@pytest.mark.unit
def test_a_recorded_start_is_used_exactly_rather_than_re_picked() -> None:
    """What lets a remake reproduce the soundtrack that was approved. The
    recorded start came off a plan that was already beat-snapped, so snapping it
    again — here, onto the 0.5s grid — would move it."""
    plan = plan_music(
        synthetic(120.0, duration=40.0),
        clip_duration_sec=8.0,
        mode=MusicMode.BED,
        start_sec=3.27,
    )

    assert plan.music_start_sec == 3.27


@pytest.mark.unit
def test_a_recorded_start_is_still_clamped_to_the_end_of_the_track() -> None:
    """A remake may lengthen the clip, and the excerpt then no longer fits where
    it was. Overrunning the track would end the music early."""
    plan = plan_music(
        synthetic(120.0, duration=30.0),
        clip_duration_sec=8.0,
        mode=MusicMode.BED,
        start_sec=100.0,
    )

    assert plan.music_start_sec == 22.0


@pytest.mark.unit
def test_a_short_track_is_marked_for_looping() -> None:
    short = plan_music(synthetic(120.0, duration=5.0), clip_duration_sec=20.0, mode=MusicMode.BED)
    long = plan_music(synthetic(120.0, duration=60.0), clip_duration_sec=20.0, mode=MusicMode.BED)

    assert short.loop is True
    assert long.loop is False


# ── The filtergraph ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_replace_never_references_the_clips_own_audio() -> None:
    """The distinction the whole mode rests on. Mixing the original in at zero
    volume would look identical in the UI and sound wrong."""
    graph = build_audio_filter(
        plan_music(synthetic(120.0), clip_duration_sec=8.0, mode=MusicMode.REPLACE)
    )

    assert "[0:a]" not in graph
    assert "amix" not in graph
    assert "loudnorm" in graph


@pytest.mark.unit
def test_a_bed_ducks_against_the_speech_rather_than_sitting_at_a_fixed_level() -> None:
    graph = build_audio_filter(
        plan_music(synthetic(120.0), clip_duration_sec=8.0, mode=MusicMode.BED)
    )

    assert "sidechaincompress" in graph
    assert "[0:a]" in graph
    # amix halves every input by default, which would undo the chosen level.
    assert "normalize=0" in graph


@pytest.mark.unit
def test_both_modes_end_at_the_same_output_label() -> None:
    """So the caller's `-map` does not have to know which mode ran."""
    for mode in (MusicMode.BED, MusicMode.REPLACE):
        graph = build_audio_filter(plan_music(synthetic(120.0), clip_duration_sec=8.0, mode=mode))
        assert graph.rstrip().endswith("[aout]")


@pytest.mark.unit
def test_the_video_is_copied_when_the_picture_has_not_changed() -> None:
    """Keeping the captions means the picture is already right; re-encoding it
    would cost minutes and lose a generation for nothing."""
    plan = plan_music(synthetic(120.0), clip_duration_sec=8.0, mode=MusicMode.BED)
    args = build_ffmpeg_args(
        plan, clip_path="c.mp4", music_path="m.m4a", output_path="o.mp4", reencode_video=False
    )

    assert "copy" in args
    assert "libx264" not in args


@pytest.mark.unit
def test_a_looping_track_is_bounded_by_the_clips_duration() -> None:
    """`-stream_loop -1` is infinite; without `-t` so is the output."""
    plan = plan_music(synthetic(120.0, duration=4.0), clip_duration_sec=20.0, mode=MusicMode.BED)
    args = build_ffmpeg_args(
        plan, clip_path="c.mp4", music_path="m.m4a", output_path="o.mp4", reencode_video=False
    )

    assert "-stream_loop" in args
    assert "-t" in args
    assert args[args.index("-t") + 1] == str(plan.duration_sec)


@pytest.mark.unit
def test_the_loop_flag_precedes_the_music_input_it_applies_to() -> None:
    """`-stream_loop` is an input option: after the input it belongs to, it
    silently does nothing, and the music would stop partway through the clip."""
    plan = plan_music(synthetic(120.0, duration=4.0), clip_duration_sec=20.0, mode=MusicMode.BED)
    args = build_ffmpeg_args(
        plan, clip_path="c.mp4", music_path="m.m4a", output_path="o.mp4", reencode_video=False
    )

    loop_at = args.index("-stream_loop")
    music_at = args.index("m.m4a")
    assert loop_at < music_at
    assert args[music_at - 1] == "-i"


@pytest.mark.unit
def test_the_output_is_cut_at_the_plans_duration_and_so_is_the_copied_picture() -> None:
    """`-t` is not only a bound on the looping music — it bounds the video
    stream too, `-c:v copy` and all. So the duration a plan is built from has to
    be the length of the file being mixed, never a window computed somewhere
    else. Measured: a 20.000 s picture with a 12 s plan came out at 12.067 s,
    the rest of the footage gone, and it looked like a successful mix."""
    plan = plan_music(synthetic(120.0, duration=60.0), clip_duration_sec=12.0, mode=MusicMode.BED)
    args = build_ffmpeg_args(
        plan, clip_path="c.mp4", music_path="m.m4a", output_path="o.mp4", reencode_video=False
    )

    assert args[args.index("-t") + 1] == str(plan.duration_sec) == "12.0"


# ── Bounds ───────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_a_decode_that_never_returns_is_cut_off_and_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Provoked rather than waited for: what is under test is that a bound is
    passed at all and that the operator gets a sentence instead of a traceback
    from inside subprocess."""
    seen: dict[str, Any] = {}

    def _wedge(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(argv, timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", _wedge)

    with pytest.raises(TimeoutError, match=r"timed out after 30\.0s"):
        decode_mono(tmp_path / "track.mp3", timeout_s=30.0)

    assert seen["timeout"] == 30.0


class WedgedMixStage:
    """A MUSIC stage that dies where a hung mix really dies: inside the seam.

    The two halves of that distinction — a mix that hung against a mix that
    refused — are asserted directly on `clipforge.media.scoring`, which is where
    the mix now runs for both MUSIC and REMAKE. What is left here is the thing
    only the scheduler can show: what a stage failing that way costs the job.
    """

    name = StageName.MUSIC
    lane = Lane.CPU

    def run(self, context: StageContext) -> StageOutcome:
        scoring._run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            what="mixing the music",
            timeout_s=0.5,
        )
        raise AssertionError("the mix was supposed to time out")


class CapturingJobStore:
    """The only method the runner touches on the way to recording a failure."""

    def __init__(self) -> None:
        self.job: Job | None = None

    def apply(self, transition: Transition) -> None:
        self.job = transition.job


@pytest.mark.unit
def test_a_mix_that_timed_out_is_retried_rather_than_spending_the_last_attempt() -> None:
    """Asserted from the far end, because the cost is paid there. The runner
    classifies a failure by the exception's type — `ScoringError` is a
    `RuntimeError`, which it reads as terminal — and `lease.fail_stage` then
    takes a terminal failure straight to FAILED with the remaining attempts
    unspent. So the one failure in this stage that another attempt would fix was
    the one failure that never got another attempt."""
    job = Job(
        id="job-1",
        uid="user-1",
        type=JobType.MUSIC,
        status=JobStatus.RUNNING,
        worker_id="worker-1",
        lease_expires_at=datetime(2026, 9, 14, 12, 0, tzinfo=UTC),
        stages=[Stage(name=StageName.MUSIC, lane=Lane.CPU, status=StageStatus.PENDING)],
        attempts=1,
        max_attempts=3,
        created_at=datetime(2026, 9, 14, 12, 0, tzinfo=UTC),
        updated_at=datetime(2026, 9, 14, 12, 0, tzinfo=UTC),
    )
    registry = StageRegistry()
    registry.register(WedgedMixStage())

    final = StageRunner(
        store=CapturingJobStore(),  # type: ignore[arg-type]
        registry=registry,
        settings=Settings(use_emulators=True),
        broker=ModelBroker(reserve_mb=0),
    ).run(job)

    assert final.status is JobStatus.QUEUED, "a wedged ffmpeg is not a terminal failure"
    assert final.attempts == 2, "one attempt spent of three, not all three"
    assert final.error is not None and final.error.retryable
