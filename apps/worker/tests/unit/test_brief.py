"""The brief: what a CLIP job asks ANALYZE to look for, and how it is honoured.

A scripted model stands in for Ollama. What is under test is that the job's
options reach the prompt, the ranking and the record — and that a brief which
matches nothing is reported as that, not as a failure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from threading import Event
from typing import Any

import pytest
from clipforge.analysis.prompts import (
    BRIEF_PROMPT_VERSION,
    PROMPT_VERSION,
    THEMED_PROMPT_VERSION,
    build_prompt,
)
from clipforge.analysis.windows import DEFAULT_WINDOW_SPEC
from clipforge.config import Settings
from clipforge.models.broker import ModelBroker
from clipforge.stages.analyze import AnalyzeStage, _brief_of, _window_spec_for
from clipforge.stages.base import StageContext
from clipforge_contracts import (
    Candidate,
    ClipOptions,
    Job,
    JobStatus,
    JobType,
    Lane,
    LlmClipProposal,
    LlmClipResponse,
    Stage,
    StageName,
    StageStatus,
    SubScores,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
)

# The fakes below stand in for the stores by shape, not by type.
# mypy: disable-error-code="arg-type"

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


# ── The prompt ───────────────────────────────────────────────────────────────


def test_a_brief_is_told_to_the_model_as_a_filter() -> None:
    prompt = build_prompt(
        window_text="...", start_sec=0.0, end_sec=120.0, instructions="  the goals  "
    )
    assert "asked for: the goals" in prompt
    assert "Return ONLY moments that fit" in prompt
    assert "cannot see the picture" in prompt


def test_no_brief_leaves_the_prompt_as_it_was() -> None:
    plain = build_prompt(window_text="...", start_sec=0.0, end_sec=120.0)
    assert "asked for" not in plain
    assert build_prompt(window_text="...", start_sec=0.0, end_sec=120.0, instructions="  ") == plain


def test_the_length_range_reaches_the_prompt() -> None:
    prompt = build_prompt(
        window_text="...", start_sec=0.0, end_sec=120.0, min_duration_sec=20, max_duration_sec=45
    )
    assert "20-45 seconds" in prompt


# ── The defaults ─────────────────────────────────────────────────────────────


def test_no_options_and_empty_options_mean_the_same_thing() -> None:
    absent = _brief_of(None, default_limit=5)
    empty = _brief_of(ClipOptions(), default_limit=5)
    assert absent == empty
    assert absent.instructions is None
    assert (absent.limit, absent.min_duration_sec, absent.max_duration_sec) == (5, 15.0, 75.0)


def test_a_blank_instruction_is_no_instruction_and_an_upside_down_range_is_righted() -> None:
    brief = _brief_of(
        ClipOptions(instructions="   ", max_clips=3, min_duration_sec=60, max_duration_sec=30),
        default_limit=5,
    )
    assert brief.instructions is None
    assert brief.limit == 3
    assert (brief.min_duration_sec, brief.max_duration_sec) == (30.0, 60.0)


def test_the_window_widens_only_when_the_longest_clip_would_not_fit() -> None:
    assert _window_spec_for(75.0) is DEFAULT_WINDOW_SPEC
    assert _window_spec_for(90.0) is DEFAULT_WINDOW_SPEC
    wide = _window_spec_for(150.0)
    assert wide.window_sec == 180.0
    assert wide.stride_sec == 45.0


# ── The stage ────────────────────────────────────────────────────────────────


class ScriptedModel:
    """Answers every window with one proposal — or none, when the brief says so."""

    model = "qwen-test"

    def __init__(self, *, answer: bool = True) -> None:
        self._answer = answer
        self.prompts: list[str] = []
        self.stats = _Stats()

    def is_available(self) -> bool:
        return True

    def generate_structured(self, *, schema_model: Any, system: str, prompt: str) -> Any:
        self.prompts.append(prompt)
        if not self._answer:
            return LlmClipResponse(clips=[])
        # One moment near the start of whatever window this is.
        start = float(prompt.split("in the range ")[1].split(" to ")[0])
        return LlmClipResponse(
            clips=[
                LlmClipProposal(
                    start_sec=start + 5.0,
                    end_sec=start + 30.0,
                    sub_scores=SubScores(
                        hook=20, curiosity=15, standalone=15, emotion=10, pacing=8, shareability=8
                    ),
                    hook="a hook",
                    reason="fits",
                )
            ]
        )


class _Stats:
    calls = 0

    def summary(self) -> dict[str, float | int]:
        return {"calls": self.calls, "firstAttemptRate": 1.0}


class FakeArchive:
    def __init__(self, transcript: Transcript) -> None:
        self._transcript = transcript

    def load(self, content_hash: str, model_version: str) -> Transcript:
        return self._transcript

    def load_speech_spans(
        self, content_hash: str, model_version: str
    ) -> tuple[tuple[float, float], ...]:
        return ((0.0, self._transcript.duration_sec or 0.0),)


class FakeCandidateStore:
    def __init__(self) -> None:
        self.written: list[Candidate] = []

    def replace_for_job(self, job_id: str, candidates: list[Candidate]) -> None:
        self.written = list(candidates)


def transcript(seconds: float = 300.0) -> Transcript:
    words = [
        TranscriptWord(text=f"w{i}", start_sec=float(i), end_sec=float(i) + 0.8)
        for i in range(int(seconds))
    ]
    return Transcript(
        source_id="src-1",
        model_version="whisper-test",
        duration_sec=seconds,
        segments=[
            TranscriptSegment(
                index=0,
                text=" ".join(w.text for w in words),
                start_sec=0.0,
                end_sec=seconds,
                words=words,
            )
        ],
        created_at=NOW,
    )


def job(options: ClipOptions | None) -> Job:
    return Job(
        id="job-1",
        uid="user-1",
        type=JobType.CLIP,
        status=JobStatus.RUNNING,
        submission="https://youtu.be/aaaaaaaaaaa",
        clip_options=options,
        stages=[
            Stage(name=StageName.DOWNLOAD, lane=Lane.CPU, status=StageStatus.DONE),
            Stage(
                name=StageName.TRANSCRIBE,
                lane=Lane.GPU,
                status=StageStatus.DONE,
                checkpoint={
                    "sourceId": "src-1",
                    "contentHash": "h",
                    "modelVersion": "whisper-test",
                },
            ),
            Stage(name=StageName.ANALYZE, lane=Lane.GPU, status=StageStatus.PENDING),
            Stage(name=StageName.RENDER, lane=Lane.CPU, status=StageStatus.PENDING),
        ],
        attempts=0,
        max_attempts=3,
        created_at=NOW,
        updated_at=NOW,
    )


def run(options: ClipOptions | None, model: ScriptedModel) -> tuple[Any, FakeCandidateStore]:
    settings = Settings(_env_file=None)
    candidates = FakeCandidateStore()
    stage = AnalyzeStage(
        sources=None,
        candidates=candidates,
        archive=FakeArchive(transcript()),
        client_factory=lambda _ctx: model,
    )
    outcome = stage.run(
        StageContext(
            job=job(options),
            stage_name=StageName.ANALYZE,
            checkpoint=None,
            settings=settings,
            broker=ModelBroker(reserve_mb=settings.vram_reserve_mb),
            should_stop=Event(),
        )
    )
    return outcome, candidates


def test_the_brief_reaches_every_window_and_stamps_the_candidates() -> None:
    model = ScriptedModel()
    outcome, candidates = run(
        ClipOptions(
            instructions="the goals", max_clips=2, min_duration_sec=20, max_duration_sec=45
        ),
        model,
    )
    assert model.prompts and all("asked for: the goals" in p for p in model.prompts)
    assert all("20-45 seconds" in p for p in model.prompts)
    assert len(candidates.written) == 2
    assert {c.prompt_version for c in candidates.written} == {BRIEF_PROMPT_VERSION}
    assert outcome.checkpoint is not None
    assert outcome.checkpoint["promptVersion"] == BRIEF_PROMPT_VERSION
    assert outcome.checkpoint["limit"] == 2
    assert outcome.checkpoint["durationSec"] == [20.0, 45.0]


def test_without_a_brief_the_stage_behaves_as_it_always_has() -> None:
    model = ScriptedModel()
    _, candidates = run(None, model)
    assert not any("asked for" in p for p in model.prompts)
    assert len(candidates.written) == 5
    assert {c.prompt_version for c in candidates.written} == {PROMPT_VERSION}
    assert PROMPT_VERSION != THEMED_PROMPT_VERSION != BRIEF_PROMPT_VERSION


def test_a_brief_that_matches_nothing_is_an_answer_not_a_failure() -> None:
    outcome, candidates = run(ClipOptions(instructions="the goals"), ScriptedModel(answer=False))
    assert candidates.written == []
    assert "no moment matched the brief" in (outcome.detail or "")
