"""The job state machine, exhaustively.

Every transition in docs/PLAN.md Â§3.3 is exercised here, including the ones that
are awkward to provoke against a real database â€” a lease lapsing mid-stage,
attempts running out, a worker trying to renew someone else's lease. That is the
payoff of keeping the state machine pure: these cost microseconds and need no
emulator.

The emulator-backed half â€” that two workers cannot both win the same job, and
that the reaper actually reclaims â€” lives in tests/integration/test_lease_store.py.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from clipforge.scheduler import lease
from clipforge.scheduler.lease import LeaseError
from clipforge_contracts import (
    Job,
    JobEventKind,
    JobStatus,
    JobType,
    Lane,
    Stage,
    StageError,
    StageName,
    StageStatus,
)
from google.api_core import exceptions as gcloud_exceptions

T0 = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)
LEASE_SECONDS = 90


def _ids() -> Callable[[], str]:
    """Deterministic event ids, so assertions can name them."""
    counter = itertools.count(1)
    return lambda: f"evt-{next(counter)}"


def make_job(**overrides: object) -> Job:
    defaults: dict[str, object] = {
        "id": "job-1",
        "uid": "user-1",
        "type": JobType.ECHO,
        "status": JobStatus.QUEUED,
        "stages": [
            Stage(name=StageName.ECHO_ONE, lane=Lane.CPU, status=StageStatus.PENDING),
            Stage(name=StageName.ECHO_TWO, lane=Lane.CPU, status=StageStatus.PENDING),
        ],
        "attempts": 0,
        "max_attempts": 3,
        "created_at": T0,
        "updated_at": T0,
    }
    return Job(**{**defaults, **overrides})


def running_job(**overrides: object) -> Job:
    running: dict[str, object] = {
        "status": JobStatus.RUNNING,
        "worker_id": "worker-a",
        "lease_expires_at": T0 + timedelta(seconds=LEASE_SECONDS),
        "started_at": T0,
    }
    return make_job(**{**running, **overrides})


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Predicates
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.unit
def test_a_queued_job_is_claimable() -> None:
    assert lease.is_claimable(make_job(), T0)


@pytest.mark.unit
def test_a_running_job_with_a_live_lease_is_not_claimable() -> None:
    job = running_job()
    assert not lease.is_claimable(job, T0 + timedelta(seconds=30))


@pytest.mark.unit
def test_a_running_job_with_a_lapsed_lease_is_claimable() -> None:
    job = running_job()
    assert lease.is_claimable(job, T0 + timedelta(seconds=91))


@pytest.mark.unit
def test_a_lease_expiring_exactly_now_counts_as_expired() -> None:
    """The boundary matters: `<=`, not `<`. A lease that expires at exactly the
    current instant is over, and treating it as live strands the job for a whole
    reaper cycle."""
    job = running_job()
    assert lease.is_lease_expired(job, T0 + timedelta(seconds=LEASE_SECONDS))


@pytest.mark.unit
def test_a_running_job_with_no_lease_at_all_is_treated_as_expired() -> None:
    """Unreachable in principle, but a stuck job is worse than an extra reclaim."""
    job = running_job(lease_expires_at=None)
    assert lease.is_lease_expired(job, T0)


@pytest.mark.unit
@pytest.mark.parametrize(
    "status", [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.QUEUED]
)
def test_only_running_jobs_can_have_an_expired_lease(status: JobStatus) -> None:
    assert not lease.is_lease_expired(make_job(status=status), T0)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Claiming
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.unit
def test_claiming_sets_owner_lease_and_status() -> None:
    result = lease.claim(
        make_job(), worker_id="worker-a", now=T0, lease_seconds=LEASE_SECONDS, event_id=_ids()
    )

    assert result.job.status is JobStatus.RUNNING
    assert result.job.worker_id == "worker-a"
    assert result.job.lease_expires_at == T0 + timedelta(seconds=LEASE_SECONDS)
    assert result.job.started_at == T0
    assert [e.kind for e in result.events] == [JobEventKind.CLAIMED]


@pytest.mark.unit
def test_claiming_a_queued_job_does_not_cost_an_attempt() -> None:
    result = lease.claim(
        make_job(), worker_id="worker-a", now=T0, lease_seconds=LEASE_SECONDS, event_id=_ids()
    )
    assert result.job.attempts == 0


@pytest.mark.unit
def test_reclaiming_an_expired_lease_costs_an_attempt() -> None:
    """Otherwise a job whose worker keeps dying is retried forever."""
    job = running_job()
    later = T0 + timedelta(seconds=200)

    result = lease.claim(
        job, worker_id="worker-b", now=later, lease_seconds=LEASE_SECONDS, event_id=_ids()
    )

    assert result.job.attempts == 1
    assert result.job.worker_id == "worker-b"
    assert result.events[0].detail == "reclaimed an expired lease"


@pytest.mark.unit
def test_reclaiming_preserves_the_original_start_time() -> None:
    job = running_job()
    result = lease.claim(
        job,
        worker_id="worker-b",
        now=T0 + timedelta(seconds=200),
        lease_seconds=LEASE_SECONDS,
        event_id=_ids(),
    )
    assert result.job.started_at == T0


@pytest.mark.unit
def test_claiming_a_live_job_is_refused() -> None:
    with pytest.raises(LeaseError, match="not claimable"):
        lease.claim(
            running_job(),
            worker_id="worker-b",
            now=T0 + timedelta(seconds=10),
            lease_seconds=LEASE_SECONDS,
        )


@pytest.mark.unit
@pytest.mark.parametrize("status", [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED])
def test_claiming_a_finished_job_is_refused(status: JobStatus) -> None:
    with pytest.raises(LeaseError):
        lease.claim(
            make_job(status=status),
            worker_id="worker-a",
            now=T0,
            lease_seconds=LEASE_SECONDS,
        )


@pytest.mark.unit
def test_claiming_does_not_mutate_the_input() -> None:
    """These are pure functions; the caller still holds what it read."""
    job = make_job()
    lease.claim(job, worker_id="worker-a", now=T0, lease_seconds=LEASE_SECONDS, event_id=_ids())
    assert job.status is JobStatus.QUEUED
    assert job.worker_id is None


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Heartbeat
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.unit
def test_renewing_extends_the_lease_from_now() -> None:
    job = running_job()
    at = T0 + timedelta(seconds=30)

    result = lease.renew(job, worker_id="worker-a", now=at, lease_seconds=LEASE_SECONDS)

    assert result.job.lease_expires_at == at + timedelta(seconds=LEASE_SECONDS)


@pytest.mark.unit
def test_renewing_emits_no_event() -> None:
    """A 30s heartbeat would otherwise dominate the event log and the write budget."""
    result = lease.renew(running_job(), worker_id="worker-a", now=T0, lease_seconds=LEASE_SECONDS)
    assert result.events == ()


@pytest.mark.unit
def test_a_worker_cannot_renew_someone_elses_lease() -> None:
    with pytest.raises(LeaseError, match="cannot renew a lease held by"):
        lease.renew(running_job(), worker_id="worker-b", now=T0, lease_seconds=LEASE_SECONDS)


@pytest.mark.unit
def test_cannot_renew_a_job_that_is_not_running() -> None:
    with pytest.raises(LeaseError):
        lease.renew(make_job(), worker_id="worker-a", now=T0, lease_seconds=LEASE_SECONDS)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# The reaper â€” Phase 1, exit criterion 3
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.unit
def test_the_reaper_ignores_a_healthy_job() -> None:
    """The common case. Most reaper passes find nothing, which is not an error."""
    assert lease.reap(running_job(), now=T0 + timedelta(seconds=30)) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "status", [JobStatus.QUEUED, JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]
)
def test_the_reaper_ignores_jobs_that_are_not_running(status: JobStatus) -> None:
    assert lease.reap(make_job(status=status), now=T0 + timedelta(days=1)) is None


@pytest.mark.unit
def test_an_expired_lease_returns_the_job_to_the_queue_with_attempts_incremented() -> None:
    """Phase 1, exit criterion 3."""
    result = lease.reap(running_job(), now=T0 + timedelta(seconds=200), event_id=_ids())

    assert result is not None
    assert result.job.status is JobStatus.QUEUED
    assert result.job.attempts == 1
    assert result.job.worker_id is None
    assert result.job.lease_expires_at is None
    assert [e.kind for e in result.events] == [
        JobEventKind.LEASE_EXPIRED,
        JobEventKind.REQUEUED,
    ]


@pytest.mark.unit
def test_reaping_retains_stage_checkpoints() -> None:
    """Decision D2: a requeued job resumes where it stopped. If the reaper reset
    stage state, a crash during ANALYZE would cost the whole download again."""
    job = running_job(
        stages=[
            Stage(
                name=StageName.ECHO_ONE,
                lane=Lane.CPU,
                status=StageStatus.DONE,
                checkpoint={"bytes": 1024},
            ),
            Stage(name=StageName.ECHO_TWO, lane=Lane.CPU, status=StageStatus.RUNNING),
        ]
    )

    result = lease.reap(job, now=T0 + timedelta(seconds=200), event_id=_ids())

    assert result is not None
    assert result.job.stages[0].status is StageStatus.DONE
    assert result.job.stages[0].checkpoint == {"bytes": 1024}


@pytest.mark.unit
def test_the_last_attempt_fails_the_job_rather_than_requeueing_forever() -> None:
    job = running_job(attempts=2, max_attempts=3)

    result = lease.reap(job, now=T0 + timedelta(seconds=200), event_id=_ids())

    assert result is not None
    assert result.job.status is JobStatus.FAILED
    assert result.job.attempts == 3
    assert result.job.error is not None
    assert result.job.error.type == "LeaseExpired"
    assert result.job.error.retryable is False
    assert result.job.ended_at == T0 + timedelta(seconds=200)
    assert [e.kind for e in result.events] == [JobEventKind.LEASE_EXPIRED, JobEventKind.FAILED]


@pytest.mark.unit
def test_a_single_attempt_job_fails_on_first_expiry() -> None:
    job = running_job(attempts=0, max_attempts=1)
    result = lease.reap(job, now=T0 + timedelta(seconds=200), event_id=_ids())
    assert result is not None
    assert result.job.status is JobStatus.FAILED


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Completion and failure
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.unit
def test_completing_requires_every_stage_to_be_settled() -> None:
    with pytest.raises(LeaseError, match="unfinished stages"):
        lease.complete(running_job(), now=T0)


@pytest.mark.unit
def test_completing_releases_the_lease() -> None:
    job = running_job(
        stages=[
            Stage(name=StageName.ECHO_ONE, lane=Lane.CPU, status=StageStatus.DONE),
            Stage(name=StageName.ECHO_TWO, lane=Lane.CPU, status=StageStatus.SKIPPED),
        ]
    )

    result = lease.complete(job, now=T0 + timedelta(seconds=60), event_id=_ids())

    assert result.job.status is JobStatus.COMPLETED
    assert result.job.worker_id is None
    assert result.job.lease_expires_at is None
    assert result.job.ended_at == T0 + timedelta(seconds=60)
    assert [e.kind for e in result.events] == [JobEventKind.COMPLETED]


@pytest.mark.unit
def test_a_retryable_failure_requeues() -> None:
    error = StageError(type="TimeoutError", message="ollama did not respond", retryable=True)

    result = lease.fail_stage(running_job(), error=error, now=T0, event_id=_ids())

    assert result.job.status is JobStatus.QUEUED
    assert result.job.attempts == 1
    assert result.job.error is not None
    assert [e.kind for e in result.events] == [
        JobEventKind.STAGE_FAILED,
        JobEventKind.REQUEUED,
    ]


@pytest.mark.unit
def test_a_non_retryable_failure_skips_the_remaining_attempts() -> None:
    """Burning two more attempts on a malformed URL wastes twenty minutes and
    tells nobody anything new."""
    error = StageError(type="ValueError", message="not a YouTube URL", retryable=False)

    result = lease.fail_stage(running_job(attempts=0), error=error, now=T0, event_id=_ids())

    assert result.job.status is JobStatus.FAILED
    assert result.job.attempts == 1
    assert [e.kind for e in result.events] == [JobEventKind.STAGE_FAILED, JobEventKind.FAILED]


@pytest.mark.unit
def test_a_retryable_failure_on_the_last_attempt_still_fails() -> None:
    error = StageError(type="TimeoutError", message="again", retryable=True)
    result = lease.fail_stage(
        running_job(attempts=2, max_attempts=3), error=error, now=T0, event_id=_ids()
    )
    assert result.job.status is JobStatus.FAILED


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Cancellation
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.unit
@pytest.mark.parametrize("status", [JobStatus.QUEUED, JobStatus.RUNNING])
def test_cancelling_is_legal_from_queued_and_running(status: JobStatus) -> None:
    job = running_job() if status is JobStatus.RUNNING else make_job()
    result = lease.cancel(job, now=T0, event_id=_ids())
    assert result.job.status is JobStatus.CANCELLED
    assert result.job.lease_expires_at is None


@pytest.mark.unit
@pytest.mark.parametrize("status", [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED])
def test_cancelling_a_finished_job_is_refused(status: JobStatus) -> None:
    with pytest.raises(LeaseError, match="already"):
        lease.cancel(make_job(status=status), now=T0)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Round-trip: transitions must survive the wire.
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.unit
def test_a_transitioned_job_serialises_to_the_wire_form() -> None:
    """Everything here eventually becomes a Firestore document, so a transition
    that produced an unserialisable job would fail far from this module."""
    result = lease.claim(
        make_job(), worker_id="worker-a", now=T0, lease_seconds=LEASE_SECONDS, event_id=_ids()
    )

    wire = result.job.model_dump(by_alias=True, mode="json")
    assert wire["status"] == "RUNNING"
    assert wire["workerId"] == "worker-a"
    assert Job.model_validate(wire) == result.job

    event_wire = result.events[0].model_dump(by_alias=True, mode="json")
    assert event_wire["kind"] == "CLAIMED"
    assert event_wire["jobId"] == "job-1"


# ─────────────────────────────────────────────────────────────────────────────
# Phase 8: scheduled publishing, expressed as a claim predicate.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_a_job_with_no_schedule_is_always_due() -> None:
    assert lease.is_due(make_job(), T0)


@pytest.mark.unit
def test_a_job_scheduled_for_later_is_not_claimable_yet() -> None:
    """The publish-at time, expressed where every claim path already looks.

    A separate scheduler could disagree with this one; a predicate cannot.
    """
    scheduled = make_job(not_before=T0 + timedelta(hours=2))
    assert not lease.is_due(scheduled, T0)
    assert not lease.is_claimable(scheduled, T0)


@pytest.mark.unit
def test_a_scheduled_job_becomes_claimable_at_its_instant() -> None:
    """Inclusive at the boundary: `notBefore` means "not before", so the moment
    itself is allowed."""
    when = T0 + timedelta(hours=2)
    scheduled = make_job(not_before=when)
    assert lease.is_due(scheduled, when)
    assert lease.is_claimable(scheduled, when)


@pytest.mark.unit
def test_a_scheduled_job_whose_worker_died_still_waits_for_its_time() -> None:
    """The schedule is a property of the job, not of the attempt.

    Without this, a crash would promote a publish scheduled for 06:00 into one
    that goes out the moment the reaper notices — which is precisely the
    surprise scheduling exists to prevent.
    """
    stranded = running_job(
        not_before=T0 + timedelta(hours=2),
        lease_expires_at=T0 - timedelta(seconds=1),
    )
    assert lease.is_lease_expired(stranded, T0)
    assert not lease.is_claimable(stranded, T0)


# ─────────────────────────────────────────────────────────────────────────────
# Contention, as the Firestore client actually reports it.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_a_lost_race_is_recognised_in_both_shapes_the_client_raises() -> None:
    """Losing a race must never look like a fault.

    ``Aborted`` is the obvious shape. The one that actually bites is the other:
    when the client library exhausts its own retries it wraps the last
    ``Aborted`` in a plain ``ValueError``, so a handler catching only ``Aborted``
    turns a lost race into a crashed worker. Eight threads on one document
    reproduces it, which is how this was found.
    """
    from clipforge.store.firestore import _is_contention

    assert _is_contention(gcloud_exceptions.Aborted("too much contention"))  # type: ignore[no-untyped-call]

    exhausted = ValueError("Failed to commit transaction in 5 attempts.")
    exhausted.__cause__ = gcloud_exceptions.Aborted("aborted")  # type: ignore[no-untyped-call]
    assert _is_contention(exhausted)


@pytest.mark.unit
def test_an_unrelated_value_error_is_not_mistaken_for_contention() -> None:
    """Matching on the cause rather than the message keeps a genuine bug from
    being silently swallowed as "someone else won"."""
    from clipforge.store.firestore import _is_contention

    # Same message, no `Aborted` cause: a real bug, not a lost race.
    assert not _is_contention(ValueError("Failed to commit transaction in 5 attempts."))
    assert not _is_contention(ValueError("something else went wrong"))
    assert not _is_contention(RuntimeError("unrelated"))


def test_a_stage_failure_says_which_stage() -> None:
    """A job's history is read by someone working out why a clip never arrived.

    An unnamed failure renders as "null failed" directly above the only line
    that says what went wrong, which reads like a second, separate fault.
    """
    job = running_job(
        stages=[
            Stage(name=StageName.ECHO_ONE, lane=Lane.CPU, status=StageStatus.DONE),
            Stage(name=StageName.ECHO_TWO, lane=Lane.CPU, status=StageStatus.RUNNING),
        ]
    )
    transition = lease.fail_stage(
        job,
        error=StageError(type="RemakeStageError", retryable=False, message="nothing was narrated"),
        now=T0,
    )
    failed = next(e for e in transition.events if e.kind is JobEventKind.STAGE_FAILED)
    assert failed.stage is StageName.ECHO_TWO


def test_a_job_with_one_attempt_is_not_reclaimed_after_its_lease_expires() -> None:
    """`maxAttempts: 1` means never retry, and reclaiming is a retry.

    REMAKE is created that way on purpose: every way it can fail is a property
    of the request rather than of the moment. One of them deadlocked, lost its
    lease, and was picked straight back up — `reap` has always refused to
    requeue an exhausted job, but a polling worker reaches `is_claimable` first,
    so whichever ran sooner decided the outcome.
    """
    job = running_job(max_attempts=1, attempts=0)
    expired = T0 + timedelta(seconds=LEASE_SECONDS + 1)

    assert not lease.is_claimable(job, expired)


def test_the_reaper_is_what_fails_it() -> None:
    """Declining to take a job says nothing useful. The reaper names the reason."""
    job = running_job(max_attempts=1, attempts=0)
    expired = T0 + timedelta(seconds=LEASE_SECONDS + 1)

    outcome = lease.reap(job, now=expired)

    assert outcome is not None
    assert outcome.job.status is JobStatus.FAILED
    assert "attempts exhausted" in (outcome.job.error.message if outcome.job.error else "")


def test_a_job_with_attempts_left_is_still_reclaimed() -> None:
    """The crash-recovery path this must not break."""
    job = running_job(max_attempts=3, attempts=0)
    expired = T0 + timedelta(seconds=LEASE_SECONDS + 1)

    assert lease.is_claimable(job, expired)


def test_the_last_attempt_is_not_reclaimed() -> None:
    """Two used of three allows one more; three used allows none."""
    expired = T0 + timedelta(seconds=LEASE_SECONDS + 1)

    assert lease.is_claimable(running_job(max_attempts=3, attempts=1), expired)
    assert not lease.is_claimable(running_job(max_attempts=3, attempts=2), expired)
