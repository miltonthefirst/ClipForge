"""The ANALYZE stage: transcript in, ranked candidates out.

The pipeline from docs/PLAN.md Phase 5, in order: window, map, reduce, snap,
score, filter, rank. Every step except *map* is a pure function living in
`clipforge.analysis`, which is what makes this stage's behaviour testable without
a model — and what keeps the model's job narrow enough for a 4B one to do it.

The GPU discipline here matters as much as the algorithm. Whisper must be gone
before the LLM loads: at 6 GB they cannot be co-resident. The broker enforces
that, and the LLM call passes `keep_alive=0` so Ollama releases rather than
holding the model for the next request.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from clipforge_contracts import (
    Candidate,
    Lane,
    LlmClipResponse,
    StageName,
    Transcript,
)

from clipforge.analysis.boundaries import (
    DEFAULT_TOLERANCES,
    SnapTolerances,
    silences_from_speech,
    snap_end,
    snap_start,
    words_in,
)
from clipforge.analysis.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_prompt
from clipforge.analysis.ranking import (
    DEFAULT_WEIGHTS,
    ScoredWindow,
    ScoreWeights,
    merge_overlapping,
    rank,
    total_score,
)
from clipforge.analysis.windows import (
    DEFAULT_WINDOW_SPEC,
    WindowSpec,
    build_windows,
    render_window,
)
from clipforge.models.ollama import OllamaClient, OllamaError
from clipforge.observability import get_logger
from clipforge.stages.base import StageContext, StageOutcome
from clipforge.store.firestore import CandidateStore, SourceStore
from clipforge.store.transcripts import TranscriptArchive

log = get_logger(__name__)

__all__ = ["AnalyzeStage", "TranscriptMissingError", "select_candidates"]

# Ollama reports qwen3.5:4b at roughly 3.4 GB resident. Pessimistic on purpose:
# refusing to start is recoverable, an OOM part-way through 40 windows is not.
DEFAULT_LLM_VRAM_MB = 3600


class TranscriptMissingError(RuntimeError):
    """ANALYZE ran without TRANSCRIBE having produced a transcript."""


def select_candidates(
    transcript: Transcript,
    speech_spans: tuple[tuple[float, float], ...],
    *,
    propose: object,
    window_spec: WindowSpec | None = None,
    weights: ScoreWeights = DEFAULT_WEIGHTS,
    tolerances: SnapTolerances | None = None,
    iou_threshold: float = 0.5,
    min_duration_sec: float = 15.0,
    max_duration_sec: float = 75.0,
    score_floor: int = 0,
    limit: int = 5,
) -> list[ScoredWindow]:
    """Window, map, reduce, snap, filter, rank.

    ``propose`` is the only impure part — everything else is deterministic. It
    takes a rendered window and returns an :class:`LlmClipResponse`, which is
    what lets the whole selection algorithm be tested against a scripted model.

    Snapping happens **after** merging, deliberately. Merging compares the
    model's own proposals, and snapping two near-identical windows first would
    move them onto the same silences and make them look identical when they were
    not — or vice versa.
    """
    window_spec = window_spec or DEFAULT_WINDOW_SPEC
    tolerances = tolerances or DEFAULT_TOLERANCES
    duration = transcript.duration_sec or (
        max((s.end_sec for s in transcript.segments), default=0.0)
    )
    silences = silences_from_speech(
        speech_spans, duration_sec=duration, min_silence_sec=tolerances.min_silence_sec
    )

    proposed: list[ScoredWindow] = []
    for source_window in build_windows(transcript, window_spec):
        response = propose(source_window)  # type: ignore[operator]
        for clip in response.clips:
            # A model that ignores its window bounds produces a clip of someone
            # else's sentence. Cheaper to drop it than to render it.
            if clip.end_sec <= clip.start_sec:
                continue
            outside = (
                clip.start_sec < source_window.start_sec - 1.0
                or clip.end_sec > source_window.end_sec + 1.0
            )
            if outside:
                log.debug(
                    "analyze.window_escape",
                    window=source_window.index,
                    start=clip.start_sec,
                    end=clip.end_sec,
                )
                continue
            proposed.append(
                ScoredWindow(
                    start_sec=clip.start_sec,
                    end_sec=clip.end_sec,
                    sub_scores=clip.sub_scores,
                    hook=clip.hook,
                    reason=clip.reason,
                    total=total_score(clip.sub_scores, weights),
                )
            )

    merged = merge_overlapping(proposed, threshold=iou_threshold)

    snapped: list[ScoredWindow] = []
    for merged_window in merged:
        words = words_in(transcript, merged_window.start_sec, merged_window.end_sec)
        # A wider slice than the clip itself, so the snapper can see the words
        # just outside the boundary — which are exactly the ones a cut might
        # land inside.
        nearby = words_in(
            transcript,
            merged_window.start_sec - tolerances.start_back_sec - 1.0,
            merged_window.end_sec + tolerances.end_forward_sec + 1.0,
        )
        start = snap_start(
            merged_window.start_sec,
            first_word=words[0] if words else None,
            silences=silences,
            tolerances=tolerances,
            source_start_sec=0.0,
            words=nearby,
        )
        end = snap_end(
            merged_window.end_sec,
            last_word=words[-1] if words else None,
            silences=silences,
            tolerances=tolerances,
            source_end_sec=duration,
            words=nearby,
        )
        if end.time_sec > start.time_sec:
            snapped.append(
                ScoredWindow(
                    start_sec=start.time_sec,
                    end_sec=end.time_sec,
                    sub_scores=merged_window.sub_scores,
                    hook=merged_window.hook,
                    reason=merged_window.reason,
                    total=merged_window.total,
                )
            )

    return rank(
        snapped,
        weights=weights,
        min_duration_sec=min_duration_sec,
        max_duration_sec=max_duration_sec,
        score_floor=score_floor,
        limit=limit,
    )


class AnalyzeStage:
    """Turn a transcript into ranked, deduplicated, exactly-bounded candidates."""

    name = StageName.ANALYZE
    lane = Lane.GPU

    def __init__(
        self,
        *,
        sources: SourceStore,
        candidates: CandidateStore,
        archive: TranscriptArchive,
        client_factory: object = None,
        weights: ScoreWeights = DEFAULT_WEIGHTS,
        limit: int = 5,
    ) -> None:
        self._sources = sources
        self._candidates = candidates
        self._archive = archive
        self._client_factory = client_factory
        self._weights = weights
        self._limit = limit

    def run(self, context: StageContext) -> StageOutcome:
        transcript, speech_spans, source_id = self._load(context)
        client = self.build_client(context)

        # The whole map step runs under one broker lease. Acquiring per window
        # would let Whisper reload between calls and thrash a 6 GB card.
        #
        # Said before the acquire, not after: the lease is what TRANSCRIBE is
        # holding, so this line can block for the rest of another job's
        # twenty-minute stage without a byte of work happening here.
        context.progress("Waiting for the GPU")
        with context.broker.acquire(f"ollama:{client.model}", DEFAULT_LLM_VRAM_MB) as leased:
            selected = self.propose_windows(
                context, client, transcript, speech_spans, limit=self._limit
            )
            peak = leased.observe()

        documents = self.to_candidates(
            context,
            selected,
            transcript,
            source_id,
            model_version=f"ollama:{client.model}",
            prompt_version=PROMPT_VERSION,
        )
        self._candidates.replace_for_job(context.job.id, documents)

        stats = client.stats.summary()
        log.info("analyze.done", candidates=len(documents), **stats)

        return StageOutcome(
            checkpoint={
                "sourceId": source_id,
                "candidateIds": [c.id for c in documents],
                "promptVersion": PROMPT_VERSION,
                "modelVersion": f"ollama:{client.model}",
                # Recorded because a rising repair rate is the early warning that
                # a prompt or model change has degraded.
                "llm": stats,
            },
            peak_vram_mb=peak or None,
            detail=(
                f"{len(documents)} candidates from {stats['calls']} windows "
                f"({stats['firstAttemptRate']:.0%} first-attempt valid)"
            ),
        )

    # ── The selection, without the lease ─────────────────────────────────────
    #
    # Public so a COMPILE job's SELECT stage can run the same map-reduce over
    # each of its sources under one lease of its own, rather than acquiring
    # and releasing the card once per video.

    def propose_windows(
        self,
        context: StageContext,
        client: OllamaClient,
        transcript: Transcript,
        speech_spans: tuple[tuple[float, float], ...],
        *,
        limit: int,
        theme: str | None = None,
        min_duration_sec: float = 15.0,
        max_duration_sec: float = 75.0,
    ) -> list[ScoredWindow]:
        """Window, map, reduce, snap, rank — the caller holds the broker lease."""
        windows_read = 0

        def propose(window: object) -> LlmClipResponse:
            nonlocal windows_read
            windows_read += 1
            # A number with no total, because the windows are built inside
            # `select_candidates` and this side does not know how many there
            # are. A count that moves is still what separates "working" from
            # "hung", which is the only question a long map step leaves open.
            context.progress(f"Reading the transcript: window {windows_read}")
            return client.generate_structured(
                schema_model=LlmClipResponse,
                system=SYSTEM_PROMPT,
                prompt=build_prompt(
                    window_text=render_window(window),  # type: ignore[arg-type]
                    start_sec=window.start_sec,  # type: ignore[attr-defined]
                    end_sec=window.end_sec,  # type: ignore[attr-defined]
                    min_duration_sec=min_duration_sec,
                    max_duration_sec=max_duration_sec,
                    theme=theme,
                ),
            )

        try:
            return select_candidates(
                transcript,
                speech_spans,
                propose=propose,
                weights=self._weights,
                limit=limit,
                min_duration_sec=min_duration_sec,
                max_duration_sec=max_duration_sec,
            )
        except OllamaError as exc:
            raise _as_stage_failure(exc) from exc

    def to_candidates(
        self,
        context: StageContext,
        windows: Sequence[ScoredWindow],
        transcript: Transcript,
        source_id: str,
        *,
        model_version: str,
        prompt_version: str,
    ) -> list[Candidate]:
        """The documents the review queue reads, stamped with what produced them."""
        now = datetime.now(UTC)
        return [
            Candidate(
                id=uuid.uuid4().hex,
                uid=context.job.uid,
                source_id=source_id,
                job_id=context.job.id,
                start_sec=round(window.start_sec, 3),
                end_sec=round(window.end_sec, 3),
                sub_scores=window.sub_scores,
                total=window.total,
                hook=window.hook,
                reason=window.reason,
                transcript_excerpt=_excerpt(transcript, window.start_sec, window.end_sec),
                model_version=model_version,
                prompt_version=prompt_version,
                created_at=now,
            )
            for window in windows
        ]

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _load(
        self, context: StageContext
    ) -> tuple[Transcript, tuple[tuple[float, float], ...], str]:
        for stage in context.job.stages:
            if stage.name is StageName.TRANSCRIBE and stage.checkpoint:
                checkpoint = stage.checkpoint
                content_hash = str(checkpoint.get("contentHash", ""))
                version = str(checkpoint.get("modelVersion", ""))
                source_id = str(checkpoint.get("sourceId", ""))
                transcript = self._archive.load(content_hash, version)
                if transcript is None:
                    raise TranscriptMissingError(
                        f"transcript for {content_hash} is missing; re-run the job to rebuild it"
                    )
                spans = self._archive.load_speech_spans(content_hash, version)
                return transcript, spans, source_id

        raise TranscriptMissingError(f"job {context.job.id} reached ANALYZE with no transcript")

    def build_client(self, context: StageContext) -> OllamaClient:
        if self._client_factory is not None:
            return self._client_factory(context)  # type: ignore[operator, no-any-return]
        settings = context.settings
        return OllamaClient(
            host=settings.ollama_host,
            model=settings.ollama_model,
            num_ctx=settings.ollama_num_ctx,
        )


def _excerpt(transcript: Transcript, start_sec: float, end_sec: float, limit: int = 600) -> str:
    """The transcript text a reviewer actually reads.

    Load-bearing on the free tier: with no remote video playback, this and the
    poster frame are most of what a phone review has to go on.
    """
    text = " ".join(
        segment.text
        for segment in transcript.segments
        if segment.end_sec > start_sec and segment.start_sec < end_sec
    ).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _as_stage_failure(exc: OllamaError) -> Exception:
    failure = _ClassifiedLlmError(str(exc))
    failure.retryable = exc.retryable
    failure.code = "LLM_UNAVAILABLE" if exc.retryable else "LLM_INVALID_OUTPUT"
    return failure


class _ClassifiedLlmError(RuntimeError):
    """Carries the LLM failure's retryability into the runner's error record."""

    code: str | None = None
    retryable: bool = True
