"""The COMPOSE stages: what each writes, what each tolerates, and how they resume.

Fake model, fake voice, fake transcriber, fake renderer, fake stores. What is
under test is the orchestration — the script that is spoken is the script
that was written, a given script is spoken verbatim, a missing model degrades
where it can and refuses where it cannot, and a crash mid-draw redraws only
what is missing.
"""

# The fakes below stand in for the stores by shape, not by type. Said once here
# rather than on every argument, because the formatter moves a trailing comment
# off the line it was written for.
# mypy: disable-error-code="arg-type,return-value"

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from clipforge.config import Settings
from clipforge.media.assemble import AssembledClip
from clipforge.media.speech import SpeechError, Utterance
from clipforge.models.broker import ModelBroker
from clipforge.models.ollama import OllamaError
from clipforge.stages.base import StageContext
from clipforge.stages.compose import (
    AlignStage,
    ComposeError,
    DrawStage,
    NarrateStage,
    ScriptStage,
    compose_dir,
)
from clipforge_contracts import (
    ComposeOptions,
    Job,
    JobStatus,
    JobType,
    Lane,
    LlmScene,
    LlmScriptResponse,
    SceneMood,
    Stage,
    StageName,
    StageStatus,
    StickActor,
    StickMood,
    StickPose,
    StickProp,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


# ── Fakes ────────────────────────────────────────────────────────────────────


class _Stats:
    def summary(self) -> dict[str, float | int]:
        return {"calls": 1}


class ScriptedModel:
    model = "qwen-test"

    def __init__(self, *, available: bool = True, fail: OllamaError | None = None) -> None:
        self._available = available
        self._fail = fail
        self.prompts: list[str] = []
        self.stats = _Stats()

    def is_available(self) -> bool:
        return self._available

    def generate_structured(self, *, schema_model: Any, system: str, prompt: str, **_: Any) -> Any:
        self.prompts.append(prompt)
        if self._fail is not None:
            raise self._fail
        return LlmScriptResponse(
            title="The late winner",
            scenes=[
                LlmScene(
                    line="Arsenal scored in the ninetieth minute.",
                    actors=[
                        StickActor(name="a fan", pose=StickPose.CELEBRATE, mood=StickMood.HAPPY)
                    ],
                    props=[StickProp.BALL],
                    mood=SceneMood.UPBEAT,
                ),
                LlmScene(
                    line="Nobody saw it coming.",
                    actors=[
                        StickActor(
                            name="the manager", pose=StickPose.SHRUG, mood=StickMood.SURPRISED
                        )
                    ],
                    props=[StickProp.SIGN],
                    mood=SceneMood.TENSE,
                    label="90'",
                ),
            ],
        )


class FakeSynth:
    engine = "fake-tts:1"

    def __init__(self, *, fail: bool = False) -> None:
        self.spoken: list[tuple[str, str]] = []
        self._fail = fail

    def voices(self) -> tuple[str, ...]:
        return ("af_heart",)

    def speak(
        self, text: str, *, voice: str, language: str, speed: float, destination: Path
    ) -> Utterance:
        if self._fail:
            raise SpeechError("no voice weights")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"RIFF")
        self.spoken.append((text, voice))
        return Utterance(
            path=destination,
            duration_sec=6.0,
            sample_rate=24000,
            text=text,
            voice=voice,
            language=language,
            engine=self.engine,
        )


class _Aligned:
    def __init__(self, words: list[TranscriptWord]) -> None:
        self.transcript = Transcript(
            source_id="x",
            model_version="fake",
            language="en",
            duration_sec=6.0,
            segments=[
                TranscriptSegment(
                    index=0,
                    start_sec=0.0,
                    end_sec=6.0,
                    text=" ".join(w.text for w in words),
                    words=words,
                )
            ],
            created_at=NOW,
        )


