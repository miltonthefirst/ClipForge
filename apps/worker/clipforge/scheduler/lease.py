"""The job lease protocol, as pure functions over job documents.

This module contains no I/O. Every function takes a :class:`Job` and a clock
reading and returns the job that should replace it, plus the events that
transition should append. Nothing here knows Firestore exists.

That is deliberate, and it buys three things:

1. **The reaper is bindable anywhere.** `reap()` is a pure function over a job
   document, so the same logic runs as a periodic worker task or as a scheduled
   Cloud Function without being written twice. For a single-worker deployment
   the worker task is preferred — it is cheaper, and it is what keeps the
   deployment free of Cloud Functions entirely.
   See docs/adr/0004-dedicated-firebase-project.md.

2. **The state machine is testable without infrastructure.** Every transition in
   docs/PLAN.md §3.3, including the ones that are awkward to provoke against a
   real database (a lease expiring mid-stage, attempts running out), is an
   ordinary unit test.

3. **Transactional correctness stays in one place.** The adapter's job is only to
   read, apply, and compare-and-swap. It cannot get the *rules* wrong because it
   does not contain any.

The state machine, from docs/PLAN.md §3.3::

    QUEUED  --claim (CAS status, set workerId + leaseExpiresAt)--> RUNNING
    RUNNING --heartbeat every 30s, extends the lease-----------> RUNNING
    RUNNING --all stages DONE---------------------------------> COMPLETED
    RUNNING --stage FAILED, attempts < max--------------------> QUEUED
    RUNNING --stage FAILED, attempts >= max-------------------> FAILED
    RUNNING --lease expired (reaper)--------------------------> QUEUED
    any     --user cancels------------------------------------> CANCELLED
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from clipforge_contracts import (
    Job,
    JobEvent,
    JobEventKind,
    JobStatus,
    StageError,
    StageName,
    StageStatus,
)

__all__ = [
    "LeaseError",
    "Transition",
    "cancel",
    "claim",
    "complete",
    "fail_stage",
    "is_claimable",
    "is_due",
    "is_lease_expired",
    "reap",
    "release",
    "renew",
]

EventIdFactory = Callable[[], str]


def _default_event_id() -> str:
    return uuid.uuid4().hex


class LeaseError(RuntimeError):
    """An attempted transition is not legal from the job's current state.

    Raised rather than returned because every call site in the worker treats it
    as a bug: the scheduler should not be attempting an illegal transition, and
    silently returning ``None`` would let it continue as though it had worked.
    The one genuinely-expected "no" — a job that is simply not claimable because
    someone else got there first — is answered by :func:`is_claimable`, not by
    an exception.
    """


@dataclass(frozen=True)
class Transition:
    """The result of a state transition: the new job, and what to log about it."""

    job: Job
    events: tuple[JobEvent, ...]

    @property
    def status(self) -> JobStatus:
        return self.job.status


def _event(
    job: Job,
    kind: JobEventKind,
    at: datetime,
    *,
    event_id: str,
    seq: int = 0,
    detail: str | None = None,
    worker_id: str | None = None,
    stage: StageName | None = None,
) -> JobEvent:
    return JobEvent(
        id=event_id,
        job_id=job.id,
        kind=kind,
        at=at,
        seq=seq,
        stage=stage,
        worker_id=worker_id if worker_id is not None else job.worker_id,
        detail=detail,
        attempts=job.attempts,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Predicates
# ─────────────────────────────────────────────────────────────────────────────


def is_lease_expired(job: Job, now: datetime) -> bool:
    """True when a RUNNING job's lease has lapsed.

    A RUNNING job with no ``leaseExpiresAt`` at all is treated as expired. That
    state should be unreachable — claiming always sets a lease — but if it ever
    occurs the job is stuck forever unless something reclaims it, and a stuck job
    is worse than an extra reclaim.
    """
    if job.status is not JobStatus.RUNNING:
        return False
    if job.lease_expires_at is None:
        return True
    return job.lease_expires_at <= now


def is_due(job: Job, now: datetime) -> bool:
    """True when a job's scheduled start time has arrived.

    ``notBefore`` is how a publish-at time is honoured (docs/PLAN.md Phase 8).
    Putting it here rather than in a separate scheduler means every path that
    can start a job — a fresh claim, a reclaim after a crash — consults the same
    predicate, so there is no second scheduler to disagree with the first.
    """
    return job.not_before is None or job.not_before <= now


def is_claimable(job: Job, now: datetime) -> bool:
    """True when a worker may take this job.

    Either it is waiting, or its previous owner stopped renewing the lease. The
    second case is what makes a worker crash recoverable without operator
    intervention.

    A job scheduled for later is not claimable yet, however it got here. Note
    that this also holds for a reclaim: a scheduled job whose worker died stays
    unclaimable until its time, which is correct — the schedule is a property of
    the job, not of the attempt.

    **A reclaim that would exceed `maxAttempts` is refused.** Reclaiming costs
    an attempt — `claim` says so — and nothing here was checking the budget it
    was spending. `reap` has always refused to requeue an exhausted job, but a
    polling worker reaches `is_claimable` first, so whichever ran sooner decided
    the outcome. A REMAKE is created with `maxAttempts: 1` precisely because
    every way it can fail is a property of the request rather than of the
    moment; one of them deadlocked, lost its lease, and was picked straight back
    up for a second attempt it was never entitled to.

    Refusing here rather than raising in `claim` leaves the job RUNNING with a
    dead lease until the reaper sees it, which is exactly what the reaper is
    for: it fails the job with a message naming the exhausted attempts, and a
    worker that merely declined to take it has nothing useful to say.
    """
    if not is_due(job, now):
        return False
    if job.status is JobStatus.QUEUED:
        return True
    return is_lease_expired(job, now) and job.attempts + 1 < job.max_attempts


def _is_terminal(job: Job) -> bool:
    return job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)


# ─────────────────────────────────────────────────────────────────────────────
# Transitions
# ─────────────────────────────────────────────────────────────────────────────


def claim(
    job: Job,
    *,
    worker_id: str,
    now: datetime,
    lease_seconds: int,
    event_id: EventIdFactory = _default_event_id,
) -> Transition:
    """Take ownership of a job.

    The caller must apply this inside a transaction that compare-and-swaps on the
    job's prior state. Two workers can both *decide* to claim the same job; only
    the one whose write commits first may proceed. That check belongs to the
    adapter, because only the adapter knows what it read.
    """
    if not is_claimable(job, now):
        raise LeaseError(
            f"job {job.id} is not claimable: status={job.status.value}, "
            f"leaseExpiresAt={job.lease_expires_at}"
        )

    # Reclaiming an expired lease is a retry and costs an attempt; taking a
    # QUEUED job is not. Without this distinction a job whose worker keeps dying
    # would be retried forever.
    reclaiming = job.status is JobStatus.RUNNING
    attempts = job.attempts + 1 if reclaiming else job.attempts

    claimed = job.model_copy(
        update={
            "status": JobStatus.RUNNING,
            "worker_id": worker_id,
            "lease_expires_at": now + timedelta(seconds=lease_seconds),
            "attempts": attempts,
            "started_at": job.started_at or now,
            "updated_at": now,
        }
    )

    detail = "reclaimed an expired lease" if reclaiming else None
    return Transition(
        job=claimed,
        events=(
            _event(
                claimed,
                JobEventKind.CLAIMED,
                now,
                event_id=event_id(),
                detail=detail,
                worker_id=worker_id,
            ),
        ),
    )


def renew(
    job: Job,
    *,
    worker_id: str,
    now: datetime,
    lease_seconds: int,
) -> Transition:
    """Extend the lease on a job this worker already owns.

    Deliberately emits **no event**. A heartbeat every 30 seconds would otherwise
    dominate the event log and the Firestore write budget, while telling a reader
    nothing they could not infer from ``leaseExpiresAt``.
    """
    if job.status is not JobStatus.RUNNING:
        raise LeaseError(f"cannot renew a lease on a {job.status.value} job")
    if job.worker_id != worker_id:
        raise LeaseError(f"worker {worker_id} cannot renew a lease held by {job.worker_id}")

    renewed = job.model_copy(
        update={
            "lease_expires_at": now + timedelta(seconds=lease_seconds),
            "updated_at": now,
        }
    )
    return Transition(job=renewed, events=())


def reap(
    job: Job,
    *,
    now: datetime,
    event_id: EventIdFactory = _default_event_id,
) -> Transition | None:
    """Reclaim a job whose worker stopped heartbeating.

    Returns ``None`` when there is nothing to do, which is the overwhelmingly
    common case — the reaper runs on a timer and most passes find nothing. That
    is a normal outcome, not an error, so it is not an exception.

    Checkpoints are retained: a job requeued here resumes at the stage it reached,
    which is the whole point of the stage model (docs/PLAN.md decision D2).
    """
    if not is_lease_expired(job, now):
        return None

    attempts = job.attempts + 1
    expiry = _event(
        job,
        JobEventKind.LEASE_EXPIRED,
        now,
        event_id=event_id(),
        seq=0,
        detail=f"lease expired at {job.lease_expires_at}",
    )

    if attempts >= job.max_attempts:
        failed = job.model_copy(
            update={
                "status": JobStatus.FAILED,
                "attempts": attempts,
                "worker_id": None,
                "lease_expires_at": None,
                "error": StageError(
                    type="LeaseExpired",
                    message=(
                        f"worker stopped heartbeating; {attempts} of "
                        f"{job.max_attempts} attempts exhausted"
                    ),
                    traceback=None,
                    retryable=False,
                ),
                "ended_at": now,
                "updated_at": now,
            }
        )
        return Transition(
            job=failed,
            events=(
                expiry,
                _event(failed, JobEventKind.FAILED, now, event_id=event_id(), seq=1),
            ),
        )

    requeued = job.model_copy(
        update={
            "status": JobStatus.QUEUED,
            "attempts": attempts,
            "worker_id": None,
            "lease_expires_at": None,
            "updated_at": now,
        }
    )
    return Transition(
        job=requeued,
        events=(
            expiry,
            _event(requeued, JobEventKind.REQUEUED, now, event_id=event_id(), seq=1),
        ),
    )


def complete(
    job: Job,
    *,
    now: datetime,
    event_id: EventIdFactory = _default_event_id,
) -> Transition:
    """Mark a job finished. Every stage must have reached a settled state."""
    if job.status is not JobStatus.RUNNING:
        raise LeaseError(f"cannot complete a {job.status.value} job")

    unfinished = [
        stage.name.value
        for stage in job.stages
        if stage.status not in (StageStatus.DONE, StageStatus.SKIPPED)
    ]
    if unfinished:
        raise LeaseError(f"job {job.id} has unfinished stages: {', '.join(unfinished)}")

    completed = job.model_copy(
        update={
            "status": JobStatus.COMPLETED,
            "worker_id": None,
            "lease_expires_at": None,
            "ended_at": now,
            "updated_at": now,
        }
    )
    return Transition(
        job=completed,
        events=(_event(completed, JobEventKind.COMPLETED, now, event_id=event_id()),),
    )


def _running_stage(job: Job) -> StageName | None:
    """Which stage a failure belongs to.

    The one that is RUNNING, and failing back to the first that is not finished
    — a stage can be marked failed from a path that has already moved it off
    RUNNING, and an approximate name beats none.
    """
    for stage in job.stages:
        if stage.status is StageStatus.RUNNING:
            return stage.name
    for stage in job.stages:
        if stage.status not in (StageStatus.DONE, StageStatus.SKIPPED):
            return stage.name
    return None


def fail_stage(
    job: Job,
    *,
    error: StageError,
    now: datetime,
    event_id: EventIdFactory = _default_event_id,
) -> Transition:
    """Record a stage failure and decide whether the job retries or dies.

    A non-retryable error skips the remaining attempts entirely. Burning two more
    attempts on a malformed URL or a refused rights attestation wastes twenty
    minutes and tells nobody anything new.
    """
    if job.status is not JobStatus.RUNNING:
        raise LeaseError(f"cannot fail a stage on a {job.status.value} job")

    attempts = job.attempts + 1
    give_up = not error.retryable or attempts >= job.max_attempts

    # Named, unlike every other event here. A job's history is read by whoever
    # is working out why a clip did not arrive, and an unnamed failure renders
    # as "null failed" above the only line that says what went wrong.
    stage_failed = _event(
        job,
        JobEventKind.STAGE_FAILED,
        now,
        event_id=event_id(),
        seq=0,
        detail=error.message,
        stage=_running_stage(job),
    )

    if give_up:
        failed = job.model_copy(
            update={
                "status": JobStatus.FAILED,
                "attempts": attempts,
                "worker_id": None,
                "lease_expires_at": None,
                "error": error,
                "ended_at": now,
                "updated_at": now,
            }
        )
        return Transition(
            job=failed,
            events=(
                stage_failed,
                _event(failed, JobEventKind.FAILED, now, event_id=event_id(), seq=1),
            ),
        )

    requeued = job.model_copy(
        update={
            "status": JobStatus.QUEUED,
            "attempts": attempts,
            "worker_id": None,
            "lease_expires_at": None,
            "error": error,
            "updated_at": now,
        }
    )
    return Transition(
        job=requeued,
        events=(
            stage_failed,
            _event(requeued, JobEventKind.REQUEUED, now, event_id=event_id(), seq=1),
        ),
    )


def release(
    job: Job,
    *,
    now: datetime,
    reason: str = "worker shut down",
    event_id: EventIdFactory = _default_event_id,
) -> Transition:
    """Hand a job back to the queue on a clean shutdown.

    Deliberately does **not** cost an attempt. A worker stopping on purpose is a
    handover, not a failure — charging it an attempt would mean three orderly
    restarts could exhaust a job that never actually went wrong.

    Returning the job to QUEUED rather than leaving the lease to lapse is what
    lets another worker pick it up immediately instead of after 90 seconds of
    nothing happening.
    """
    if job.status is not JobStatus.RUNNING:
        raise LeaseError(f"cannot release a {job.status.value} job")

    released = job.model_copy(
        update={
            "status": JobStatus.QUEUED,
            "worker_id": None,
            "lease_expires_at": None,
            "updated_at": now,
        }
    )
    return Transition(
        job=released,
        events=(
            _event(
                released,
                JobEventKind.REQUEUED,
                now,
                event_id=event_id(),
                detail=reason,
                worker_id=job.worker_id,
            ),
        ),
    )


def cancel(
    job: Job,
    *,
    now: datetime,
    event_id: EventIdFactory = _default_event_id,
) -> Transition:
    """Cancel a job. Legal from QUEUED or RUNNING only."""
    if _is_terminal(job):
        raise LeaseError(f"cannot cancel a job that is already {job.status.value}")

    cancelled = job.model_copy(
        update={
            "status": JobStatus.CANCELLED,
            "worker_id": None,
            "lease_expires_at": None,
            "ended_at": now,
            "updated_at": now,
        }
    )
    return Transition(
        job=cancelled,
        events=(_event(cancelled, JobEventKind.CANCELLED, now, event_id=event_id()),),
    )
