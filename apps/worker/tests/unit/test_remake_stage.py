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

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from clipforge.config import Settings
from clipforge.media.speech import Utterance
from clipforge.models.broker import ModelBroker
from clipforge.stages.base import StageContext
from clipforge.stages.remake import RemakeStage, RemakeStageError
from clipforge.store.blobs import BlobRef
from clipforge_contracts import (
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
    def __init__(self) -> None:
        self.puts: list[tuple[str, Path]] = []

    def put(
        self, key: str, source: Path, *, content_type: str | None = None, required: bool = False
    ) -> BlobRef:
        self.puts.append((key, source))
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
    blobs = FakeBlobStore()
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
    the corrected clip looks like an orphan: the source it was cut from, the
    candidate that selected it, and the soundtrack already applied to it.
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
    assert remade.music == original.music
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
