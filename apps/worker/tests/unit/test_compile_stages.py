"""GATHER and SELECT: several sources through the single-source machinery.

The single-source stages are faked here — ingesting and transcribing are their
own tests' business. What is under test is the bookkeeping across several of
them: which items are kept, what a re-run skips, which failures are one item's
and which are the job's, and what SELECT writes down about how each moment was
chosen. ASSEMBLE needs ffmpeg and lives in the integration tier.
"""

# The fakes below stand in for the stores by shape, not by type. Said once here
# rather than on every argument, because the formatter moves a trailing comment
# off the line it was written for.
# mypy: disable-error-code="arg-type"

from __future__ import annotations

from datetime import UTC, datetime
from threading import Event
from typing import Any

import pytest
from clipforge.analysis.ranking import ScoredWindow
from clipforge.config import Settings
from clipforge.models.broker import ModelBroker
from clipforge.stages.base import StageContext, StageOutcome
from clipforge.stages.compile import CompileError, GatherStage, SelectStage
from clipforge.stages.transcribe import SourceNotIngestedError
from clipforge_contracts import (
    Candidate,
    CompileItem,
    CompileOptions,
    Job,
    JobStatus,
    JobType,
    Lane,
    Source,
    Stage,
    StageName,
    StageStatus,
    SubScores,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeDownload:
    """Answers each submission with a source id, or with a classified failure."""

    def __init__(self, answers: dict[str, str | Exception]) -> None:
        self._answers = answers
        self.asked: list[str] = []

    def ingest(self, context: StageContext, submission: str) -> StageOutcome:
        self.asked.append(submission)
        answer = self._answers[submission]
        if isinstance(answer, Exception):
            raise answer
        return StageOutcome(checkpoint={"sourceId": answer}, metadata={"sourceId": answer})


class RateLimitedError(RuntimeError):
    retryable = True
    code = "RATE_LIMITED"


class DeadError(RuntimeError):
    retryable = False
    code = "NOT_FOUND"


class FakeTranscribe:
    def __init__(self, missing: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self._missing = set(missing)
        self.asked: list[str] = []

    def transcribe_source(self, context: StageContext, source_id: str) -> StageOutcome:
        self.asked.append(source_id)
        if source_id in self._missing:
            raise SourceNotIngestedError(f"source {source_id} was garbage-collected")
        return StageOutcome(
            checkpoint={
                "sourceId": source_id,
                "contentHash": f"hash-{source_id}",
                "modelVersion": "whisper-test",
            }
        )


class FakeAnalyze:
    """Proposes one window per call, or nothing, and records the theme it saw."""

    model = "qwen-test"

    def __init__(self, windows: dict[str, ScoredWindow | None]) -> None:
        self._windows = windows
        self.themes: list[str | None] = []
        self.budgets: list[float] = []

    def build_client(self, context: StageContext) -> Any:
        return self

    def propose_windows(
        self,
        context: StageContext,
        client: Any,
        transcript: Transcript,
        spans: tuple[tuple[float, float], ...],
        *,
        limit: int,
        theme: str | None = None,
        min_duration_sec: float = 15.0,
        max_duration_sec: float = 75.0,
    ) -> list[ScoredWindow]:
        self.themes.append(theme)
        self.budgets.append(max_duration_sec)
        window = self._windows.get(transcript.source_id)
        return [window] if window else []


class FakeArchive:
    def __init__(self, transcripts: dict[str, Transcript]) -> None:
        self._by_hash = transcripts

    def load(self, content_hash: str, model_version: str) -> Transcript | None:
        return self._by_hash.get(content_hash)

    def load_speech_spans(
        self, content_hash: str, model_version: str
    ) -> tuple[tuple[float, float], ...]:
        return ((0.0, 60.0),)


class FakeCandidateStore:
    def __init__(self) -> None:
        self.written: list[Candidate] = []

    def replace_for_job(self, job_id: str, candidates: list[Candidate]) -> None:
        self.written = list(candidates)


class FakeSourceStore:
    def __init__(self, sources: dict[str, Source]) -> None:
        self._sources = sources

    def get(self, source_id: str) -> Source | None:
        return self._sources.get(source_id)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def options(**over: Any) -> CompileOptions:
    defaults: dict[str, Any] = {
        "theme": "best goals of the week",
        "items": [
            CompileItem(submission="https://youtu.be/aaaaaaaaaaa"),
            CompileItem(submission="https://youtu.be/bbbbbbbbbbb"),
        ],
    }
    return CompileOptions(**{**defaults, **over})


def job(opts: CompileOptions, *, gather: dict[str, Any] | None = None) -> Job:
    return Job(
        id="job-compile",
        uid="user-1",
        type=JobType.COMPILE,
        status=JobStatus.RUNNING,
        compile_options=opts,
        stages=[
            Stage(
                name=StageName.GATHER,
                lane=Lane.CPU,
                status=StageStatus.DONE if gather else StageStatus.PENDING,
                checkpoint=gather,
            ),
            Stage(name=StageName.SELECT, lane=Lane.GPU, status=StageStatus.PENDING),
            Stage(name=StageName.ASSEMBLE, lane=Lane.CPU, status=StageStatus.PENDING),
        ],
        attempts=0,
        max_attempts=2,
        created_at=NOW,
        updated_at=NOW,
    )


def context(
    the_job: Job, *, stage: StageName, checkpoint: dict[str, Any] | None = None
) -> StageContext:
    settings = Settings(_env_file=None)
    return StageContext(
        job=the_job,
        stage_name=stage,
        checkpoint=checkpoint,
        settings=settings,
        broker=ModelBroker(reserve_mb=settings.vram_reserve_mb),
        should_stop=Event(),
    )


def transcript(source_id: str, *, seconds: float = 60.0) -> Transcript:
    words = [
        TranscriptWord(text=f"w{i}", start_sec=float(i), end_sec=float(i) + 0.8)
        for i in range(int(seconds))
    ]
    return Transcript(
        source_id=source_id,
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


def window(start: float, end: float, total: int = 70) -> ScoredWindow:
    return ScoredWindow(
        start_sec=start,
        end_sec=end,
        sub_scores=SubScores(
            hook=20, curiosity=15, standalone=15, emotion=10, pacing=5, shareability=5
        ),
        hook="the hook",
        reason="the reason",
        total=total,
    )


def gathered(*pairs: tuple[str, str | None]) -> dict[str, Any]:
    return {
        "items": [
            {
                "submission": submission,
                "sourceId": source_id,
                "error": None if source_id else "dead",
            }
            for submission, source_id in pairs
        ]
    }


A = "https://youtu.be/aaaaaaaaaaa"
B = "https://youtu.be/bbbbbbbbbbb"
C = "https://youtu.be/ccccccccccc"


# ── GATHER ───────────────────────────────────────────────────────────────────


def test_gather_ingests_every_item_and_records_which_source_each_became() -> None:
    download = FakeDownload({A: "src-a", B: "src-b"})
    outcome = GatherStage(download=download).run(context(job(options()), stage=StageName.GATHER))
    assert download.asked == [A, B]
    assert outcome.checkpoint == {
        "items": [
            {"submission": A, "sourceId": "src-a", "error": None},
            {"submission": B, "sourceId": "src-b", "error": None},
        ]
    }
    assert outcome.detail == "fetched 2 of 2 videos"


def test_gather_skips_a_dead_link_and_keeps_the_rest() -> None:
    download = FakeDownload({A: "src-a", B: DeadError("This video does not exist"), C: "src-c"})
    opts = options(items=[CompileItem(submission=s) for s in (A, B, C)])
    outcome = GatherStage(download=download).run(context(job(opts), stage=StageName.GATHER))
    items = (outcome.checkpoint or {})["items"]
    assert items[1]["sourceId"] is None
    assert items[1]["error"] == "This video does not exist"
    assert [i["sourceId"] for i in items] == ["src-a", None, "src-c"]
    assert "1 could not be" in (outcome.detail or "")


def test_gather_fails_the_job_when_fewer_than_two_survive() -> None:
    download = FakeDownload({A: "src-a", B: DeadError("gone")})
    with pytest.raises(CompileError) as caught:
        GatherStage(download=download).run(context(job(options()), stage=StageName.GATHER))
    assert caught.value.retryable is False
    assert caught.value.code == "COMPILE_TOO_FEW"
    assert "gone" in str(caught.value)


def test_gather_lets_a_rate_limit_retry_the_stage() -> None:
    download = FakeDownload({A: "src-a", B: RateLimitedError("429")})
    with pytest.raises(RateLimitedError):
        GatherStage(download=download).run(context(job(options()), stage=StageName.GATHER))


def test_gather_resumes_without_refetching_what_already_landed() -> None:
    download = FakeDownload({A: "src-a", B: "src-b"})
    resumed = {
        "items": [
            {"submission": A, "sourceId": "src-a", "error": None},
            {"submission": B, "sourceId": None, "error": None},
        ]
    }
    GatherStage(download=download).run(
        context(job(options()), stage=StageName.GATHER, checkpoint=resumed)
    )
    assert download.asked == [B]


def test_gather_refuses_a_job_with_no_options() -> None:
    the_job = job(options()).model_copy(update={"compile_options": None})
    with pytest.raises(CompileError, match="no compileOptions"):
        GatherStage(download=FakeDownload({})).run(context(the_job, stage=StageName.GATHER))


# ── SELECT ───────────────────────────────────────────────────────────────────


def select_stage(
    *,
    analyze: FakeAnalyze,
    transcribe: FakeTranscribe | None = None,
    archive: FakeArchive | None = None,
    sources: FakeSourceStore | None = None,
) -> tuple[SelectStage, FakeCandidateStore]:
    candidates = FakeCandidateStore()
    stage = SelectStage(
        transcribe=transcribe or FakeTranscribe(),
        analyze=analyze,
        archive=archive
        or FakeArchive({"hash-src-a": transcript("src-a"), "hash-src-b": transcript("src-b")}),
        candidates=candidates,
        sources=sources or FakeSourceStore({}),
    )
    return stage, candidates


def test_select_transcribes_each_source_then_asks_the_model_with_the_theme() -> None:
    analyze = FakeAnalyze({"src-a": window(10.0, 25.0), "src-b": window(40.0, 55.0, total=60)})
    transcribe = FakeTranscribe()
    stage, candidates = select_stage(analyze=analyze, transcribe=transcribe)

    outcome = stage.run(
        context(job(options(), gather=gathered((A, "src-a"), (B, "src-b"))), stage=StageName.SELECT)
    )

    assert transcribe.asked == ["src-a", "src-b"]
    assert analyze.themes == ["best goals of the week", "best goals of the week"]
    # Two items into a 60-second target is 30 each, capped by the 20-second segment ceiling.
    assert analyze.budgets == [20.0, 20.0]
    segments = (outcome.checkpoint or {})["segments"]
    assert segments["0"]["startSec"] == 10.0 and segments["0"]["chosenBy"] == "model"
    assert segments["0"]["hook"] == "the hook"
    assert [c.source_id for c in candidates.written] == ["src-a", "src-b"]
    assert candidates.written[0].id == segments["0"]["candidateId"]
    assert candidates.written[0].prompt_version == "v1-theme"
    assert candidates.written[0].total == 70
    assert "2 moments chosen, 2 by the model" in (outcome.detail or "")


def test_a_window_the_operator_gave_skips_the_model_and_is_trimmed_to_budget() -> None:
    analyze = FakeAnalyze({"src-b": window(40.0, 55.0)})
    opts = options(
        items=[CompileItem(submission=A, start_sec=100.0, end_sec=150.0), CompileItem(submission=B)]
    )
    stage, candidates = select_stage(analyze=analyze)

    outcome = stage.run(
        context(job(opts, gather=gathered((A, "src-a"), (B, "src-b"))), stage=StageName.SELECT)
    )

    segments = (outcome.checkpoint or {})["segments"]
    assert segments["0"] == {
        **segments["0"],
        "startSec": 100.0,
        "endSec": 120.0,
        "chosenBy": "operator",
        "trimmed": True,
    }
    assert analyze.themes == ["best goals of the week"]  # asked once, for B only
    given = candidates.written[0]
    assert given.total == 0
    assert given.reason == "the window the operator gave"
    assert given.prompt_version is None


def test_when_nothing_stands_out_the_middle_is_used_and_labelled_as_such() -> None:
    analyze = FakeAnalyze({"src-a": window(10.0, 25.0), "src-b": None})
    stage, candidates = select_stage(analyze=analyze)
    outcome = stage.run(
        context(job(options(), gather=gathered((A, "src-a"), (B, "src-b"))), stage=StageName.SELECT)
    )
    segment = (outcome.checkpoint or {})["segments"]["1"]
    assert segment["chosenBy"] == "fallback"
    assert segment["startSec"] == 20.0 and segment["endSec"] == 40.0
    assert "middle of the video" in candidates.written[1].reason  # type: ignore[operator]


def test_a_collected_source_is_skipped_and_the_rest_carry_on() -> None:
    analyze = FakeAnalyze({"src-a": window(10.0, 25.0), "src-c": window(1.0, 12.0)})
    opts = options(items=[CompileItem(submission=s) for s in (A, B, C)])
    archive = FakeArchive({"hash-src-a": transcript("src-a"), "hash-src-c": transcript("src-c")})
    stage, candidates = select_stage(
        analyze=analyze, transcribe=FakeTranscribe(missing={"src-b"}), archive=archive
    )

    outcome = stage.run(
        context(
            job(opts, gather=gathered((A, "src-a"), (B, "src-b"), (C, "src-c"))),
            stage=StageName.SELECT,
        )
    )

    segments = (outcome.checkpoint or {})["segments"]
    assert "garbage-collected" in segments["1"]["error"]
    assert [c.source_id for c in candidates.written] == ["src-a", "src-c"]
    # Three survivors' worth of budget: 60 / 3 = 20, still under the cap.
    assert analyze.budgets == [20.0, 20.0]


def test_select_fails_when_fewer_than_two_moments_remain() -> None:
    analyze = FakeAnalyze({"src-a": window(10.0, 25.0)})
    stage, _ = select_stage(analyze=analyze, transcribe=FakeTranscribe(missing={"src-b"}))
    with pytest.raises(CompileError) as caught:
        stage.run(
            context(
                job(options(), gather=gathered((A, "src-a"), (B, "src-b"))), stage=StageName.SELECT
            )
        )
    assert caught.value.code == "COMPILE_TOO_FEW"


def test_select_resumes_past_moments_already_chosen() -> None:
    analyze = FakeAnalyze({"src-b": window(40.0, 55.0)})
    transcribe = FakeTranscribe()
    stage, candidates = select_stage(analyze=analyze, transcribe=transcribe)
    resumed = {
        "segments": {
            "0": {
                "submission": A,
                "sourceId": "src-a",
                "contentHash": "hash-src-a",
                "modelVersion": "whisper-test",
                "startSec": 1.0,
                "endSec": 9.0,
                "chosenBy": "model",
                "candidateId": "kept",
            },
        }
    }
    stage.run(
        context(
            job(options(), gather=gathered((A, "src-a"), (B, "src-b"))),
            stage=StageName.SELECT,
            checkpoint=resumed,
        )
    )
    assert transcribe.asked == ["src-b"]
    assert analyze.themes == ["best goals of the week"]
    assert candidates.written[0].id == "kept"
