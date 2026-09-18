"""The RESEARCH and CURATE stages: what they write, and what they tolerate.

Fake providers, a fake store and a scripted model. What is under test is the
orchestration — which rows get written, in what order, with what attached, and
which failures are the run's problem versus one provider's.
"""

# The fakes below stand in for the stores by shape, not by type. Said once here
# rather than on every argument, because the formatter moves a trailing comment
# off the line it was written for.
# mypy: disable-error-code="arg-type"

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Event
from typing import Any

import pytest
from clipforge.config import Settings
from clipforge.models.broker import ModelBroker
from clipforge.models.ollama import OllamaError
from clipforge.research.signals import ResearchRequest, Signal, VideoHit
from clipforge.stages.base import StageContext
from clipforge.stages.research import CurateStage, ResearchError, ResearchStage
from clipforge_contracts import (
    Job,
    JobStatus,
    JobType,
    Lane,
    LlmTrendVerdict,
    ResearchOptions,
    Stage,
    StageName,
    StageStatus,
    Trend,
    TrendSignal,
    TrendSource,
    TrendStatus,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeTrendStore:
    def __init__(self, rows: list[Trend] | None = None) -> None:
        self.rows: dict[str, list[Trend]] = {}
        if rows:
            self.rows[rows[0].job_id] = list(rows)
        self.annotations: list[dict[str, Any]] = []
        self.reranks: list[list[tuple[str, int]]] = []

    def for_job(self, job_id: str) -> list[Trend]:
        return sorted(self.rows.get(job_id, []), key=lambda t: t.rank)

    def replace_for_job(self, job_id: str, trends: list[Trend]) -> None:
        self.rows[job_id] = list(trends)

    def annotate(self, trend_id: str, **fields: Any) -> None:
        self.annotations.append({"id": trend_id, **fields})
        for rows in self.rows.values():
            for index, trend in enumerate(rows):
                if trend.id == trend_id:
                    rows[index] = trend.model_copy(
                        update={
                            "angle": fields["angle"],
                            "relevance": fields["relevance"],
                            "worth_clipping": fields["worth_clipping"],
                            "compilation_title": fields["compilation_title"],
                            "curated": True,
                        }
                    )

    def rerank(self, ranks: list[tuple[str, int]]) -> None:
        self.reranks.append(list(ranks))
        for rows in self.rows.values():
            for index, trend in enumerate(rows):
                for trend_id, rank in ranks:
                    if trend.id == trend_id:
                        rows[index] = trend.model_copy(update={"rank": rank})


class FakeProvider:
    def __init__(
        self, source: TrendSource, signals: list[Signal] | None = None, *, fails: str = ""
    ) -> None:
        self.source = source
        self._signals = signals or []
        self._fails = fails
        self.requests: list[ResearchRequest] = []

    def fetch(self, request: ResearchRequest) -> list[Signal]:
        self.requests.append(request)
        if self._fails:
            raise RuntimeError(self._fails)
        return list(self._signals)


class FakeFinder:
    def __init__(self, hits: dict[str, list[VideoHit]]) -> None:
        self._hits = hits
        self.asked: list[str] = []
        self.enriched: list[str] = []

    def find_videos(self, topic: str, *, limit: int, request: ResearchRequest) -> list[VideoHit]:
        self.asked.append(topic)
        return self._hits.get(topic, [])[:limit]

    def enrich(self, hit: VideoHit) -> VideoHit:
        self.enriched.append(hit.external_id)
        return VideoHit(
            url=hit.url,
            external_id=hit.external_id,
            title=hit.title,
            via=hit.via,
            view_count=12_000,
            uploaded_at=NOW - timedelta(hours=12),
            duration_sec=240.0,
        )


class ScriptedModel:
    """Answers every trend the same way, and can be made unreachable."""

    model = "qwen-test"

    def __init__(self, *, available: bool = True, fail_on: str | None = None) -> None:
        self._available = available
        self._fail_on = fail_on
        self.prompts: list[str] = []
        self.stats = _Stats()

    def is_available(self) -> bool:
        return self._available

    def generate_structured(self, *, schema_model: Any, system: str, prompt: str) -> Any:
        self.prompts.append(prompt)
        if self._fail_on and self._fail_on in prompt:
            raise OllamaError("twice invalid", retryable=False)
        relevance = 9 if "premier" in prompt.lower() else 2
        return LlmTrendVerdict(
            angle="What a clip would show.",
            relevance=relevance,
            worth_clipping="dull topic" not in prompt,
            compilation_title="A title",
        )


class _Stats:
    def summary(self) -> dict[str, float | int]:
        return {"calls": 0}


# ── Fixtures ─────────────────────────────────────────────────────────────────


def job(
    options: ResearchOptions | None = None, *, checkpoints: dict[str, dict[str, Any]] | None = None
) -> Job:
    checkpoints = checkpoints or {}
    return Job(
        id="job-research",
        uid="user-1",
        type=JobType.RESEARCH,
        status=JobStatus.RUNNING,
        research_options=options,
        stages=[
            Stage(
                name=StageName.RESEARCH,
                lane=Lane.CPU,
                status=StageStatus.DONE if "RESEARCH" in checkpoints else StageStatus.PENDING,
                checkpoint=checkpoints.get("RESEARCH"),
            ),
            Stage(name=StageName.CURATE, lane=Lane.GPU, status=StageStatus.PENDING),
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


def sig(topic: str, source: TrendSource, strength: float = 0.5, **over: Any) -> Signal:
    return Signal(
        source=source,
        topic=topic,
        strength=strength,
        observed_at=NOW - timedelta(hours=1),
        detail=f"{source.value} says so",
        **over,
    )


def hit(video_id: str, via: TrendSource = TrendSource.YOUTUBE, *, blank: bool = False) -> VideoHit:
    return VideoHit(
        url=f"https://www.youtube.com/watch?v={video_id}",
        external_id=video_id,
        title=f"Video {video_id}",
        via=via,
        view_count=None if blank else 50_000,
        uploaded_at=None if blank else NOW - timedelta(hours=6),
        duration_sec=None if blank else 400.0,
    )


def trend(trend_id: str, topic: str, rank: int, *, score: int = 50) -> Trend:
    return Trend(
        id=trend_id,
        uid="user-1",
        job_id="job-research",
        topic=topic,
        rank=rank,
        score=score,
        signals=[TrendSignal(source=TrendSource.GOOGLE_TRENDS, strength=0.5, detail="d")],
        videos=[],
        status=TrendStatus.NEW,
        created_at=NOW,
    )


# ── RESEARCH ─────────────────────────────────────────────────────────────────


def test_research_writes_a_ranked_list_with_the_videos_it_found() -> None:
    store = FakeTrendStore()
    google = FakeProvider(
        TrendSource.GOOGLE_TRENDS,
        [
            sig("brewers vs orioles", TrendSource.GOOGLE_TRENDS, 0.6),
            sig("arsenal", TrendSource.GOOGLE_TRENDS, 0.3),
        ],
    )
    reddit = FakeProvider(
        TrendSource.REDDIT,
        [
            sig(
                "Brewers beat the Orioles late",
                TrendSource.REDDIT,
                0.8,
                videos=(hit("rrrrrrrrrrr", TrendSource.REDDIT, blank=True),),
            )
        ],
    )
    finder = FakeFinder({"arsenal": [hit("aaaaaaaaaaa")]})
    stage = ResearchStage(
        trends=store, providers=[google, reddit], finder=finder, clock=lambda: NOW
    )

    outcome = stage.run(context(job(ResearchOptions(max_trends=5)), stage=StageName.RESEARCH))

    rows = store.for_job("job-research")
    assert [row.rank for row in rows] == [1, 2]
    assert rows[0].topic == "brewers vs orioles"
    assert {s.source for s in rows[0].signals} == {TrendSource.GOOGLE_TRENDS, TrendSource.REDDIT}
    assert [v.external_id for v in rows[0].videos] == ["rrrrrrrrrrr"]
    # The Reddit link arrived knowing nothing; the finder looked it up, and the
    # row still says Reddit surfaced it.
    assert finder.enriched == ["rrrrrrrrrrr"]
    assert rows[0].videos[0].views_per_hour == pytest.approx(12_000 / 12, rel=0.01)
    assert rows[0].videos[0].via is TrendSource.REDDIT
    # Arsenal arrived with no video, so the finder was asked — and only for it.
    assert finder.asked == ["arsenal"]
    assert [v.external_id for v in rows[1].videos] == ["aaaaaaaaaaa"]
    assert rows[1].status is TrendStatus.NEW
    assert outcome.checkpoint is not None
    assert outcome.checkpoint["trendIds"] == [row.id for row in rows]
    assert outcome.checkpoint["providers"] == {"GOOGLE_TRENDS": 2, "REDDIT": 1}
    assert outcome.checkpoint["failures"] == {}


def test_research_records_a_provider_that_failed_and_carries_on() -> None:
    store = FakeTrendStore()
    google = FakeProvider(TrendSource.GOOGLE_TRENDS, [sig("thing", TrendSource.GOOGLE_TRENDS)])
    reddit = FakeProvider(TrendSource.REDDIT, fails="answered 403")
    stage = ResearchStage(trends=store, providers=[google, reddit], finder=None, clock=lambda: NOW)

    outcome = stage.run(context(job(), stage=StageName.RESEARCH))

    assert len(store.for_job("job-research")) == 1
    assert outcome.checkpoint is not None
    assert outcome.checkpoint["failures"] == {"REDDIT": "answered 403"}
    assert "Reddit did not answer" in (outcome.detail or "")


def test_research_fails_retryably_when_every_provider_failed() -> None:
    stage = ResearchStage(
        trends=FakeTrendStore(),
        providers=[FakeProvider(TrendSource.REDDIT, fails="down")],
        finder=None,
        clock=lambda: NOW,
    )
    with pytest.raises(ResearchError) as caught:
        stage.run(context(job(), stage=StageName.RESEARCH))
    assert caught.value.retryable is True
    assert caught.value.code == "RESEARCH_UNAVAILABLE"


def test_research_refuses_a_run_that_asks_for_no_enabled_source() -> None:
    stage = ResearchStage(
        trends=FakeTrendStore(),
        providers=[FakeProvider(TrendSource.REDDIT)],
        finder=None,
        clock=lambda: NOW,
    )
    with pytest.raises(ResearchError) as caught:
        stage.run(
            context(
                job(ResearchOptions(sources=[TrendSource.GOOGLE_TRENDS])), stage=StageName.RESEARCH
            )
        )
    assert caught.value.retryable is False


def test_research_asks_only_the_sources_the_run_named_and_passes_its_topics() -> None:
    store = FakeTrendStore()
    google = FakeProvider(TrendSource.GOOGLE_TRENDS, [sig("x", TrendSource.GOOGLE_TRENDS)])
    youtube = FakeProvider(TrendSource.YOUTUBE, [sig("premier league", TrendSource.YOUTUBE)])
    stage = ResearchStage(trends=store, providers=[google, youtube], finder=None, clock=lambda: NOW)

    stage.run(
        context(
            job(
                ResearchOptions(
                    topics=["premier league"],
                    sources=[TrendSource.YOUTUBE],
                    region="GB",
                    lookback_hours=24,
                )
            ),
            stage=StageName.RESEARCH,
        )
    )

    assert google.requests == []
    assert youtube.requests[0].topics == ("premier league",)
    assert youtube.requests[0].region == "GB"
    assert youtube.requests[0].lookback_hours == 24
    rows = store.for_job("job-research")
    assert [str(m.root) for m in rows[0].matched_topics or []] == ["premier league"]


def test_an_empty_window_is_a_result_not_a_failure() -> None:
    store = FakeTrendStore()
    stage = ResearchStage(
        trends=store,
        providers=[FakeProvider(TrendSource.REDDIT, [])],
        finder=None,
        clock=lambda: NOW,
    )
    outcome = stage.run(context(job(), stage=StageName.RESEARCH))
    assert store.for_job("job-research") == []
    assert outcome.checkpoint == {
        "trendIds": [],
        "signals": 0,
        "providers": {"REDDIT": 0},
        "failures": {},
    }


def test_a_stop_request_leaves_the_stage_pending() -> None:
    store = FakeTrendStore()
    stage = ResearchStage(
        trends=store,
        providers=[FakeProvider(TrendSource.REDDIT, [sig("x", TrendSource.REDDIT)])],
        finder=None,
        clock=lambda: NOW,
    )
    ctx = context(job(), stage=StageName.RESEARCH)
    ctx.should_stop.set()
    outcome = stage.run(ctx)
    assert outcome.incomplete is True
    assert store.rows == {}


# ── CURATE ───────────────────────────────────────────────────────────────────


def test_curate_annotates_every_row_and_reorders_by_the_blend() -> None:
    rows = [trend("t1", "dull topic", 1, score=70), trend("t2", "premier league", 2, score=60)]
    store = FakeTrendStore(rows)
    model = ScriptedModel()
    stage = CurateStage(trends=store, client_factory=lambda _ctx: model)

    outcome = stage.run(
        context(job(ResearchOptions(topics=["premier league"])), stage=StageName.CURATE)
    )

    assert {a["id"] for a in store.annotations} == {"t1", "t2"}
    by_id = {a["id"]: a for a in store.annotations}
    assert by_id["t2"]["relevance"] == 9
    assert by_id["t1"]["worth_clipping"] is False
    assert by_id["t2"]["compilation_title"] == "A title"
    # The premier league row is relevant and worth clipping; the other is neither.
    assert [row.topic for row in store.for_job("job-research")] == [
        "premier league",
        "dull topic",
    ]
    assert outcome.checkpoint is not None
    assert outcome.checkpoint["curated"] == ["t1", "t2"]
    assert outcome.checkpoint["promptVersion"] == "curate-v1"
    assert "2 of 2" in (outcome.detail or "")


def test_curate_records_no_relevance_when_the_run_named_no_interests() -> None:
    store = FakeTrendStore([trend("t1", "anything", 1)])
    stage = CurateStage(trends=store, client_factory=lambda _ctx: ScriptedModel())
    stage.run(context(job(ResearchOptions()), stage=StageName.CURATE))
    assert store.annotations[0]["relevance"] is None
    assert store.annotations[0]["angle"] == "What a clip would show."


def test_curate_is_skipped_when_the_run_turned_it_off() -> None:
    store = FakeTrendStore([trend("t1", "x", 1)])
    stage = CurateStage(trends=store, client_factory=lambda _ctx: ScriptedModel())
    outcome = stage.run(context(job(ResearchOptions(curate=False)), stage=StageName.CURATE))
    assert outcome.skipped is True
    assert store.annotations == []


def test_curate_degrades_to_nothing_without_a_model() -> None:
    store = FakeTrendStore([trend("t1", "x", 1)])
    stage = CurateStage(trends=store, client_factory=lambda _ctx: ScriptedModel(available=False))
    outcome = stage.run(context(job(), stage=StageName.CURATE))
    assert outcome.skipped is True
    assert "not reachable" in (outcome.detail or "")
    assert store.annotations == []


def test_curate_resumes_past_rows_it_already_explained() -> None:
    store = FakeTrendStore([trend("t1", "one", 1), trend("t2", "two", 2)])
    model = ScriptedModel()
    stage = CurateStage(trends=store, client_factory=lambda _ctx: model)
    stage.run(context(job(), stage=StageName.CURATE, checkpoint={"curated": ["t1"]}))
    assert [a["id"] for a in store.annotations] == ["t2"]
    assert len(model.prompts) == 1


def test_curate_keeps_going_when_the_model_refuses_one_row() -> None:
    store = FakeTrendStore([trend("t1", "bad row", 1), trend("t2", "good row", 2)])
    stage = CurateStage(trends=store, client_factory=lambda _ctx: ScriptedModel(fail_on="bad row"))
    outcome = stage.run(context(job(), stage=StageName.CURATE))
    assert [a["id"] for a in store.annotations] == ["t2"]
    assert outcome.checkpoint is not None
    assert list(outcome.checkpoint["failed"]) == ["t1"]
    assert "1 refused" in (outcome.detail or "")