class FakeTranscriber:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.asked: list[str] = []

    def transcribe(
        self, audio_path: Path, *, source_id: str, language: str | None = None
    ) -> _Aligned:
        self.asked.append(source_id)
        if self._fail:
            raise RuntimeError("CUDA fell over")
        words = [
            TranscriptWord(text=t, start_sec=s, end_sec=s + 0.3)
            for t, s in [
                ("Arsenal", 0.1),
                ("scored", 0.5),
                ("in", 0.8),
                ("the", 1.0),
                ("ninetieth", 1.3),
                ("minute", 1.8),
                ("Nobody", 3.5),
                ("saw", 3.9),
                ("it", 4.2),
                ("coming", 4.5),
            ]
        ]
        return _Aligned(words)


class FakeRenderer:
    def __init__(self, *, fail_on: int | None = None) -> None:
        self.rendered: list[int] = []
        self._fail_on = fail_on

    def __call__(self, scene: Any, destination: Path, **kwargs: Any) -> AssembledClip:
        from clipforge.media.render import RenderError

        if scene.index == self._fail_on:
            raise RenderError("ffmpeg said no")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"mp4")
        self.rendered.append(scene.index)
        return AssembledClip(path=destination, duration_sec=scene.duration_sec, size_bytes=3)


class FakeWorkspace:
    def __init__(self, root: Path) -> None:
        self.tmp_dir = root / "tmp"


# ── Fixtures ─────────────────────────────────────────────────────────────────


def options(**over: Any) -> ComposeOptions:
    base: dict[str, Any] = {"topic": "Arsenal's late winner", "target_duration_sec": 30}
    base.update(over)
    return ComposeOptions(**base)


def job(opts: ComposeOptions, *, checkpoints: dict[StageName, dict[str, Any]] | None = None) -> Job:
    checkpoints = checkpoints or {}
    names = [
        StageName.SCRIPT,
        StageName.NARRATE,
        StageName.ALIGN,
        StageName.DRAW,
        StageName.ASSEMBLE,
    ]
    lanes = [Lane.GPU, Lane.CPU, Lane.GPU, Lane.CPU, Lane.CPU]
    return Job(
        id="job-compose",
        uid="user-1",
        type=JobType.COMPOSE,
        status=JobStatus.RUNNING,
        compose_options=opts,
        stages=[
            Stage(
                name=name,
                lane=lane,
                status=StageStatus.DONE if name in checkpoints else StageStatus.PENDING,
                checkpoint=checkpoints.get(name),
            )
            for name, lane in zip(names, lanes, strict=True)
        ],
        attempts=0,
        max_attempts=2,
        created_at=NOW,
        updated_at=NOW,
    )


def context(
    the_job: Job, *, stage: StageName, checkpoint: dict[str, Any] | None = None, stop: bool = False
) -> StageContext:
    settings = Settings(_env_file=None)
    event = Event()
    if stop:
        event.set()
    return StageContext(
        job=the_job,
        stage_name=stage,
        checkpoint=checkpoint,
        settings=settings,
        broker=ModelBroker(reserve_mb=settings.vram_reserve_mb),
        should_stop=event,
    )


SCRIPT_CP: dict[str, Any] = {
    "title": "The late winner",
    "script": "Arsenal scored in the ninetieth minute. Nobody saw it coming.",
    "scenes": [
        {
            "index": 0,
            "line": "Arsenal scored in the ninetieth minute.",
            "actors": [{"name": "a fan", "pose": "CELEBRATE", "mood": "HAPPY"}],
            "props": ["BALL"],
            "mood": "UPBEAT",
            "label": None,
        },
        {
            "index": 1,
            "line": "Nobody saw it coming.",
            "actors": [{"name": "the manager", "pose": "SHRUG", "mood": "SURPRISED"}],
            "props": ["SIGN"],
            "mood": "TENSE",
            "label": "90'",
        },
    ],
    "scriptBy": "MODEL",
    "modelVersion": "ollama:qwen-test",
    "promptVersion": "script-v1",
}


# ── SCRIPT ───────────────────────────────────────────────────────────────────


