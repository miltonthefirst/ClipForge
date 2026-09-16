"""The REMAKE stage: what it decides, and what it refuses.

In-memory stores and a stub synthesiser, because what is under test is the
orchestration — which path a request takes, what gets recorded, and which
refusals happen before any work is done. Whether the filtergraphs it builds do
what they claim is the integration tier's business.

The refusals matter as much as the successes. A reviewer asking for a remake is
on a phone, and the difference between "the source was garbage-collected, so
reframing is impossible but a voice change would still work" and a stack trace
is the difference between a next step and a dead end.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from clipforge.config import Settings
from clipforge.media.ffprobe import probe
from clipforge.media.scoring import apply_music
from clipforge.media.speech import Utterance
from clipforge.models.broker import ModelBroker
from clipforge.stages import remake as remake_module
from clipforge.stages.base import StageContext
from clipforge.stages.remake import RemakeStage, RemakeStageError
from clipforge.store.blobs import BlobRef
from clipforge_contracts import (
    AppliedMusic,
    AppliedRemake,
    AppliedVoice,
    Candidate,
    Clip,
    ClipLocation,
    FitFill,
    Framing,
    FramingMode,
    Job,
    JobStatus,
    JobType,
    Lane,
    LlmRemakeNote,
    MusicCaptions,
    MusicMode,
    NoteAudio,
    NoteCrop,
    NoteFraming,
    NoteObscure,
    NoteTopic,
    ObscureOptions,
    PanKeyframe,
    RemakeOptions,
    ReviewState,
    Source,
    SpeechMode,
    Stage,
    StageName,
    StageStatus,
    SubScores,
    UnsupportedAsk,
    VoiceCaptions,
    VoiceOptions,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeClipStore:
    def __init__(self, clip: Clip | None) -> None:
        self._clip = clip
        self.saved: list[Clip] = []

    def get(self, clip_id: str) -> Clip | None:
        return self._clip if self._clip and self._clip.id == clip_id else None

    def save(self, clip: Clip, preview: object = None) -> None:
        self.saved.append(clip)


class FakeCandidateStore:
    def __init__(self, candidate: Candidate | None) -> None:
        self._candidate = candidate

    def get(self, candidate_id: str) -> Candidate | None:
        if self._candidate and self._candidate.id == candidate_id:
            return self._candidate
        return None


class FakeSourceStore:
    def __init__(self, source: Source | None) -> None:
        self._source = source

    def get(self, source_id: str) -> Source | None:
        return self._source if self._source and self._source.id == source_id else None


class FakeBlobStore:
    """Records what was uploaded, and keeps a copy of it.

    The copy is not incidental. The stage deletes its scratch directory in a
    `finally`, so by the time a test can look at the clip it saved, the file
    that clip describes is gone — and the assertion that matters for the music
    is on the file itself. `music` was the field that lied.
    """

    def __init__(self, keep_in: Path) -> None:
        self.puts: list[tuple[str, Path]] = []
        self.kept: list[Path] = []
        self._keep_in = keep_in

    def put(
        self, key: str, source: Path, *, content_type: str | None = None, required: bool = False
    ) -> BlobRef:
        self.puts.append((key, source))
        self._keep_in.mkdir(parents=True, exist_ok=True)
        copy = self._keep_in / f"{len(self.kept)}-{source.name}"
        shutil.copy2(source, copy)
        self.kept.append(copy)
        return BlobRef(key=key, local_path=source, size_bytes=source.stat().st_size)


class FakeArchive:
    def load(self, content_hash: str, model_version: str) -> None:
        return None


class StubSynth:
    """A synthesiser that produces real audio without a model.

    Real audio rather than a mock, because the mix downstream is real ffmpeg
    and a zero-byte wav would fail there for reasons unrelated to what is being
    tested.
    """

    engine = "stub-tts"

    def __init__(self, seconds: float = 4.0) -> None:
        self.seconds = seconds
        self.spoken: list[str] = []

    def voices(self) -> tuple[str, ...]:
        return ("af_heart",)

    def speak(
        self, text: str, *, voice: str, language: str, speed: float, destination: Path
    ) -> Utterance:
        self.spoken.append(text)
        destination.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=330:duration={self.seconds}",
                "-ar",
                "24000",
                str(destination),
            ],
            check=True,
            capture_output=True,
        )
        return Utterance(
            path=destination,
            duration_sec=self.seconds,
            sample_rate=24000,
            text=text,
            voice=voice,
            language=language,
            engine=self.engine,
        )


class FakeWorkspace:
    def __init__(self, root: Path) -> None:
        self.tmp_dir = root / "tmp"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def make_video(
    path: Path, *, seconds: int = 8, audio: bool = True, size: str = "1920x1080"
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={size}:rate=30:duration={seconds}",
    ]
    if audio:
        argv += ["-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}", "-c:a", "aac"]
    argv += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(argv, check=True, capture_output=True)  # noqa: S603 - fixed argv
    return path


def clip(tmp_path: Path, **overrides: object) -> Clip:
    defaults = {
        "id": "clip-1",
        "uid": "user-1",
        "candidate_id": "cand-1",
        "source_id": "src-1",
        "job_id": "job-0",
        "location": ClipLocation.LOCAL,
        "local_path": str(tmp_path / "clips" / "clip-1.mp4"),
        "duration_sec": 8.0,
        "width_px": 1080,
        "height_px": 1920,
        "render_profile": "default:v1",
        "title": "a goal",
        "review": ReviewState.APPROVED,
        "created_at": NOW,
    }
    return Clip(**{**defaults, **overrides})


def make_track(path: Path, *, seconds: float = 30.0) -> Path:
    """A track on this machine, which `resolve_audio_source` takes as a source.

    A local file rather than a URL, so nothing in this file touches the network
    — and that is a claim about the seam too, not a test affordance: a path is a
    first-class music source.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-ar",
            "44100",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def soundtrack(source: Path | str, **overrides: object) -> AppliedMusic:
    """A soundtrack record as MUSIC writes one, with every input it recorded."""
    defaults: dict[str, object] = {
        "mode": MusicMode.REPLACE,
        "captions": MusicCaptions.KEEP,
        "source": str(source),
        "track_title": "bed",
        "tempo_bpm": 120.0,
        "music_start_sec": 2.0,
        "gain_db": -6.0,
        "align_to_beat": True,
    }
    return AppliedMusic(**{**defaults, **overrides})


