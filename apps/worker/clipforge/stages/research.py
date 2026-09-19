"""The RESEARCH job: what is the web talking about, and which videos carry it.

Two stages, and the split between them is the point.

**RESEARCH** runs on the CPU lane and asks the providers. It is network-bound,
touches no media and no model, and ends with a ranked list written to
``trends/``. Everything it decides is arithmetic in :mod:`clipforge.research.scoring`,
so the ranking is the same for the same feeds and can be explained by reading
the components off the row.

**CURATE** runs on the GPU lane and puts each row to the local model for the
one thing arithmetic cannot supply: a sentence saying what a clip about this
would actually be, and a judgement of whether it belongs on this channel. It is
optional per run and it degrades to nothing — the list is still ranked, just
not explained — when the model is not there.

Nothing here runs without a person creating the job. docs/PLAN.md Phase 10 is
explicit that automating an uncalibrated scorer produces bad clips faster, and
this is built to make the next step a tap rather than to take it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from clipforge_contracts import (
    Category,
    Lane,
    LlmTrendVerdict,
    ResearchOptions,
    StageName,
    Trend,
    TrendSignal,
    TrendSource,
    TrendStatus,
    TrendVideo,
    category_by_code,
)

from clipforge.models.ollama import OllamaClient, OllamaError
from clipforge.observability import get_logger
from clipforge.research.curate import CURATE_PROMPT_VERSION, curate_trend
from clipforge.research.scoring import (
    Cluster,
    ScoredTrend,
    blend_rank,
    cluster_signals,
    lookup_query,
    rank_trends,
    score_trend,
)
from clipforge.research.signals import (
    ResearchRequest,
    Signal,
    TrendProvider,
    VideoFinder,
    VideoHit,
)
from clipforge.stages.base import StageContext, StageOutcome
from clipforge.store.firestore import TrendStore

log = get_logger(__name__)

__all__ = ["CurateStage", "ResearchError", "ResearchStage", "plain_strings", "resolve_category"]

# The same lease ANALYZE takes: one small call per row on the same model.
_CURATE_VRAM_MB = 3600

_LABELS = {
    TrendSource.GOOGLE_TRENDS: "Google Trends",
    TrendSource.REDDIT: "Reddit",
    TrendSource.YOUTUBE: "YouTube",
}


class ResearchError(RuntimeError):
    """Nothing could be learned. ``retryable`` and ``code`` are read by the runner."""

    def __init__(self, message: str, *, retryable: bool, code: str) -> None:
        self.retryable = retryable
        self.code = code
        super().__init__(message)


def plain_strings(items: Sequence[Any] | None) -> list[str]:
    """Constrained-string lists arrive as RootModel wrappers; the code wants words."""
    return [str(getattr(item, "root", item)).strip() for item in (items or []) if item is not None]


def resolve_category(options: ResearchOptions) -> Category | None:
    """The catalogue entry a run named, or None.

    A code the catalogue does not know is ignored with a warning rather than
    refused: the client and the worker are deployed separately, and a newer
    page offering a category an older worker has not heard of should still get
    its list — just not steered.
    """
    category = category_by_code(options.category)
    if options.category and category is None:
        log.warning("research.unknown_category", category=options.category)
    return category


class ResearchStage:
    """Ask every configured provider, cluster what they said, rank it, write it."""

    name = StageName.RESEARCH
    lane = Lane.CPU

    def __init__(
        self,
        *,
        trends: TrendStore,
        providers: Sequence[TrendProvider],
        finder: VideoFinder | None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._trends = trends
        self._providers = list(providers)
        self._finder = finder
        self._clock = clock or (lambda: datetime.now(UTC))

    def run(self, context: StageContext) -> StageOutcome:
        options = context.job.research_options or ResearchOptions()
        now = self._clock()
        interests = plain_strings(options.topics)
        # The category fills in what the run left blank and never overrides
        # what it said: its own topics are searched and scored as before, and
        # its own subreddits are read. Only the lookup hint applies regardless,
        # because a feed phrase is nobody's words.
        category = resolve_category(options)
        request = ResearchRequest(
            topics=tuple(interests) or (category.terms if category else ()),
            region=options.region or context.settings.research_region,
            lookback_hours=options.lookback_hours or 48,
            videos_per_topic=options.videos_per_topic or 5,
            subreddits=tuple(plain_strings(options.subreddits))
            or (category.subreddits if category else ()),
            now=now,
            category=category.code if category else None,
            category_hint=category.hint if category else None,
        )
        max_trends = options.max_trends or 12

        wanted = set(options.sources) if options.sources else None
        chosen = [p for p in self._providers if wanted is None or p.source in wanted]
        if not chosen:
            raise ResearchError(
                "no trend provider is enabled for this run; turn one on in the worker's "
                "settings or ask for a different set of sources",
                retryable=False,
                code="RESEARCH_NO_PROVIDERS",
            )

        signals, counts, failures = self._gather(context, chosen, request)
        if context.stopping():
            return StageOutcome(incomplete=True)
        if not signals:
            if failures:
                raise ResearchError(
                    "every trend provider failed: "
                    + "; ".join(f"{name}: {why}" for name, why in failures.items()),
                    retryable=True,
                    code="RESEARCH_UNAVAILABLE",
                )
            self._trends.replace_for_job(context.job.id, [])
            return StageOutcome(
                checkpoint={
                    "trendIds": [],
                    "signals": 0,
                    "providers": counts,
                    "failures": {},
                    "region": request.region,
                    "category": request.category,
                },
                detail="nothing is moving in this window",
            )

        max_duration = context.settings.max_source_duration_sec
        clusters = cluster_signals(signals)

        def scored() -> list[ScoredTrend]:
            return [
                score_trend(
                    cluster,
                    now=now,
                    lookback_hours=request.lookback_hours,
                    interests=interests,
                    max_duration_sec=max_duration,
                )
                for cluster in clusters
            ]

        # Videos are looked up for the rows most likely to make the list, not
        # for every cluster: a busy feed day is sixty clusters and each lookup
        # is a handful of requests. A row that already has videos — a Reddit
        # link — gets those looked up instead, because a link carries no view
        # count and an unknown scores as neutral, which put every shared video
        # below every searched one on the first real run.
        if self._finder is not None:
            shortlist = rank_trends(scored(), limit=max_trends * 2)
            for scored_trend in shortlist:
                if context.stopping():
                    return StageOutcome(incomplete=True)
                cluster = scored_trend.cluster
                if cluster.videos:
                    self._enrich(context, cluster, limit=request.videos_per_topic)
                    continue
                context.progress(f"Looking for videos about {cluster.label[:60]}")
                try:
                    cluster.found.extend(
                        self._finder.find_videos(
                            lookup_query(cluster.label, request.category_hint),
                            limit=request.videos_per_topic,
                            request=request,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - one lookup must not fail the run
                    log.warning(
                        "research.videos_not_found", topic=cluster.label, error=str(exc)[:200]
                    )

        ranked = rank_trends(scored(), limit=max_trends)
        documents = [
            _document(scored_trend, rank=index, job=context, now=now)
            for index, scored_trend in enumerate(ranked, start=1)
        ]
        self._trends.replace_for_job(context.job.id, documents)

        log.info(
            "research.done",
            trends=len(documents),
            signals=len(signals),
            clusters=len(clusters),
            failures=list(failures),
        )
        detail = f"{len(documents)} trends from {len(signals)} signals"
        if failures:
            detail += f"; {', '.join(_LABELS[TrendSource(n)] for n in failures)} did not answer"
        return StageOutcome(
            checkpoint={
                "trendIds": [document.id for document in documents],
                "signals": len(signals),
                "clusters": len(clusters),
                "providers": counts,
                "failures": failures,
                # What the run resolved to, so a list can be read back against
                # the question actually asked — the region defaulted here, and
                # a category the worker did not know shows up as null.
                "region": request.region,
                "category": request.category,
            },
            detail=detail,
        )

    def _enrich(self, context: StageContext, cluster: Cluster, *, limit: int) -> None:
        """Look up the videos a sighting left blank, a bounded number per row."""
        if self._finder is None:
            return
        wanting = [
            hit for hit in cluster.videos if hit.view_count is None or hit.uploaded_at is None
        ][:limit]
        if not wanting:
            return
        context.progress(f"Looking up {len(wanting)} video(s) about {cluster.label[:50]}")
        for hit in wanting:
            try:
                # Appended rather than replaced: `Cluster.videos` merges what
                # every sighting knew, and prefers a known value to a blank.
                cluster.found.append(self._finder.enrich(hit))
            except Exception as exc:  # noqa: BLE001 - a blank view count is not worth the run
                log.debug("research.enrich_failed", video=hit.external_id, error=str(exc)[:120])

    def _gather(
        self,
        context: StageContext,
        providers: Sequence[TrendProvider],
        request: ResearchRequest,
    ) -> tuple[list[Signal], dict[str, int], dict[str, str]]:
        """Ask each provider once. A provider that fails is recorded, not fatal."""
        signals: list[Signal] = []
        counts: dict[str, int] = {}
        failures: dict[str, str] = {}
        for provider in providers:
            if context.stopping():
                break
            context.progress(f"Asking {_LABELS[provider.source]}")
            try:
                found = provider.fetch(request)
            except Exception as exc:  # noqa: BLE001 - somebody else's server
                failures[provider.source.value] = str(exc)[:200]
                log.warning(
                    "research.provider_failed", provider=provider.source.value, error=str(exc)[:200]
                )
                continue
            counts[provider.source.value] = len(found)
            signals.extend(found)
        return signals, counts, failures


def _document(scored: ScoredTrend, *, rank: int, job: StageContext, now: datetime) -> Trend:
    cluster = scored.cluster
    return Trend(
        id=uuid.uuid4().hex,
        uid=job.job.uid,
        job_id=job.job.id,
        topic=cluster.label[:200],
        rank=rank,
        score=scored.score,
        signals=[
            TrendSignal(
                source=signal.source,
                strength=round(signal.strength, 3),
                detail=(signal.detail or "")[:200] or None,
                url=signal.url,
            )
            for signal in cluster.signals[:12]
        ],
        videos=[_video(hit, score, now) for hit, score in scored.videos[:20]],
        matched_topics=list(scored.matched)[:12],
        status=TrendStatus.NEW,
        created_at=now,
    )


def _video(hit: VideoHit, score: int, now: datetime) -> TrendVideo:
    velocity: float | None = None
    if hit.view_count is not None and hit.uploaded_at is not None:
        hours = max((now - hit.uploaded_at).total_seconds() / 3600.0, 1.0)
        velocity = round(hit.view_count / hours, 1)
    return TrendVideo(
        url=hit.url,
        external_id=hit.external_id,
        title=hit.title[:300],
        channel=hit.channel,
        duration_sec=hit.duration_sec,
        view_count=hit.view_count,
        uploaded_at=hit.uploaded_at,
        thumbnail_url=hit.thumbnail_url,
        views_per_hour=velocity,
        score=score,
        via=hit.via,
    )


class CurateStage:
    """Explain each row, and fold the model's judgement into the order."""

    name = StageName.CURATE
    lane = Lane.GPU

    def __init__(self, *, trends: TrendStore, client_factory: object = None) -> None:
        self._trends = trends
        self._client_factory = client_factory

    def run(self, context: StageContext) -> StageOutcome:
        options = context.job.research_options or ResearchOptions()
        if options.curate is False:
            return StageOutcome(skipped=True, detail="curation was turned off for this run")

        rows = self._trends.for_job(context.job.id)
        if not rows:
            return StageOutcome(skipped=True, detail="nothing to curate")

        client = self._build_client(context)
        if not client.is_available():
            # Not a failure. The list is complete and ranked; this stage adds a
            # sentence per row, and a machine with no model still gets the list.
            log.info("curate.skipped", reason="ollama unavailable")
            return StageOutcome(
                skipped=True,
                detail="the local model is not reachable, so the list is ranked but not explained",
            )

        interests = plain_strings(options.topics)
        category = resolve_category(options)
        checkpoint = context.checkpoint or {}
        done: set[str] = set(checkpoint.get("curated") or [])
        failed: dict[str, str] = dict(checkpoint.get("failed") or {})

        context.progress("Waiting for the GPU")
        with context.broker.acquire(f"ollama:{client.model}", _CURATE_VRAM_MB) as leased:
            for index, trend in enumerate(rows, start=1):
                if trend.id in done or trend.id in failed:
                    continue
                if context.stopping():
                    return StageOutcome(
                        incomplete=True,
                        checkpoint={"curated": sorted(done), "failed": failed},
                    )
                context.progress(f"Judging trend {index} of {len(rows)}: {trend.topic[:50]}")
                try:
                    verdict = curate_trend(client, trend, interests=interests, category=category)
                except OllamaError as exc:
                    if exc.retryable:
                        raise _as_stage_failure(exc) from exc
                    failed[trend.id] = str(exc)[:200]
                    continue
                self._trends.annotate(
                    trend.id,
                    angle=verdict.angle.strip()[:300] or None,
                    # Relevance to nothing is a number the model made up.
                    relevance=verdict.relevance if (interests or category) else None,
                    worth_clipping=verdict.worth_clipping,
                    compilation_title=verdict.compilation_title.strip()[:120] or None,
                )
                done.add(trend.id)
            peak = leased.observe()

        self._rerank(context.job.id)
        stats = client.stats.summary()
        return StageOutcome(
            checkpoint={
                "curated": sorted(done),
                "failed": failed,
                "promptVersion": CURATE_PROMPT_VERSION,
                "modelVersion": f"ollama:{client.model}",
                "llm": stats,
            },
            peak_vram_mb=peak or None,
            detail=f"explained {len(done)} of {len(rows)} trends"
            + (f", {len(failed)} refused" if failed else ""),
        )

    def _rerank(self, job_id: str) -> None:
        """Re-read the rows — a resumed run has verdicts this attempt never saw."""
        rows = self._trends.for_job(job_id)
        ordered = sorted(
            rows,
            key=lambda trend: (
                -blend_rank(
                    trend.score,
                    trend.relevance,
                    worth_clipping=trend.worth_clipping is not False,
                ),
                trend.rank,
            ),
        )
        self._trends.rerank(
            [
                (trend.id, position)
                for position, trend in enumerate(ordered, start=1)
                if trend.rank != position
            ]
        )

    def _build_client(self, context: StageContext) -> OllamaClient:
        if self._client_factory is not None:
            return self._client_factory(context)  # type: ignore[operator, no-any-return]
        settings = context.settings
        return OllamaClient(
            host=settings.ollama_host,
            model=settings.ollama_model,
            num_ctx=settings.ollama_num_ctx,
        )


def _as_stage_failure(exc: OllamaError) -> Exception:
    failure = ResearchError(
        str(exc),
        retryable=exc.retryable,
        code="LLM_UNAVAILABLE" if exc.retryable else "LLM_INVALID_OUTPUT",
    )
    return failure


# Re-exported for the stage's tests, which script the model.
__all__ += ["LlmTrendVerdict"]