def test_script_writes_the_scenes_and_the_script_they_add_up_to() -> None:
    model = ScriptedModel()
    outcome = ScriptStage(client_factory=lambda _ctx: model).run(
        context(job(options()), stage=StageName.SCRIPT)
    )
    assert outcome.checkpoint is not None
    cp = outcome.checkpoint
    assert cp["title"] == "The late winner"
    assert cp["script"] == "Arsenal scored in the ninetieth minute. Nobody saw it coming."
    assert [s["line"] for s in cp["scenes"]] == [
        "Arsenal scored in the ninetieth minute.",
        "Nobody saw it coming.",
    ]
    assert cp["scenes"][1]["label"] == "90'"
    assert cp["scriptBy"] == "MODEL"
    assert cp["modelVersion"] == "ollama:qwen-test"
    assert cp["promptVersion"] == "script-v1"
    assert "Write the narration" in model.prompts[0]
    assert "2 scenes" in (outcome.detail or "")


def test_a_given_script_is_kept_word_for_word_and_the_model_only_stages_it() -> None:
    model = ScriptedModel()
    given = "These are my exact words. They must not change."
    outcome = ScriptStage(client_factory=lambda _ctx: model).run(
        context(job(options(script=given)), stage=StageName.SCRIPT)
    )
    assert outcome.checkpoint is not None
    assert outcome.checkpoint["script"] == given
    assert outcome.checkpoint["scriptBy"] == "OPERATOR"
    assert "spoken exactly as it is" in model.prompts[0]
    # The model's staging of the first line is used; its words are not.
    assert outcome.checkpoint["scenes"][0]["props"] == ["BALL"]


def test_no_model_refuses_to_write_but_still_draws_a_given_script_plainly() -> None:
    stage = ScriptStage(client_factory=lambda _ctx: ScriptedModel(available=False))
    with pytest.raises(ComposeError) as refused:
        stage.run(context(job(options()), stage=StageName.SCRIPT))
    assert refused.value.code == "COMPOSE_NO_MODEL"
    assert refused.value.retryable is True

    outcome = stage.run(
        context(job(options(script="One. Two three four five.")), stage=StageName.SCRIPT)
    )
    assert outcome.checkpoint is not None
    assert [s["line"] for s in outcome.checkpoint["scenes"]] == ["One. Two three four five."]
    assert outcome.checkpoint["scenes"][0]["actors"][0]["pose"] == "TALK"
    assert outcome.checkpoint["modelVersion"] is None


def test_a_model_that_will_not_answer_fails_a_written_script_and_degrades_a_given_one() -> None:
    bad = OllamaError("twice invalid", retryable=False)
    stage = ScriptStage(client_factory=lambda _ctx: ScriptedModel(fail=bad))
    with pytest.raises(ComposeError) as refused:
        stage.run(context(job(options()), stage=StageName.SCRIPT))
    assert refused.value.retryable is False

    outcome = stage.run(context(job(options(script="Given words here.")), stage=StageName.SCRIPT))
    assert outcome.checkpoint is not None
    assert outcome.checkpoint["script"] == "Given words here."


def test_a_model_that_is_down_is_retryable() -> None:
    stage = ScriptStage(
        client_factory=lambda _ctx: ScriptedModel(fail=OllamaError("timeout", retryable=True))
    )
    with pytest.raises(ComposeError) as refused:
        stage.run(context(job(options()), stage=StageName.SCRIPT))
    assert refused.value.retryable is True


def test_an_empty_given_script_is_refused() -> None:
    stage = ScriptStage(client_factory=lambda _ctx: ScriptedModel())
    with pytest.raises(ComposeError) as refused:
        stage.run(context(job(options(script="   ")), stage=StageName.SCRIPT))
    assert refused.value.code == "COMPOSE_EMPTY_SCRIPT"


# ── NARRATE ──────────────────────────────────────────────────────────────────


def test_narrate_speaks_the_script_that_was_written_in_the_default_voice(tmp_path: Path) -> None:
    synth = FakeSynth()
    stage = NarrateStage(workspace=FakeWorkspace(tmp_path), synth_factory=lambda _ctx: synth)
    the_job = job(options(), checkpoints={StageName.SCRIPT: SCRIPT_CP})
    outcome = stage.run(context(the_job, stage=StageName.NARRATE))
    assert synth.spoken == [(SCRIPT_CP["script"], "af_heart")]
    assert outcome.checkpoint is not None
    assert outcome.checkpoint["durationSec"] == 6.0
    assert outcome.checkpoint["engine"] == "fake-tts:1"
    assert Path(outcome.checkpoint["path"]).parent == compose_dir(
        FakeWorkspace(tmp_path), "job-compose"
    )


