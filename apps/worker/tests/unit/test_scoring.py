"""The shared music seam: what MUSIC and REMAKE both go through.

Every test here uses a local file as the track, so nothing touches the network
and the whole file runs in a couple of seconds — which is also a claim about the
seam itself, since `resolve_audio_source` treats a path on this machine as a
first-class music source rather than a test affordance.

Three things are worth pinning, and none of them is "does ffmpeg work".

**A bed against a picture with no audio must not fail.** It used to: the BED
filtergraph names `[0:a]`, and ffmpeg answers a video-only input with *"Stream
specifier ':a' ... matches no streams"* and exit -22, having written nothing.

**An approved soundtrack must come back identical.** Given the start and the
tempo that were recorded, the track is not analysed again — not as an
optimisation, but because `pick_section` reads the energy envelope and would
answer differently the moment the clip's duration moved.

**A mix that hung is not a mix that refused.** `ScoringError` means the request
itself cannot work and the runner spends no further attempt on it; a wedged
ffmpeg is exactly the failure another attempt fixes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from clipforge.media import scoring
from clipforge.media.beats import BeatAnalysis
from clipforge.media.ffprobe import probe
from clipforge.media.scoring import ScoringError, apply_music
from clipforge_contracts import MusicMode

FIXTURES = Path(__file__).resolve().parents[2] / "assets" / "fixtures"
VIDEO_ONLY = FIXTURES / "video-only.mp4"


def picture(path: Path, *, seconds: float = 3.0) -> Path:
    """A short clip with its own audio, standing in for a rendered one."""
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=128x128:rate=24:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=300:duration={seconds}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
    )
    return path


def track(path: Path, *, seconds: float = 8.0) -> Path:
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-ar",
            "44100",
            str(path),
        ],
        check=True,
    )
    return path


def mix(tmp_path: Path, **overrides: Any) -> Any:
    """`apply_music` with everything a caller must decide already decided."""
    arguments: dict[str, Any] = {
        "picture": picture(tmp_path / "clip.mp4"),
        "destination": tmp_path / "scored.mp4",
        "duration_sec": 3.0,
        "source": str(track(tmp_path / "bed.wav")),
        "mode": MusicMode.BED,
        "gain_db": None,
        "align_to_beat": True,
        "forced_start_sec": None,
        "forced_tempo_bpm": None,
        "has_original_audio": True,
        "work_dir": tmp_path / "work",
        "ffmpeg": "ffmpeg",
        "ffprobe": "ffprobe",
    }
    arguments.update(overrides)
    return apply_music(**arguments)


# ── The mix ──────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_the_scored_file_keeps_the_pictures_length_and_gains_a_soundtrack(
    tmp_path: Path,
) -> None:
    result = mix(tmp_path)

    media = probe(result.path, ffprobe_bin="ffprobe")
    assert media.has_audio
    assert media.has_video
    # Within a frame of the picture it was given. `-t` is the only thing holding
    # the looping music back, and it bounds the copied video stream too.
    assert media.duration_sec == pytest.approx(3.0, abs=0.1)
    assert result.plan.mode is MusicMode.BED


@pytest.mark.unit
def test_the_track_title_is_whatever_the_source_called_it(tmp_path: Path) -> None:
    """A local file has no metadata to read, so the stem is the honest answer —
    and it is what the clip will end up displaying."""
    assert mix(tmp_path).track_title == "bed"


@pytest.mark.unit
@pytest.mark.skipif(not VIDEO_ONLY.is_file(), reason="needs the media fixtures")
def test_a_bed_over_a_silent_picture_is_downgraded_rather_than_refused(
    tmp_path: Path,
) -> None:
    """The live bug, end to end. Before the downgrade this invocation exited -22
    with *"Stream specifier ':a' ... matches no streams"* and produced no file,
    which on the remake path would have lost the picture correction as well as
    the music."""
    result = mix(
        tmp_path,
        picture=VIDEO_ONLY,
        duration_sec=2.0,
        mode=MusicMode.BED,
        has_original_audio=False,
    )

    assert result.plan.mode is MusicMode.REPLACE
    assert probe(result.path, ffprobe_bin="ffprobe").has_audio


@pytest.mark.unit
def test_a_picture_with_no_duration_is_refused_before_anything_is_fetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero is what an unprobed or unrecorded clip looks like, and planning
    against it would ask ffmpeg for a zero-length excerpt."""
    monkeypatch.setattr(scoring, "resolve_audio_source", _never_called)

    with pytest.raises(ScoringError, match="no duration"):
        mix(tmp_path, duration_sec=0.0)


