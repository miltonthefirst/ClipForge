"""The COMPOSE stages: what each writes, what each tolerates, and how they resume.

Fake model, fake voice, fake transcriber, fake renderer, fake stores. What is
under test is the orchestration — every line is spoken in its character's
own voice and the timeline says exactly when, a given script is spoken
verbatim, a missing model degrades where it can and refuses where it cannot,
and a crash mid-draw redraws only what is missing.
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

import numpy as np
import pytest
from clipforge.config import Settings
from clipforge.media.assemble import AssembledClip
from clipforge.media.speech import SpeechError, Utterance, write_wav
from clipforge.models.broker import ModelBroker
from clipforge.models.ollama import OllamaError
from clipforge.stages.base import StageContext
from clipforge.stages.compose import (
    LINE_GAP_SEC,
    SCENE_GAP_SEC,
    TAIL_SEC,
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
    LlmCharacter,
    LlmLine,
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
    VoiceKind,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
RATE = 24000


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
            cast=[
                LlmCharacter(name="a fan", voice=VoiceKind.MAN_UK),
                LlmCharacter(name="Ada", voice=VoiceKind.WOMAN_US),
            ],
            scenes=[
                LlmScene(
                    lines=[
                        LlmLine(speaker="a fan", text="Arsenal scored in the ninetieth minute!"),
                        LlmLine(speaker="Ada", text="Nobody saw it coming."),
                    ],
                    actors=[
                        StickActor(name="a fan", pose=StickPose.CELEBRATE, mood=StickMood.HAPPY),
                        StickActor(name="Ada", pose=StickPose.SHRUG, mood=StickMood.SURPRISED),
                    ],
                    props=[StickProp.BALL],
                    mood=SceneMood.UPBEAT,
                ),
                LlmScene(
                    lines=[LlmLine(speaker="Ada", text="So what changed?")],
                    actors=[StickActor(name="Ada", pose=StickPose.THINK, mood=StickMood.NEUTRAL)],
                    props=[StickProp.SIGN],
                    mood=SceneMood.TENSE,
                    label="90'",
                ),
            ],
        )


class FakeSynth:
    """Speaks a line as half a second of silence per ten characters, in the voice asked."""

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
        seconds = 0.5 * max(1, len(text) // 10)
        duration = write_wav(
            np.zeros(int(seconds * RATE), dtype=np.float32),
            sample_rate=RATE,
            destination=destination,
        )
        self.spoken.append((text, voice))
        return Utterance(
            path=destination,
            duration_sec=duration,
            sample_rate=RATE,
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
            for t, s in [("Arsenal", 0.1), ("scored", 0.5), ("Nobody", 3.5), ("saw", 3.9)]
        ]
        return _Aligned(words)


class FakeRenderer:
    def __init__(self, *, fail_on: int | None = None) -> None:
        self.rendered: list[int] = []
        self.scenes: list[Any] = []
        self._fail_on = fail_on

    def __call__(self, scene: Any, destination: Path, **kwargs: Any) -> AssembledClip:
        from clipforge.media.render import RenderError

        if scene.index == self._fail_on:
            raise RenderError("ffmpeg said no")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"mp4")
        self.rendered.append(scene.index)
        self.scenes.append(scene)
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
    "script": "a fan: Arsenal scored in the ninetieth minute!\nAda: Nobody saw it coming.\nAda: So what changed?",
    "cast": [{"name": "a fan", "kind": "MAN_UK"}, {"name": "Ada", "kind": "WOMAN_US"}],
    "scenes": [
        {
            "index": 0,
            "lines": [
                {"speaker": "a fan", "text": "Arsenal scored in the ninetieth minute!"},
                {"speaker": "Ada", "text": "Nobody saw it coming."},
            ],
            "actors": [
                {"name": "a fan", "pose": "CELEBRATE", "mood": "HAPPY"},
                {"name": "Ada", "pose": "SHRUG", "mood": "SURPRISED"},
            ],
            "props": ["BALL"],
            "mood": "UPBEAT",
            "label": None,
        },
        {
            "index": 1,
            "lines": [{"speaker": "Ada", "text": "So what changed?"}],
            "actors": [{"name": "Ada", "pose": "THINK", "mood": "NEUTRAL"}],
            "props": ["SIGN"],
            "mood": "TENSE",
            "label": "90'",
        },
    ],
    "scriptBy": "MODEL",
    "modelVersion": "ollama:qwen-test",
    "promptVersion": "script-v2",
}


# ── SCRIPT ───────────────────────────────────────────────────────────────────


def test_script_writes_a_cast_and_scenes_of_dialogue() -> None:
    model = ScriptedModel()
    outcome = ScriptStage(client_factory=lambda _ctx: model).run(
        context(job(options()), stage=StageName.SCRIPT)
    )
    assert outcome.checkpoint is not None
    cp = outcome.checkpoint
    assert cp["title"] == "The late winner"
    assert cp["cast"] == [{"name": "a fan", "kind": "MAN_UK"}, {"name": "Ada", "kind": "WOMAN_US"}]
    assert [[line["speaker"] for line in s["lines"]] for s in cp["scenes"]] == [
        ["a fan", "Ada"],
        ["Ada"],
    ]
    assert cp["script"].splitlines()[0] == "a fan: Arsenal scored in the ninetieth minute!"
    assert cp["scenes"][1]["label"] == "90'"
    assert cp["scriptBy"] == "MODEL"
    assert cp["promptVersion"] == "script-v2"
    assert "Write a 30-second conversation" in model.prompts[0]
    assert "2 scenes, 2 characters" in (outcome.detail or "")


def test_a_given_dialogue_is_kept_word_for_word_and_the_model_only_stages_it() -> None:
    model = ScriptedModel()
    given = "Ada: These are my exact words.\nBen: They must not change."
    outcome = ScriptStage(client_factory=lambda _ctx: model).run(
        context(job(options(script=given)), stage=StageName.SCRIPT)
    )
    assert outcome.checkpoint is not None
    assert outcome.checkpoint["script"] == given
    assert outcome.checkpoint["scriptBy"] == "OPERATOR"
    assert "spoken exactly as it is" in model.prompts[0]
    # The model's staging of the first scene is used; its words are not.
    assert outcome.checkpoint["scenes"][0]["props"] == ["BALL"]
    # Ben spoke and was not in the model's cast; he is cast anyway.
    assert [member["name"] for member in outcome.checkpoint["cast"]] == ["a fan", "Ada", "Ben"]


def test_no_model_refuses_to_write_but_still_stages_a_given_dialogue_plainly() -> None:
    stage = ScriptStage(client_factory=lambda _ctx: ScriptedModel(available=False))
    with pytest.raises(ComposeError) as refused:
        stage.run(context(job(options()), stage=StageName.SCRIPT))
    assert refused.value.code == "COMPOSE_NO_MODEL"
    assert refused.value.retryable is True

    outcome = stage.run(
        context(job(options(script="Ada: One.\nBen: Two.\nAda: Three.")), stage=StageName.SCRIPT)
    )
    assert outcome.checkpoint is not None
    cp = outcome.checkpoint
    assert [[line["text"] for line in s["lines"]] for s in cp["scenes"]] == [
        ["One.", "Two."],
        ["Three."],
    ]
    assert [a["name"] for a in cp["scenes"][0]["actors"]] == ["Ada", "Ben"]
    assert cp["cast"][0]["name"] == "Ada" and cp["cast"][1]["name"] == "Ben"
    assert cp["cast"][0]["kind"] != cp["cast"][1]["kind"]
    assert cp["modelVersion"] is None


def test_a_model_that_is_down_is_retryable_and_an_empty_script_is_refused() -> None:
    stage = ScriptStage(
        client_factory=lambda _ctx: ScriptedModel(fail=OllamaError("timeout", retryable=True))
    )
    with pytest.raises(ComposeError) as refused:
        stage.run(context(job(options()), stage=StageName.SCRIPT))
    assert refused.value.retryable is True
    with pytest.raises(ComposeError) as empty:
        ScriptStage(client_factory=lambda _ctx: ScriptedModel()).run(
            context(job(options(script="   ")), stage=StageName.SCRIPT)
        )
    assert empty.value.code == "COMPOSE_EMPTY_SCRIPT"


# ── NARRATE ──────────────────────────────────────────────────────────────────


def test_narrate_speaks_each_line_in_its_character_s_voice_and_times_them(tmp_path: Path) -> None:
    synth = FakeSynth()
    stage = NarrateStage(workspace=FakeWorkspace(tmp_path), synth_factory=lambda _ctx: synth)
    the_job = job(options(seed=0), checkpoints={StageName.SCRIPT: SCRIPT_CP})
    outcome = stage.run(context(the_job, stage=StageName.NARRATE))

    voices = [voice for _, voice in synth.spoken]
    assert voices[0].startswith("bm_") and voices[1].startswith("af_") and voices[2] == voices[1]
    assert voices[1] != "af_heart", "a character does not share the narrator's voice"
    assert outcome.checkpoint is not None
    cp = outcome.checkpoint
    assert cp["voice"] == "af_heart"
    assert [member["voice"] for member in cp["cast"]] == [voices[0], voices[1]]
    lines = cp["lines"]
    assert [line["speaker"] for line in lines] == ["a fan", "Ada", "Ada"]
    # 39 chars → 1.5 s; then a beat; 21 chars → 1.0 s; a longer beat; 16 chars → 0.5 s.
    assert (lines[0]["startSec"], lines[0]["endSec"]) == (0.0, 1.5)
    assert lines[1]["startSec"] == pytest.approx(1.5 + LINE_GAP_SEC, abs=0.01)
    assert lines[2]["scene"] == 1
    assert lines[2]["startSec"] == pytest.approx(lines[1]["endSec"] + SCENE_GAP_SEC, abs=0.01)
    assert cp["durationSec"] == pytest.approx(lines[2]["endSec"] + TAIL_SEC, abs=0.01)
    assert Path(cp["path"]) == compose_dir(FakeWorkspace(tmp_path), "job-compose") / "narration.wav"
    assert Path(cp["path"]).is_file()
    assert "2 voices" in (outcome.detail or "")


def test_narrate_uses_the_voice_asked_for_as_the_narrator_s_and_fails_plainly_without_weights(
    tmp_path: Path,
) -> None:
    synth = FakeSynth()
    stage = NarrateStage(workspace=FakeWorkspace(tmp_path), synth_factory=lambda _ctx: synth)
    with_narrator = {
        **SCRIPT_CP,
        "scenes": [
            {**SCRIPT_CP["scenes"][0], "lines": [{"speaker": "Narrator", "text": "Last night."}]}
        ],
    }
    stage.run(
        context(
            job(options(voice="bm_george"), checkpoints={StageName.SCRIPT: with_narrator}),
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
    assert "a fan's line" in str(refused.value)


# ── ALIGN ────────────────────────────────────────────────────────────────────


def narrated(tmp_path: Path) -> dict[str, Any]:
    folder = compose_dir(FakeWorkspace(tmp_path), "job-compose")
    folder.mkdir(parents=True, exist_ok=True)
    wav = folder / "narration.wav"
    write_wav(np.zeros(RATE, dtype=np.float32), sample_rate=RATE, destination=wav)
    return {
        "path": str(wav),
        "durationSec": 6.0,
        "voice": "af_heart",
        "engine": "fake-tts:1",
        "language": "en-us",
        "cast": [
            {"name": "a fan", "kind": "MAN_UK", "voice": "bm_george"},
            {"name": "Ada", "kind": "WOMAN_US", "voice": "af_bella"},
        ],
        "lines": [
            {
                "scene": 0,
                "index": 0,
                "speaker": "a fan",
                "text": "Arsenal scored in the ninetieth minute!",
                "voice": "bm_george",
                "startSec": 0.0,
                "endSec": 2.0,
            },
            {
                "scene": 0,
                "index": 1,
                "speaker": "Ada",
                "text": "Nobody saw it coming.",
                "voice": "af_bella",
                "startSec": 2.35,
                "endSec": 3.5,
            },
            {
                "scene": 1,
                "index": 0,
                "speaker": "Ada",
                "text": "So what changed?",
                "voice": "af_bella",
                "startSec": 4.1,
                "endSec": 5.4,
            },
        ],
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
    assert outcome.checkpoint["count"] == 4
    assert outcome.checkpoint["words"][2] == {"text": "Nobody", "startSec": 3.5, "endSec": 3.8}


def test_align_is_skipped_without_a_transcriber_or_when_it_fails(tmp_path: Path) -> None:
    the_job = job(
        options(), checkpoints={StageName.SCRIPT: SCRIPT_CP, StageName.NARRATE: narrated(tmp_path)}
    )
    none = AlignStage(transcriber_factory=lambda _ctx: None).run(
        context(the_job, stage=StageName.ALIGN)
    )
    assert none.skipped is True and "captions are left off" in (none.detail or "")
    failed = AlignStage(transcriber_factory=lambda _ctx: FakeTranscriber(fail=True)).run(
        context(the_job, stage=StageName.ALIGN)
    )
    assert failed.skipped is True and "CUDA fell over" in (failed.detail or "")


# ── DRAW ─────────────────────────────────────────────────────────────────────


def test_draw_times_the_scenes_from_the_lines_and_hands_each_its_speakers(tmp_path: Path) -> None:
    renderer = FakeRenderer()
    stage = DrawStage(workspace=FakeWorkspace(tmp_path), renderer=renderer)
    the_job = job(
        options(seed=4),
        checkpoints={StageName.SCRIPT: SCRIPT_CP, StageName.NARRATE: narrated(tmp_path)},
    )
    outcome = stage.run(context(the_job, stage=StageName.DRAW))
    assert renderer.rendered == [0, 1]
    assert outcome.checkpoint is not None
    timed = outcome.checkpoint["timed"]
    # The second scene begins a beat before its first line at 4.1 s.
    assert (timed[0]["startSec"], timed[0]["endSec"]) == (0.0, 3.85)
    assert (timed[1]["startSec"], timed[1]["endSec"]) == (3.85, 6.0)
    assert [line["speaker"] for line in timed[0]["spoken"]] == ["a fan", "Ada"]
    # The renderer was given the timeline, which is how it knows who talks when.
    assert [line.voice for line in renderer.scenes[0].timed_lines] == ["bm_george", "af_bella"]
    assert outcome.checkpoint["scenes"]["1"]["durationSec"] == 2.15


def test_draw_resumes_past_scenes_already_on_disk(tmp_path: Path) -> None:
    first = FakeRenderer(fail_on=1)
    the_job = job(
        options(), checkpoints={StageName.SCRIPT: SCRIPT_CP, StageName.NARRATE: narrated(tmp_path)}
    )
    with pytest.raises(ComposeError) as refused:
        DrawStage(workspace=FakeWorkspace(tmp_path), renderer=first).run(
            context(the_job, stage=StageName.DRAW)
        )
    assert refused.value.retryable is True
    assert first.rendered == [0]

    partial = {
        "scenes": {
            "0": {
                "path": str(compose_dir(FakeWorkspace(tmp_path), "job-compose") / "scene-00.mp4"),
                "startSec": 0.0,
                "endSec": 3.85,
                "durationSec": 3.85,
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