def test_narrate_uses_the_voice_asked_for_and_fails_plainly_without_weights(tmp_path: Path) -> None:
    synth = FakeSynth()
    stage = NarrateStage(workspace=FakeWorkspace(tmp_path), synth_factory=lambda _ctx: synth)
    stage.run(
        context(
            job(options(voice="bm_george"), checkpoints={StageName.SCRIPT: SCRIPT_CP}),
            stage=StageName.NARRATE,
        )
    )
    assert synth.spoken[0][1] == "bm_george"

    broken = NarrateStage(
        workspace=FakeWorkspace(tmp_path), synth_factory=lambda _ctx: FakeSynth(fail=True)
    )
    with pytest.raises(ComposeError) as refused:
        broken.run(
            context(
                job(options(), checkpoints={StageName.SCRIPT: SCRIPT_CP}), stage=StageName.NARRATE
            )
        )
    assert refused.value.code == "COMPOSE_SPEECH"
    assert refused.value.retryable is False


# ── ALIGN ────────────────────────────────────────────────────────────────────


def narrated(tmp_path: Path) -> dict[str, Any]:
    folder = compose_dir(FakeWorkspace(tmp_path), "job-compose")
    folder.mkdir(parents=True, exist_ok=True)
    wav = folder / "narration.wav"
    wav.write_bytes(b"RIFF")
    return {
        "path": str(wav),
        "durationSec": 6.0,
        "voice": "af_heart",
        "engine": "fake-tts:1",
        "language": "en-us",
    }


def test_align_records_the_words_it_heard(tmp_path: Path) -> None:
    transcriber = FakeTranscriber()
    stage = AlignStage(transcriber_factory=lambda _ctx: transcriber)
    the_job = job(
        options(), checkpoints={StageName.SCRIPT: SCRIPT_CP, StageName.NARRATE: narrated(tmp_path)}
    )
    outcome = stage.run(context(the_job, stage=StageName.ALIGN))
    assert transcriber.asked == ["compose-job-compose"]
    assert outcome.checkpoint is not None
    assert outcome.checkpoint["count"] == 10
    assert outcome.checkpoint["words"][6] == {"text": "Nobody", "startSec": 3.5, "endSec": 3.8}


def test_align_is_skipped_without_a_transcriber_or_when_it_fails(tmp_path: Path) -> None:
    the_job = job(
        options(), checkpoints={StageName.SCRIPT: SCRIPT_CP, StageName.NARRATE: narrated(tmp_path)}
    )
    none = AlignStage(transcriber_factory=lambda _ctx: None).run(
        context(the_job, stage=StageName.ALIGN)
    )
    assert none.skipped is True and "word count" in (none.detail or "")
    failed = AlignStage(transcriber_factory=lambda _ctx: FakeTranscriber(fail=True)).run(
        context(the_job, stage=StageName.ALIGN)
    )
    assert failed.skipped is True and "CUDA fell over" in (failed.detail or "")


def test_align_asks_for_the_narration_again_when_it_is_gone(tmp_path: Path) -> None:
    gone = {**narrated(tmp_path), "path": str(tmp_path / "missing.wav")}
    the_job = job(options(), checkpoints={StageName.SCRIPT: SCRIPT_CP, StageName.NARRATE: gone})
    with pytest.raises(ComposeError) as refused:
        AlignStage(transcriber_factory=lambda _ctx: FakeTranscriber()).run(
            context(the_job, stage=StageName.ALIGN)
        )
    assert refused.value.retryable is True


# ── DRAW ─────────────────────────────────────────────────────────────────────