# ── Reusing an approved soundtrack ───────────────────────────────────────────


@pytest.mark.unit
def test_a_recorded_start_and_tempo_are_reused_instead_of_analysed_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer approved a specific excerpt. Re-analysing would re-pick it,
    and `pick_section` relocates by about nine seconds of track for a ten-second
    change in clip length — so the soundtrack would not be the one they saw.

    `analyse` is made to explode rather than counted, because a spy that is
    never called and an assertion that is never reached look the same."""
    monkeypatch.setattr(scoring, "analyse", _never_called)

    result = mix(tmp_path, forced_start_sec=2.0, forced_tempo_bpm=128.0)

    assert result.plan.music_start_sec == 2.0
    assert result.plan.tempo_bpm == 128.0


@pytest.mark.unit
def test_one_recorded_number_on_its_own_is_not_enough_to_skip_the_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tempo with no start still needs a start picked, and a start with no
    tempo leaves the clip with nothing to report. Either way the track has to be
    read — and the start, when there is one, is still used verbatim."""
    calls: list[Path] = []

    def spy(path: Path, ffmpeg: str = "ffmpeg") -> BeatAnalysis:
        calls.append(path)
        return _flat_analysis()

    monkeypatch.setattr(scoring, "analyse", spy)

    result = mix(tmp_path, forced_start_sec=2.0, forced_tempo_bpm=None)

    assert len(calls) == 1
    assert result.plan.music_start_sec == 2.0


# ── Narrating the wait ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_the_waiting_is_narrated_through_the_callback(tmp_path: Path) -> None:
    """The fetch and the analysis are tens of seconds on a cold cache, and a
    stage that says nothing for tens of seconds looks exactly like one that has
    hung. The callback is what keeps that reporting out of the media layer."""
    said: list[str] = []

    mix(tmp_path, on_progress=said.append)

    assert said[0] == "Fetching the track"
    assert any("Analysing" in sentence for sentence in said)
    assert any("Mixing" in sentence for sentence in said)


@pytest.mark.unit
def test_the_analysis_is_not_announced_when_it_does_not_happen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Narrating a step that is being skipped is how a progress line stops
    meaning anything."""
    monkeypatch.setattr(scoring, "analyse", _never_called)
    said: list[str] = []

    mix(tmp_path, forced_start_sec=1.0, forced_tempo_bpm=100.0, on_progress=said.append)

    assert not any("Analysing" in sentence for sentence in said)


# ── Bounds ───────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_a_mix_that_hangs_is_cut_off_and_reported_as_retryable() -> None:
    """A real process that will not exit, killed by the real timeout. What the
    retry logic reads is the exception's type: TimeoutError is in the set
    StageRunner gives another attempt, and a RuntimeError is not."""
    with pytest.raises(TimeoutError, match="mixing the music timed out"):
        scoring._run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            what="mixing the music",
            timeout_s=0.5,
        )


@pytest.mark.unit
def test_a_mix_that_ran_and_refused_is_never_retryable() -> None:
    """ffmpeg that started, read the inputs and said no will say the same no
    twice more, and the operator watches a job that merely looks slow."""
    with pytest.raises(ScoringError, match="mixing the music failed"):
        scoring._run([sys.executable, "-c", "raise SystemExit(1)"], what="mixing the music")


def _never_called(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError(f"should not have been called: {args} {kwargs}")


def _flat_analysis() -> BeatAnalysis:
    """An analysis with nothing in it, for the cases that only count the call."""
    return BeatAnalysis(
        duration_sec=8.0,
        tempo_bpm=None,
        beat_times=(),
        onset_env=np.zeros(0, dtype=np.float32),
        hop_sec=256 / 22_050,
    )
