"""The stage runner: executes a claimed job's stages in order, resumably.

The contract it implements, from docs/PLAN.md §3.3: a stage that is ``DONE`` is
never re-executed on retry. That is what makes a crash during ANALYZE cost
seconds instead of the twenty minutes that DOWNLOAD and TRANSCRIBE took.

Three behaviours are worth stating because they are easy to get wrong:

**The job document is written after every stage, not at the end.** A crash
between stages must leave a document that describes what actually happened. This
costs one write per stage — deliberately per *stage*, not per progress tick,
because Firestore bills per write and a chatty progress loop is the single
easiest way to burn the free daily quota (docs/PLAN.md §7).

**The lease is renewed between stages, and a lost lease aborts immediately.**
If the reaper reclaimed this job while a long stage ran, another worker now owns
it. Continuing would mean two workers writing conflicting stage results.

**A stage asked to stop leaves itself PENDING.** Marking it DONE would silently
skip work that never happened. Its checkpoint is still persisted, so the next
attempt resumes from wherever it reached.
"""

from __future__ import annotations

import time
import traceback
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from threading import Event

from clipforge_contracts import (
    Job,
    JobEvent,
    JobEventKind,
    JobStatus,
    Stage,
    StageError,
    StageStatus,
)

from clipforge.config import Settings
from clipforge.models.broker import InsufficientVramError, ModelBroker
from clipforge.observability import bind_job, get_logger
from clipforge.scheduler import lease
from clipforge.scheduler.lease import Transition
from clipforge.stages.base import StageContext, StageOutcome, StageRegistry
from clipforge.store.firestore import JobStore

log = get_logger(__name__)

__all__ = ["LeaseLostError", "StageRunner"]

Clock = Callable[[], datetime]


def _now() -> datetime:
    return datetime.now(UTC)


class LeaseLostError(RuntimeError):
    """This worker no longer owns the job and must stop touching it."""


def _replace_stage(job: Job, index: int, stage: Stage) -> Job:
    stages = list(job.stages)
    stages[index] = stage
    return job.model_copy(update={"stages": stages})


def _event(
    job: Job, kind: JobEventKind, at: datetime, *, stage: Stage | None, detail: str | None
) -> JobEvent:
    return JobEvent(
        id=uuid.uuid4().hex,
        job_id=job.id,
        kind=kind,
        at=at,
        seq=0,
        stage=stage.name if stage else None,
        worker_id=job.worker_id,
        detail=detail,
        attempts=job.attempts,
    )