def mixes(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every call the stage makes to the music seam, in order.

    The field is what lied, so "the music was re-applied" cannot be asserted
    from the record alone — and its opposite, "nothing re-applied it", leaves no
    trace on the record at all.
    """
    calls: list[dict[str, Any]] = []

    def recording(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return apply_music(**kwargs)

    monkeypatch.setattr(remake_module, "apply_music", recording)
    return calls


def candidate() -> Candidate:
    return Candidate(
        id="cand-1",
        uid="user-1",
        source_id="src-1",
        start_sec=100.0,
        end_sec=108.0,
        sub_scores=SubScores(
            hook=5, curiosity=5, standalone=5, emotion=5, pacing=5, shareability=5
        ),
        total=60,
        created_at=NOW,
    )


def source(path: Path | None) -> Source:
    return Source(
        id="src-1",
        uid="user-1",
        provider="youtube",
        external_id="abc",
        url="https://example.invalid/abc",
        local_path=str(path) if path else None,
        content_hash="deadbeef",
        created_at=NOW,
    )


def job(options: RemakeOptions, clip_id: str | None = "clip-1") -> Job:
    return Job(
        id="job-1",
        uid="user-1",
        type=JobType.REMAKE,
        status=JobStatus.RUNNING,
        clip_id=clip_id,
        remake_options=options,
        stages=[Stage(name=StageName.REMAKE, lane=Lane.CPU, status=StageStatus.RUNNING)],
        attempts=1,
        max_attempts=1,
        created_at=NOW,
        updated_at=NOW,
    )


def context(the_job: Job) -> StageContext:
    settings = Settings(_env_file=None, video_encoder="libx264")
    return StageContext(
        job=the_job,
        stage_name=StageName.REMAKE,
        checkpoint=None,
        settings=settings,
        broker=ModelBroker(reserve_mb=settings.vram_reserve_mb),
    )


def build(
    tmp_path: Path,
    *,
    the_clip: Clip | None = None,
    the_source: Source | None = None,
    the_candidate: Candidate | None = None,
    synth: object | None = None,
) -> tuple[RemakeStage, FakeClipStore, FakeBlobStore]:
    clips = FakeClipStore(the_clip)
    blobs = FakeBlobStore(tmp_path / "uploaded")
    stage = RemakeStage(
        settings=Settings(_env_file=None, video_encoder="libx264"),
        clips=clips,  # type: ignore[arg-type]
        candidates=FakeCandidateStore(the_candidate),  # type: ignore[arg-type]
        sources=FakeSourceStore(the_source),  # type: ignore[arg-type]
        archive=FakeArchive(),  # type: ignore[arg-type]
        workspace=FakeWorkspace(tmp_path),  # type: ignore[arg-type]
        blobs=blobs,  # type: ignore[arg-type]
        speech=synth,  # type: ignore[arg-type]
        # No note interpretation in these tests: the model is the gpu tier's.
        client_factory=lambda: None,  # type: ignore[arg-type, return-value]
    )
    return stage, clips, blobs


pytestmark = pytest.mark.unit


# ── Refusals, before any work ────────────────────────────────────────────────


def test_a_job_without_a_clip_is_refused(tmp_path: Path) -> None:
    stage, _, _ = build(tmp_path)
    with pytest.raises(RemakeStageError, match="needs a clipId"):
        stage.run(context(job(RemakeOptions(), clip_id=None)))


def test_a_job_for_a_clip_that_does_not_exist_says_so(tmp_path: Path) -> None:
    stage, _, _ = build(tmp_path)
    with pytest.raises(RemakeStageError, match="no such clip"):
        stage.run(context(job(RemakeOptions())))


def test_reframing_without_the_source_explains_the_alternative(tmp_path: Path) -> None:
    """The most likely failure in practice, and the message is the feature.

    Sources are garbage-collected; clips outlive them. A reviewer who cannot
    reframe can still re-voice, and the refusal has to say so or they are stuck.
    """
    stage, _, _ = build(
        tmp_path,
        the_clip=clip(tmp_path),
        the_source=source(None),
        the_candidate=candidate(),
    )
    options = RemakeOptions(framing=Framing(mode=FramingMode.FIT))
    with pytest.raises(RemakeStageError, match="no longer on this machine") as caught:
        stage.run(context(job(options)))
    assert "remake only the voice" in str(caught.value)


def test_a_remake_without_the_candidate_cannot_re_cut(tmp_path: Path) -> None:
    """The candidate is the only record of where in the source the clip came from."""
    stage, _, _ = build(tmp_path, the_clip=clip(tmp_path), the_source=source(None))
    with pytest.raises(RemakeStageError, match="candidate"):
        stage.run(context(job(RemakeOptions(framing=Framing(mode=FramingMode.FIT)))))


def test_nudges_that_cross_over_are_refused_with_the_arithmetic(tmp_path: Path) -> None:
    stage, _, _ = build(
        tmp_path, the_clip=clip(tmp_path), the_source=source(None), the_candidate=candidate()
    )
    options = RemakeOptions(start_delta_sec=5, end_delta_sec=-5)
    with pytest.raises(RemakeStageError, match="past each other"):
        stage.run(context(job(options)))


def test_asking_for_a_voice_with_no_synthesiser_names_the_install(tmp_path: Path) -> None:
    stage, _, _ = build(
        tmp_path,
        the_clip=clip(tmp_path),
        the_source=source(None),
        the_candidate=candidate(),
        synth=None,
    )
    options = RemakeOptions(
        voice=VoiceOptions(
            mode=SpeechMode.REPLACE,
            voice="af_heart",
            language="es",
            translate=False,
            script="hola",
            captions=VoiceCaptions.REMOVE,
        )
    )
    with pytest.raises(RemakeStageError, match="--extra speech"):
        stage.run(context(job(options)))


def test_a_voice_with_nothing_to_say_is_refused(tmp_path: Path) -> None:
    """No transcript and no script means the synthesiser has no input."""
    stage, _, _ = build(
        tmp_path,
        the_clip=clip(tmp_path),
        the_source=source(None),
        the_candidate=candidate(),
        synth=StubSynth(),
    )
    options = RemakeOptions(
        voice=VoiceOptions(
            mode=SpeechMode.REPLACE,
            voice="af_heart",
            language="en",
            translate=False,
            captions=VoiceCaptions.REMOVE,
        )
    )
    with pytest.raises(RemakeStageError, match="nothing to say"):
        stage.run(context(job(options)))


# ── The reframe path ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("framing", "expected"),
    [
        (Framing(mode=FramingMode.FIT, fill=FitFill.SOLID), "whole frame fitted"),
        (
            Framing(mode=FramingMode.PAN, keyframes=[PanKeyframe(at_sec=0, x_pct=20)]),
            "panned across 1 points",
        ),
    ],
)
def test_a_reframe_produces_a_new_clip(tmp_path: Path, framing: Framing, expected: str) -> None:
    media = make_video(tmp_path / "src" / "source.mp4", seconds=120)
    stage, clips, blobs = build(
        tmp_path,
        the_clip=clip(tmp_path),
        the_source=source(media),
        the_candidate=candidate(),
    )
    outcome = stage.run(context(job(RemakeOptions(framing=framing))))

    assert expected in (outcome.detail or "")
    assert len(clips.saved) == 1
    saved = clips.saved[0]
    assert saved.id != "clip-1"
    assert saved.derived_from_clip_id == "clip-1"
    assert (saved.width_px, saved.height_px) == (1080, 1920)
    assert len(blobs.puts) == 1


def test_the_original_is_never_touched(tmp_path: Path) -> None:
    """A remake is a second opinion, not an erratum.

    The reviewer may prefer the original once they see the alternative, and
    that only stays possible if both exist.
    """
    media = make_video(tmp_path / "src" / "source.mp4", seconds=120)
    original = clip(tmp_path)
    stage, clips, _ = build(
        tmp_path, the_clip=original, the_source=source(media), the_candidate=candidate()
    )
    stage.run(context(job(RemakeOptions(framing=Framing(mode=FramingMode.FIT)))))

    assert [c.id for c in clips.saved] == [clips.saved[0].id]
    assert original.id not in [c.id for c in clips.saved]


def test_a_remade_clip_always_needs_reviewing_again(tmp_path: Path) -> None:
    """The approval belonged to the clip it was given to, not to its successor."""
    media = make_video(tmp_path / "src" / "source.mp4", seconds=120)
    stage, clips, _ = build(
        tmp_path,
        the_clip=clip(tmp_path, review=ReviewState.APPROVED),
        the_source=source(media),
        the_candidate=candidate(),
    )
    stage.run(context(job(RemakeOptions(framing=Framing(mode=FramingMode.FIT)))))
    assert clips.saved[0].review is ReviewState.PENDING


def test_the_window_actually_cut_is_recorded_after_the_nudge(tmp_path: Path) -> None:
    media = make_video(tmp_path / "src" / "source.mp4", seconds=120)
    stage, clips, _ = build(
        tmp_path, the_clip=clip(tmp_path), the_source=source(media), the_candidate=candidate()
    )
    stage.run(
        context(job(RemakeOptions(framing=Framing(mode=FramingMode.FIT), start_delta_sec=-2.0)))
    )
    remake = clips.saved[0].remake
    assert remake is not None
    assert remake.start_sec == pytest.approx(98.0)
    assert remake.end_sec == pytest.approx(108.0)


def test_the_notes_are_recorded_even_when_nothing_reads_them(tmp_path: Path) -> None:
    """So a remake that came out wrong can be compared against what was asked."""
    media = make_video(tmp_path / "src" / "source.mp4", seconds=120)
    stage, clips, _ = build(
        tmp_path, the_clip=clip(tmp_path), the_source=source(media), the_candidate=candidate()
    )
    stage.run(
        context(
            job(
                RemakeOptions(
                    notes="it keeps losing the ball",
                    framing=Framing(mode=FramingMode.FIT),
                    interpret_notes=False,
                )
            )
        )
    )
    assert clips.saved[0].remake.notes == "it keeps losing the ball"  # type: ignore[union-attr]


def test_a_tracked_remake_records_the_path_it_followed(tmp_path: Path) -> None:
    """The only way to see what the tracker decided, and to correct it by hand."""
    media = make_video(tmp_path / "src" / "source.mp4", seconds=120)
    stage, clips, _ = build(
        tmp_path, the_clip=clip(tmp_path), the_source=source(media), the_candidate=candidate()
    )
    stage.run(context(job(RemakeOptions(framing=Framing(mode=FramingMode.TRACK)))))

    remake = clips.saved[0].remake
    assert remake is not None
    assert remake.framing_mode is FramingMode.TRACK
    assert remake.keyframes, "a tracked remake without keyframes recorded nothing"
    assert all(0 <= k.x_pct <= 100 for k in remake.keyframes)


def test_provenance_carries_over_because_it_is_the_same_footage(tmp_path: Path) -> None:
    """A correction is a new cut of the same material, not a new piece of it.

    Everything that describes where the footage came from has to survive, or
    the corrected clip looks like an orphan: the source it was cut from and the
    candidate that selected it. The soundtrack is a harder question and has a
    section of its own.
    """
    media = make_video(tmp_path / "src" / "source.mp4", seconds=120)
    original = clip(tmp_path)
    stage, clips, _ = build(
        tmp_path, the_clip=original, the_source=source(media), the_candidate=candidate()
    )

    stage.run(context(job(RemakeOptions(framing=Framing(mode=FramingMode.FIT)))))

    remade = clips.saved[0]
    assert remade.source_id == original.source_id
    assert remade.candidate_id == original.candidate_id
    assert remade.derived_from_clip_id == original.id


# ── The voice path ───────────────────────────────────────────────────────────


def test_a_voice_only_remake_does_not_need_the_source(tmp_path: Path) -> None:
    """The half of the feature that still works after the collector has run."""
    existing = make_video(tmp_path / "clips" / "clip-1.mp4", seconds=8)
    synth = StubSynth(seconds=4.0)
    stage, clips, _ = build(
        tmp_path,
        the_clip=clip(tmp_path, local_path=str(existing)),
        the_source=source(None),
        the_candidate=candidate(),
        synth=synth,
    )
    options = RemakeOptions(
        voice=VoiceOptions(
            mode=SpeechMode.REPLACE,
            voice="af_heart",
            language="es",
            translate=False,
            script="un golazo desde treinta metros",
            captions=VoiceCaptions.KEEP,
        )
    )
    outcome = stage.run(context(job(options)))

    assert "re-voiced" in (outcome.detail or "")
    assert synth.spoken == ["un golazo desde treinta metros"]
    voice = clips.saved[0].remake.voice  # type: ignore[union-attr]
    assert voice is not None
    assert voice.language == "es"
    assert voice.spoken_text == "un golazo desde treinta metros"
    assert voice.engine == "stub-tts"


def test_a_written_script_overrides_the_clip_s_own_words(tmp_path: Path) -> None:
    existing = make_video(tmp_path / "clips" / "clip-1.mp4", seconds=8)
    synth = StubSynth()
    stage, _, _ = build(
        tmp_path,
        the_clip=clip(tmp_path, local_path=str(existing)),
        the_source=source(None),
        the_candidate=candidate(),
        synth=synth,
    )
    options = RemakeOptions(
        voice=VoiceOptions(
            mode=SpeechMode.REPLACE,
            voice="af_heart",
            language="en",
            translate=False,
            script="the reviewer wrote this",
            captions=VoiceCaptions.KEEP,
        )
    )
    stage.run(context(job(options)))
    assert synth.spoken == ["the reviewer wrote this"]


def test_a_narration_that_overruns_still_produces_a_clip_of_the_right_length(
    tmp_path: Path,
) -> None:
    """Reported, not absorbed. The picture is never retimed to fit the audio."""
    existing = make_video(tmp_path / "clips" / "clip-1.mp4", seconds=8)
    stage, clips, _ = build(
        tmp_path,
        the_clip=clip(tmp_path, local_path=str(existing)),
        the_source=source(None),
        the_candidate=candidate(),
        synth=StubSynth(seconds=20.0),
    )
    options = RemakeOptions(
        voice=VoiceOptions(
            mode=SpeechMode.REPLACE,
            voice="af_heart",
            language="en",
            translate=False,
            script="a very long script indeed",
            captions=VoiceCaptions.KEEP,
        )
    )
    stage.run(context(job(options)))

    saved = clips.saved[0]
    assert saved.duration_sec == pytest.approx(8.0, abs=0.3)
    assert saved.remake.voice.speech_duration_sec == pytest.approx(20.0)  # type: ignore[union-attr]


# ── Feedback that is understood but cannot be met ────────────────────────────


def test_a_script_that_is_really_feedback_is_refused(tmp_path: Path) -> None:
    """A synthesiser read a reviewer's own instruction aloud on the live project.

    The notes box and the script box look alike and only one is obviously "for
    the machine", so this is a reasonable mistake to make. Refusing it with an
    explanation costs a sentence; accepting it cost a clip.
    """
    existing = make_video(tmp_path / "clips" / "clip-1.mp4", seconds=8)
    stage, _, _ = build(
        tmp_path,
        the_clip=clip(tmp_path, local_path=str(existing)),
        the_source=source(None),
        the_candidate=candidate(),
        synth=StubSynth(),
    )
    options = RemakeOptions(
        voice=VoiceOptions(
            mode=SpeechMode.REPLACE,
            voice="af_heart",
            language="en",
            translate=False,
            script="Cut the Canal plus caption or watermark in the upper right corner.",
            captions=VoiceCaptions.KEEP,
        )
    )
    with pytest.raises(RemakeStageError, match="notes box"):
        stage.run(context(job(options)))


def test_a_remake_of_a_revoiced_clip_speaks_from_what_that_clip_says(tmp_path: Path) -> None:
    """Not from the footage its ancestor was cut out of.

    A remake asking for English, applied to a clip that had already been
    re-voiced into Spanish, went back to the original French source transcript.
    The lineage reset on every generation, so "translate this clip" translated
    something the reviewer had already replaced.
    """
    existing = make_video(tmp_path / "clips" / "clip-1.mp4", seconds=8)
    already_voiced = clip(
        tmp_path,
        local_path=str(existing),
        derived_from_clip_id="clip-0",
        remake=AppliedRemake(
            framing_mode=FramingMode.AS_RENDERED,
            start_sec=100.0,
            end_sec=108.0,
            voice=AppliedVoice(
                mode=SpeechMode.REPLACE,
                voice="ef_dora",
                language="es",
                engine="stub-tts",
                translated=True,
                spoken_text="Que golazo desde treinta metros.",
            ),
        ),
    )
    synth = StubSynth()
    stage, _, _ = build(
        tmp_path,
        the_clip=already_voiced,
        the_source=source(None),
        the_candidate=candidate(),
        synth=synth,
    )
    options = RemakeOptions(
        voice=VoiceOptions(
            mode=SpeechMode.REPLACE,
            voice="bf_emma",
            language="en-gb",
            translate=False,
            captions=VoiceCaptions.KEEP,
        )
    )
    stage.run(context(job(options)))

    assert synth.spoken == ["Que golazo desde treinta metros."], (
        "it must start from what this clip actually says"
    )


def test_refusals_do_not_leak_from_one_remake_to_the_next(tmp_path: Path) -> None:
    """The stage is built once per worker and reused for every job.

    Refusals and warnings accumulate on the instance while a job runs, so
    without a reset the second clip of the day would carry the first clip's
    complaints — which is worse than saying nothing, because it is wrong and
    looks authoritative.
    """
    media = make_video(tmp_path / "src" / "source.mp4", seconds=120)
    stage, clips, _ = build(
        tmp_path, the_clip=clip(tmp_path), the_source=source(media), the_candidate=candidate()
    )
    # Left over from a previous job on the same stage instance.
    stage._refusals.append("removing a watermark is not something ClipForge can do")
    stage._notes.append("the recogniser was unsure of these words")

    stage.run(context(job(RemakeOptions(framing=Framing(mode=FramingMode.FIT)))))

    remade = clips.saved[0].remake
    assert remade is not None
    assert remade.refusals == [], "a previous job's refusal must not reach this clip"
    assert remade.warnings == []


def test_a_refusal_from_the_note_reaches_the_clip(tmp_path: Path) -> None:
    """The field exists so a reviewer can tell "refused" from "broken".

    It was doing the opposite. The per-run reset sat BELOW the call that reads
    the note, so every refusal and every conflict was collected and then thrown
    away one line later, and the clips in the live project show it: a remake
    whose note asked for a watermark to be removed reached the reviewer with
    `refusals: null`.
    """

    class Reading:
        """Just enough of OllamaClient for `interpret_note`."""

        model = "fake"

        def is_available(self) -> bool:
            return True

        def generate_structured(self, **_: object) -> LlmRemakeNote:
            return LlmRemakeNote(
                topics=[NoteTopic.FRAMING],
                framing_mode=NoteFraming.FIT,
                crop=NoteCrop.NOT_MENTIONED,
                language="NONE",
                audio=NoteAudio.NOT_MENTIONED,
                start_delta_sec=0,
                end_delta_sec=0,
                obscure=NoteObscure.NOT_MENTIONED,
                unsupported=[UnsupportedAsk.SLOW_MOTION],
                summary="fit the whole frame",
            )

    media = make_video(tmp_path / "src" / "source.mp4", seconds=120)
    stage, clips, _ = build(
        tmp_path, the_clip=clip(tmp_path), the_source=source(media), the_candidate=candidate()
    )
    stage._client_factory = Reading  # type: ignore[assignment]

    stage.run(context(job(RemakeOptions(notes="fit it, and slow it down a bit"))))

    remade = clips.saved[0].remake
    assert remade is not None
    assert [str(refusal.root) for refusal in remade.refusals or []] == [
        "changing the speed of the footage is not supported"
    ]


def test_a_corner_hint_does_not_leak_from_one_remake_to_the_next(tmp_path: Path) -> None:
    """A note naming a corner narrows the next job's search, if it survives."""
    media = make_video(tmp_path / "src" / "source.mp4", seconds=120)
    stage, _, _ = build(
        tmp_path, the_clip=clip(tmp_path), the_source=source(media), the_candidate=candidate()
    )
    stage._obscure_where = "TOP_LEFT"

    stage.run(context(job(RemakeOptions(framing=Framing(mode=FramingMode.FIT)))))

    assert stage._obscure_where is None


# ── Leaving the framing alone means leaving it alone ─────────────────────────


def fitted(tmp_path: Path) -> Clip:
    """A clip that was already reframed, the way a second correction finds it."""
    return clip(
        tmp_path,
        remake=AppliedRemake(framing_mode=FramingMode.FIT, start_sec=100.0, end_sec=108.0),
    )


def test_a_correction_about_something_else_keeps_the_framing_it_found(tmp_path: Path) -> None:
    """`framing=None` used to reach the renderer as the profile's centre crop.

    Correct for a clip RENDER made and wrong for one already reframed, and
    nearly unreachable until hiding existed — a re-cut only happened when
    someone asked for a different framing. Now "blur the logo" re-cuts too, and
    without this a reviewer asking about a watermark gets back a FIT clip
    silently cropped to its middle third. Which is what happened, on the first
    real note.
    """
    media = make_video(tmp_path / "src" / "source.mp4", seconds=120)
    stage, clips, _ = build(
        tmp_path, the_clip=fitted(tmp_path), the_source=source(media), the_candidate=candidate()
    )

    stage.run(context(job(RemakeOptions(start_delta_sec=-1.0))))

    remade = clips.saved[0].remake
    assert remade is not None
    assert remade.framing_mode is FramingMode.FIT


def test_a_stated_framing_still_wins_over_the_one_it_found(tmp_path: Path) -> None:
    media = make_video(tmp_path / "src" / "source.mp4", seconds=120)
    stage, clips, _ = build(
        tmp_path, the_clip=fitted(tmp_path), the_source=source(media), the_candidate=candidate()
    )

    stage.run(context(job(RemakeOptions(framing=Framing(mode=FramingMode.TRACK)))))

    remade = clips.saved[0].remake
    assert remade is not None
    assert remade.framing_mode is FramingMode.TRACK


def test_a_clip_render_made_still_gets_the_profiles_own_crop(tmp_path: Path) -> None:
    """There is nothing to inherit, and AS_RENDERED is what it actually has."""
    media = make_video(tmp_path / "src" / "source.mp4", seconds=120)
    stage, clips, _ = build(
        tmp_path, the_clip=clip(tmp_path), the_source=source(media), the_candidate=candidate()
    )

    stage.run(context(job(RemakeOptions(end_delta_sec=1.0))))

    remade = clips.saved[0].remake
    assert remade is not None
    assert remade.framing_mode is FramingMode.AS_RENDERED


# ── A re-cut must not silently un-voice the clip ─────────────────────────────


def voiced(tmp_path: Path, *, local_path: str | None = None, **voice_overrides: object) -> Clip:
    """A clip that has already been re-voiced, as a second correction finds it."""
    defaults: dict[str, object] = {
        "mode": SpeechMode.REPLACE,
        "voice": "af_heart",
        "language": "en-us",
        "engine": "kokoro-v1.0-onnx",
        "translated": True,
        "spoken_text": "Pavlovic finds the pass and Bayern are in behind.",
    }
    clip_overrides: dict[str, object] = {"local_path": local_path} if local_path else {}
    return clip(
        tmp_path,
        remake=AppliedRemake(
            framing_mode=FramingMode.FIT,
            start_sec=100.0,
            end_sec=108.0,
            voice=AppliedVoice(**{**defaults, **voice_overrides}),
        ),
        **clip_overrides,
    )


def test_hiding_a_logo_keeps_the_narration_the_clip_already_had(tmp_path: Path) -> None:
    """The bug the reviewer hit: an English clip came back speaking French.

    Re-voicing leaves the picture alone and re-cutting left the voice alone,
    which was fine while a re-cut only happened when someone asked for
    different framing. Hiding a logo re-cuts too, so a correction about a
    watermark rebuilt the soundtrack from the source and put the original
    commentary back without saying anything.
    """
    media = make_video(tmp_path / "src" / "source.mp4", seconds=120)
    stage, clips, _ = build(
        tmp_path,
        the_clip=voiced(tmp_path),
        the_source=source(media),
        the_candidate=candidate(),
        synth=StubSynth(),
    )

    stage.run(context(job(RemakeOptions(obscure=ObscureOptions(auto=True)))))

    remade = clips.saved[0].remake
    assert remade is not None
    assert remade.voice is not None
    assert remade.voice.language == "en-us"
    assert remade.voice.voice == "af_heart"


def test_the_inherited_narration_is_reproduced_not_re_derived(tmp_path: Path) -> None:
    """The exact words a synthesiser was handed last time, spoken again.

    Re-deriving them would go back to the transcript and the translator, which
    is a second opinion from a model that might read the sentence differently
    today — and is how the words were lost in the first place.
    """
    media = make_video(tmp_path / "src" / "source.mp4", seconds=120)
    stage, clips, _ = build(
        tmp_path,
        the_clip=voiced(tmp_path),
        the_source=source(media),
        the_candidate=candidate(),
        synth=StubSynth(),
    )

    stage.run(context(job(RemakeOptions(start_delta_sec=-1.0))))

    remade = clips.saved[0].remake
    assert remade is not None
    assert remade.voice is not None
    assert remade.voice.spoken_text == "Pavlovic finds the pass and Bayern are in behind."


def test_reusing_the_words_over_a_moved_cut_is_said_out_loud(tmp_path: Path) -> None:
    """Right when the window is the same, a guess when it is not."""
    media = make_video(tmp_path / "src" / "source.mp4", seconds=120)
    stage, clips, _ = build(
        tmp_path,
        the_clip=voiced(tmp_path),
        the_source=source(media),
        the_candidate=candidate(),
        synth=StubSynth(),
    )

    stage.run(context(job(RemakeOptions(end_delta_sec=2.0))))

    remade = clips.saved[0].remake
    assert remade is not None
    assert any("no longer line up" in str(w.root) for w in remade.warnings or [])


def test_a_stated_voice_still_wins_over_the_inherited_one(tmp_path: Path) -> None:
    media = make_video(tmp_path / "src" / "source.mp4", seconds=120)
    stage, clips, _ = build(
        tmp_path,
        the_clip=voiced(tmp_path),
        the_source=source(media),
        the_candidate=candidate(),
        synth=StubSynth(),
    )

    stage.run(
        context(
            job(
                RemakeOptions(
                    obscure=ObscureOptions(auto=True),
                    voice=VoiceOptions(
                        mode=SpeechMode.REPLACE,
                        voice="ef_dora",
                        language="es",
                        translate=False,
                        script="Pavlovic encuentra el pase.",
                        captions=VoiceCaptions.REBUILD,
                    ),
                )
            )
        )
    )

    remade = clips.saved[0].remake
    assert remade is not None
    assert remade.voice is not None
    assert remade.voice.language == "es"


def test_a_voice_only_remake_still_copies_the_picture(tmp_path: Path) -> None:
    """Inheriting must not become a reason to re-encode.

    The gate is the things that force a re-cut regardless of the voice. Put the
    voice in that test and every correction becomes a full render.
    """
    existing = make_video(tmp_path / "clips" / "clip-1.mp4", seconds=8)
    stage, clips, _ = build(
        tmp_path,
        the_clip=voiced(tmp_path, local_path=str(existing)),
        # No source on this machine: a re-cut would fail outright, so getting a
        # clip back at all is the assertion.
        the_source=source(None),
        the_candidate=candidate(),
        synth=StubSynth(),
    )

    stage.run(
        context(
            job(
                RemakeOptions(
                    voice=VoiceOptions(
                        mode=SpeechMode.REPLACE,
                        voice="af_heart",
                        language="en-us",
                        translate=False,
                        script="A different line entirely.",
                        captions=VoiceCaptions.KEEP,
                    )
                )
            )
        )
    )

    assert clips.saved, "a voice-only remake must not need the source"


def test_a_clip_that_was_never_re_voiced_inherits_nothing(tmp_path: Path) -> None:
    media = make_video(tmp_path / "src" / "source.mp4", seconds=120)
    stage, clips, _ = build(
        tmp_path, the_clip=clip(tmp_path), the_source=source(media), the_candidate=candidate()
    )

    stage.run(context(job(RemakeOptions(start_delta_sec=-1.0))))

    remade = clips.saved[0].remake
    assert remade is not None
    assert remade.voice is None


def test_asking_for_the_language_it_already_speaks_is_not_a_failure(tmp_path: Path) -> None:
    """It used to fail, blaming the transcript.

    Translating English into English returns the same text, which trips the
    no-op gate — so a remake whose narration was correct all along was refused
    with "the transcript is too rough to translate".
    """
    media = make_video(tmp_path / "src" / "source.mp4", seconds=120)
    stage, clips, _ = build(
        tmp_path,
        the_clip=voiced(tmp_path),
        the_source=source(media),
        the_candidate=candidate(),
        synth=StubSynth(),
    )

    stage.run(
        context(
            job(
                RemakeOptions(
                    voice=VoiceOptions(
                        mode=SpeechMode.REPLACE,
                        voice="af_heart",
                        language="en-gb",
                        translate=True,
                        captions=VoiceCaptions.REBUILD,
                    )
                )
            )
        )
    )

    remade = clips.saved[0].remake
    assert remade is not None
    assert remade.voice is not None
    assert any("already speaks" in str(w.root) for w in remade.warnings or [])


# ── A remake must not lose the soundtrack ────────────────────────────────────
#
# Music is mixed into the rendered audio and nothing keeps a separate track, so
# a remake that rebuilds the audio destroys it — and the clip was still saved
# with `music=original.music`, claiming a soundtrack the file did not contain.
# Which is the reviewer's own report: "adding music is overriding the current
# remade music". Every test here uses a local file as the track, so nothing
# reaches the network.


def scored(
    tmp_path: Path, track: Path, *, local_path: str | None = None, **overrides: object
) -> Clip:
    """A clip that has already had music added, as a correction finds it."""
    clip_overrides: dict[str, object] = {"local_path": local_path} if local_path else {}
    return clip(tmp_path, music=soundtrack(track), **clip_overrides, **overrides)


def speaks(mode: SpeechMode) -> VoiceOptions:
    """A voice change that does not touch the picture: the cheap path."""
    return VoiceOptions(
        mode=mode,
        voice="af_heart",
        language="en",
        translate=False,
        script="Bayern are in behind.",
        captions=VoiceCaptions.KEEP,
    )


def test_a_remake_that_touches_neither_picture_nor_audio_leaves_the_music_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one branch where the track is provably still in the file.

    The picture is the reviewed file handed straight on and nothing rebuilt its
    audio, so the music is exactly where it was. Mixing it again here would put
    two copies of the track on the clip.
    """
    existing = make_video(tmp_path / "clips" / "clip-1.mp4", seconds=8)
    track = make_track(tmp_path / "music" / "bed.wav")
    original = scored(tmp_path, track, local_path=str(existing))
    calls = mixes(monkeypatch)
    stage, clips, _ = build(
        tmp_path, the_clip=original, the_source=source(None), the_candidate=candidate()
    )

    stage.run(context(job(RemakeOptions(notes="nothing to change", interpret_notes=False))))

    assert clips.saved[0].music == original.music
    assert calls == [], "the track was already in the file; mixing it again doubles it"


def test_a_reframe_puts_the_soundtrack_back_onto_the_new_picture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug, in the direction the reviewer hit it.

    A reframe re-cuts from the source, and the source's audio is the original
    commentary — the music is not in it and never was. Asserted on the file as
    well as on the record, because the record is the half that lied.
    """
    media = make_video(tmp_path / "src" / "source.mp4", seconds=120)
    track = make_track(tmp_path / "music" / "bed.wav")
    calls = mixes(monkeypatch)
    stage, clips, blobs = build(
        tmp_path,
        the_clip=scored(tmp_path, track),
        the_source=source(media),
        the_candidate=candidate(),
    )

    stage.run(context(job(RemakeOptions(framing=Framing(mode=FramingMode.FIT)))))

    remade = clips.saved[0]
    assert remade.music is not None
    assert remade.music.source == str(track)
    assert remade.music.music_start_sec is not None
    # The level and the alignment the reviewer approved, fed back in rather than
    # left to the stage's defaults — which is why they are recorded at all.
    assert calls[0]["gain_db"] == -6.0
    assert calls[0]["align_to_beat"] is True

    uploaded = probe(blobs.kept[0])
    assert uploaded.has_audio, "the clip claims a soundtrack, so it has to have one"
    # `-t` bounds the copied video stream too, so a plan built from anything but
    # the picture's own length takes the end of the footage with it.
    assert uploaded.duration_sec == pytest.approx(8.0, abs=0.3)


def test_a_re_cut_that_changes_the_length_moves_the_excerpt_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`pick_section` reads the energy envelope over a window of the clip's own
    length, so a duration that moved chooses a different part of the track. The
    reviewer approved a soundtrack, not a track, and is told which one they got."""
    media = make_video(tmp_path / "src" / "source.mp4", seconds=120)
    track = make_track(tmp_path / "music" / "bed.wav")
    calls = mixes(monkeypatch)
    stage, clips, _ = build(
        tmp_path,
        the_clip=scored(tmp_path, track),
        the_source=source(media),
        the_candidate=candidate(),
    )

    stage.run(context(job(RemakeOptions(framing=Framing(mode=FramingMode.FIT), end_delta_sec=2.0))))

    assert calls[0]["forced_start_sec"] is None, "a two-second longer clip is a fresh pick"
    remade = clips.saved[0]
    assert remade.music is not None
    assert remade.music.music_start_sec != pytest.approx(2.0)
    assert remade.remake is not None
    assert any("different point in the track" in str(w.root) for w in remade.remake.warnings or [])


def test_a_track_that_cannot_be_fetched_costs_the_music_and_not_the_clip(
    tmp_path: Path,
) -> None:
    """The picture correction is what was asked for, and it is already encoded
    by the time the music pass runs. So the remake still returns a clip — one
    that says it has no soundtrack, which is the truth, and says why."""
    media = make_video(tmp_path / "src" / "source.mp4", seconds=120)
    collected = tmp_path / "music" / "collected.m4a"
    stage, clips, _ = build(
        tmp_path,
        the_clip=scored(tmp_path, collected),
        the_source=source(media),
        the_candidate=candidate(),
    )

    outcome = stage.run(context(job(RemakeOptions(framing=Framing(mode=FramingMode.FIT)))))

    assert outcome.metadata["clipId"] == clips.saved[0].id
    remade = clips.saved[0]
    assert remade.music is None, "a clip must never claim a soundtrack it does not contain"
    assert remade.remake is not None
    assert any("soundtrack" in str(r.root) for r in remade.remake.refusals or [])


def test_a_replaced_voice_takes_the_music_with_it_so_it_is_mixed_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The common case, not an edge one: the clip page sends REPLACE by default.

    The picture is untouched, which reads like the safe branch and is not —
    `SpeechMode.REPLACE` drops `[0:a]` from the graph entirely, and `[0:a]` is
    where the music lives. A carried REPLACE is downgraded to a bed on the way
    back, because otherwise the music would throw away the narration this job
    was asked to produce.
    """
    existing = make_video(tmp_path / "clips" / "clip-1.mp4", seconds=8)
    track = make_track(tmp_path / "music" / "bed.wav")
    calls = mixes(monkeypatch)
    stage, clips, blobs = build(
        tmp_path,
        the_clip=scored(
            tmp_path,
            track,
            local_path=str(existing),
            duration_sec=probe(existing).duration_sec,
        ),
        the_source=source(None),
        the_candidate=candidate(),
        synth=StubSynth(),
    )

    stage.run(context(job(RemakeOptions(voice=speaks(SpeechMode.REPLACE)))))

    assert len(calls) == 1, "the narration replaced the audio the music was in"
    assert calls[0]["mode"] is MusicMode.BED
    # Same length, so the same excerpt: the soundtrack that was approved, rather
    # than a new one chosen over the same track.
    assert calls[0]["forced_start_sec"] == 2.0
    remade = clips.saved[0]
    assert remade.music is not None
    assert remade.music.mode is MusicMode.BED
    assert remade.music.music_start_sec == pytest.approx(2.0)
    assert probe(blobs.kept[0]).has_audio
    assert remade.remake is not None
    assert any("underneath the voice" in str(w.root) for w in remade.remake.warnings or [])


def test_a_bedded_voice_keeps_the_music_that_is_already_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BED keeps `[0:a]`, and the music is in `[0:a]`. Mixing the track in again
    would leave the clip playing two copies of it, a few seconds apart."""
    existing = make_video(tmp_path / "clips" / "clip-1.mp4", seconds=8)
    track = make_track(tmp_path / "music" / "bed.wav")
    original = scored(tmp_path, track, local_path=str(existing))
    calls = mixes(monkeypatch)
    stage, clips, _ = build(
        tmp_path,
        the_clip=original,
        the_source=source(None),
        the_candidate=candidate(),
        synth=StubSynth(),
    )

    stage.run(context(job(RemakeOptions(voice=speaks(SpeechMode.BED)))))

    assert calls == []
    assert clips.saved[0].music == original.music


def test_dropping_the_music_is_an_explicit_opt_out_and_is_recorded(tmp_path: Path) -> None:
    """Keeping is the default, because the reviewer approved the soundtrack. An
    explicit false is the one way to be rid of it, and it is said out loud so a
    clip that comes back without music is not read as the bug it used to be."""
    media = make_video(tmp_path / "src" / "source.mp4", seconds=120)
    track = make_track(tmp_path / "music" / "bed.wav")
    stage, clips, _ = build(
        tmp_path,
        the_clip=scored(tmp_path, track),
        the_source=source(media),
        the_candidate=candidate(),
    )

    stage.run(context(job(RemakeOptions(framing=Framing(mode=FramingMode.FIT), keep_music=False))))

    remade = clips.saved[0]
    assert remade.music is None
    assert remade.remake is not None
    assert any("dropped, as asked" in str(w.root) for w in remade.remake.warnings or [])


def test_dropping_the_music_is_refused_when_nothing_rebuilt_the_audio(
    tmp_path: Path,
) -> None:
    """The branch where the request cannot be carried out, and used to be
    recorded as though it had been.

    Nothing re-cut the picture, so the reviewed file is handed straight on and
    the track is still mixed into its audio — a mix cannot be un-mixed, and
    there is no re-encode here for it to be left out of. Writing `music=None`
    over that was worse than inaccurate: the clip page gates the Add-a-track
    panel on `music` being null, so the null re-opened the door to mixing a
    second track over the first. The file is asserted byte for byte, because the
    record agreeing with it is the whole point.
    """
    existing = make_video(tmp_path / "clips" / "clip-1.mp4", seconds=8)
    track = make_track(tmp_path / "music" / "bed.wav")
    original = scored(tmp_path, track, local_path=str(existing))
    stage, clips, blobs = build(
        tmp_path, the_clip=original, the_source=source(None), the_candidate=candidate()
    )

    stage.run(
        context(job(RemakeOptions(notes="drop the music", interpret_notes=False, keep_music=False)))
    )

    remade = clips.saved[0]
    assert blobs.kept[0].read_bytes() == existing.read_bytes(), "the track is still in the file"
    assert remade.music == original.music, "so the record has to say so"
    assert remade.remake is not None
    assert any("could not be dropped" in str(r.root) for r in remade.remake.refusals or [])


def test_a_remake_sent_before_the_field_existed_still_keeps_the_music(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`keepMusic` is an opt-OUT, so None has to read as true. Read as false it
    would drop the soundtrack off every remake the PWA sent before it learned to
    set the field — which is the original bug, wearing a flag."""
    media = make_video(tmp_path / "src" / "source.mp4", seconds=120)
    track = make_track(tmp_path / "music" / "bed.wav")
    calls = mixes(monkeypatch)
    stage, clips, _ = build(
        tmp_path,
        the_clip=scored(tmp_path, track),
        the_source=source(media),
        the_candidate=candidate(),
    )

    options = RemakeOptions(framing=Framing(mode=FramingMode.FIT))
    assert options.keep_music is None
    stage.run(context(job(options)))

    assert len(calls) == 1
    assert clips.saved[0].music is not None


def test_a_long_explanation_is_cut_to_what_the_record_will_hold(tmp_path: Path) -> None:
    """`Warning` and `Refusal` are 300 characters, and they are validated where
    `AppliedRemake` is built — after the render, after the blob upload and after
    the poster. A yt-dlp failure interpolated into a sentence runs past 300
    routinely, so the cost of exceeding it is not a truncated sentence but the
    loss of a clip that was already made."""
    stage, _, _ = build(tmp_path)
    stage._note("the soundtrack could not be put back on this clip: " + "x" * 400)
    stage._refuse("the footage could not be searched for fixed marks: " + "y" * 400)

    AppliedRemake(
        framing_mode=FramingMode.AS_RENDERED,
        start_sec=100.0,
        end_sec=108.0,
        warnings=stage._notes,
        refusals=stage._refusals,
    )