ALIGN_CP: dict[str, Any] = {
    "words": [
        {"text": t, "startSec": s, "endSec": s + 0.3}
        for t, s in [
            ("Arsenal", 0.1),
            ("scored", 0.5),
            ("in", 0.8),
            ("the", 1.0),
            ("ninetieth", 1.3),
            ("minute", 1.8),
            ("Nobody", 3.5),
            ("saw", 3.9),
            ("it", 4.2),
            ("coming", 4.5),
        ]
    ],
    "count": 10,
}


def test_draw_times_the_scenes_by_the_voice_and_renders_each(tmp_path: Path) -> None:
    renderer = FakeRenderer()
    stage = DrawStage(workspace=FakeWorkspace(tmp_path), renderer=renderer)
    the_job = job(
        options(seed=4),
        checkpoints={
            StageName.SCRIPT: SCRIPT_CP,
            StageName.NARRATE: narrated(tmp_path),
            StageName.ALIGN: ALIGN_CP,
        },
    )
    outcome = stage.run(context(the_job, stage=StageName.DRAW))
    assert renderer.rendered == [0, 1]
    assert outcome.checkpoint is not None
    timed = outcome.checkpoint["timed"]
    assert (timed[0]["startSec"], timed[0]["endSec"]) == (0.0, 3.5)
    assert (timed[1]["startSec"], timed[1]["endSec"]) == (3.5, 6.0)
    assert outcome.checkpoint["scenes"]["1"]["durationSec"] == 2.5
    assert outcome.checkpoint["fps"] == 30


def test_draw_resumes_past_scenes_already_on_disk(tmp_path: Path) -> None:
    first = FakeRenderer(fail_on=1)
    stage = DrawStage(workspace=FakeWorkspace(tmp_path), renderer=first)
    the_job = job(
        options(),
        checkpoints={
            StageName.SCRIPT: SCRIPT_CP,
            StageName.NARRATE: narrated(tmp_path),
            StageName.ALIGN: ALIGN_CP,
        },
    )
    with pytest.raises(ComposeError) as refused:
        stage.run(context(the_job, stage=StageName.DRAW))
    assert refused.value.retryable is True
    assert first.rendered == [0]

    # The runner would hand back the partial checkpoint on the next attempt;
    # here the first scene is on disk, so only the second is drawn.
    partial = {
        "scenes": {
            "0": {
                "path": str(compose_dir(FakeWorkspace(tmp_path), "job-compose") / "scene-00.mp4"),
                "startSec": 0.0,
                "endSec": 3.5,
                "durationSec": 3.5,
            }
        }
    }
    second = FakeRenderer()
    outcome = DrawStage(workspace=FakeWorkspace(tmp_path), renderer=second).run(
        context(the_job, stage=StageName.DRAW, checkpoint=partial)
    )
    assert second.rendered == [1]
    assert outcome.checkpoint is not None
    assert sorted(outcome.checkpoint["scenes"]) == ["0", "1"]


def test_draw_stops_when_asked_and_keeps_what_it_drew(tmp_path: Path) -> None:
    renderer = FakeRenderer()
    the_job = job(
        options(), checkpoints={StageName.SCRIPT: SCRIPT_CP, StageName.NARRATE: narrated(tmp_path)}
    )
    outcome = DrawStage(workspace=FakeWorkspace(tmp_path), renderer=renderer).run(
        context(the_job, stage=StageName.DRAW, stop=True)
    )
    assert outcome.incomplete is True
    assert renderer.rendered == []


def test_draw_without_timings_shares_time_by_word_count(tmp_path: Path) -> None:
    renderer = FakeRenderer()
    the_job = job(
        options(), checkpoints={StageName.SCRIPT: SCRIPT_CP, StageName.NARRATE: narrated(tmp_path)}
    )
    outcome = DrawStage(workspace=FakeWorkspace(tmp_path), renderer=renderer).run(
        context(the_job, stage=StageName.DRAW)
    )
    assert outcome.checkpoint is not None
    timed = outcome.checkpoint["timed"]
    # Six words then four: 3.6s and 2.4s of six.
    assert (timed[0]["startSec"], timed[0]["endSec"]) == (0.0, 3.6)
    assert (timed[1]["startSec"], timed[1]["endSec"]) == (3.6, 6.0)