class StageRunner:
    """Runs the stages of one claimed job."""

    def __init__(
        self,
        *,
        store: JobStore,
        registry: StageRegistry,
        settings: Settings,
        broker: ModelBroker,
        clock: Clock = _now,
    ) -> None:
        self._store = store
        self._registry = registry
        self._settings = settings
        self._broker = broker
        self._clock = clock

    def run(self, job: Job, should_stop: Event | None = None) -> Job:
        """Execute every outstanding stage. Returns the job in its final state."""
        should_stop = should_stop or Event()

        with bind_job(job_id=job.id, worker_id=job.worker_id, job_attempt=job.attempts):
            for index, stage in enumerate(job.stages):
                if stage.status in (StageStatus.DONE, StageStatus.SKIPPED):
                    log.debug("stage.skipped", stage=stage.name.value, status=stage.status.value)
                    continue

                if should_stop.is_set():
                    log.info("runner.stopping_before_stage", stage=stage.name.value)
                    return job

                job = self._run_one(job, index, should_stop)

                # A stage that failed took the job to QUEUED or FAILED.
                if job.status is not JobStatus.RUNNING:
                    return job

                # A stage that stopped early stays PENDING; do not proceed past it.
                if job.stages[index].status is StageStatus.PENDING:
                    return job

                job = self._renew(job)

            transition = lease.complete(job, now=self._clock())
            self._store.apply(transition)
            log.info("job.completed", stages=len(job.stages))
            return transition.job

    # ── One stage ────────────────────────────────────────────────────────────

    def _run_one(self, job: Job, index: int, should_stop: Event) -> Job:
        stage = job.stages[index]
        started = self._clock()
        monotonic_start = time.monotonic()

        with bind_job(stage=stage.name.value):
            running = stage.model_copy(
                update={
                    "status": StageStatus.RUNNING,
                    "started_at": started,
                    "attempts": (stage.attempts or 0) + 1,
                    "error": None,
                }
            )
            job = _replace_stage(job, index, running).model_copy(update={"updated_at": started})
            self._store.apply(
                Transition(
                    job=job,
                    events=(
                        _event(
                            job,
                            JobEventKind.STAGE_STARTED,
                            started,
                            stage=running,
                            detail=None,
                        ),
                    ),
                )
            )
            log.info("stage.started", stage_attempt=running.attempts)

            try:
                outcome = self._registry.get(stage.name).run(
                    StageContext(
                        job=job,
                        stage_name=stage.name,
                        checkpoint=stage.checkpoint,
                        settings=self._settings,
                        broker=self._broker,
                        should_stop=should_stop,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - every stage failure is captured, not raised
                return self._record_failure(job, index, running, exc)

            return self._record_success(job, index, running, outcome, monotonic_start)

    def _record_success(
        self,
        job: Job,
        index: int,
        running: Stage,
        outcome: StageOutcome,
        monotonic_start: float,
    ) -> Job:
        ended = self._clock()
        duration_ms = int((time.monotonic() - monotonic_start) * 1000)

        # An incomplete stage goes back to PENDING, not DONE: it stopped on
        # request, and marking it DONE would silently skip work.
        settled = StageStatus.PENDING if outcome.incomplete else StageStatus.DONE

        finished = running.model_copy(
            update={
                "status": settled,
                "ended_at": None if outcome.incomplete else ended,
                "duration_ms": duration_ms,
                "peak_vram_mb": outcome.peak_vram_mb,
                "checkpoint": outcome.checkpoint,
            }
        )
        job = _replace_stage(job, index, finished).model_copy(update={"updated_at": ended})

        if outcome.incomplete:
            self._store.apply(Transition(job=job, events=()))
            log.info("stage.paused", duration_ms=duration_ms)
            return job

        self._store.apply(
            Transition(
                job=job,
                events=(
                    _event(
                        job,
                        JobEventKind.STAGE_COMPLETED,
                        ended,
                        stage=finished,
                        detail=outcome.detail,
                    ),
                ),
            )
        )
        log.info("stage.completed", duration_ms=duration_ms, peak_vram_mb=outcome.peak_vram_mb)
        return job

    def _record_failure(self, job: Job, index: int, running: Stage, exc: Exception) -> Job:
        ended = self._clock()

        # A VRAM shortfall is not the job's fault and will very likely succeed
        # once whatever is holding the card lets go, so it stays retryable. A
        # programming error will not fix itself, and burning two more attempts on
        # it wastes twenty minutes to learn nothing.
        retryable = isinstance(exc, InsufficientVramError | TimeoutError | ConnectionError)

        error = StageError(
            type=type(exc).__name__,
            message=str(exc),
            traceback="".join(traceback.format_exception(exc))[-4000:],
            retryable=retryable,
        )

        failed_stage = running.model_copy(
            update={"status": StageStatus.FAILED, "ended_at": ended, "error": error}
        )
        job = _replace_stage(job, index, failed_stage)

        transition = lease.fail_stage(job, error=error, now=ended)
        self._store.apply(transition)

        log.warning(
            "stage.failed",
            error_type=error.type,
            error=error.message,
            retryable=retryable,
            job_status=transition.job.status.value,
        )
        return transition.job

    # ── Lease ────────────────────────────────────────────────────────────────

    def _renew(self, job: Job) -> Job:
        renewed = self._store.renew(job.id)
        if renewed is None:
            log.warning("lease.lost", hint="another worker reclaimed this job; abandoning it")
            raise LeaseLostError(f"lease on job {job.id} was lost")
        return renewed
