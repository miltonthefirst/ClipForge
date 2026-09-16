"""The lease protocol against a real Firestore.

The pure state machine is covered exhaustively in tests/unit/test_lease.py. What
can only be proven here is that the *transactional* parts hold under real
concurrency: that two workers cannot both win the same job, and that a lease
genuinely expires and is genuinely reclaimed.

Phase 1, exit criteria 2 and 3.
"""

from __future__ import annotations

import concurrent.futures
from datetime import UTC, datetime, timedelta

import pytest
from clipforge.config import Settings
from clipforge.scheduler.lease import Transition
from clipforge.store.firestore import JobStore, WorkerStore
from clipforge_contracts import (
    Job,
    JobEventKind,
    JobStatus,
    JobType,
    Lane,
    Stage,
    StageName,
    StageStatus,
    WorkerCapabilities,
    WorkerHeartbeat,
    WorkerStatus,
)
from google.cloud import firestore

pytestmark = pytest.mark.integration

T0 = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)


def make_job(job_id: str = "job-1", **overrides: object) -> Job:
    defaults: dict[str, object] = {
        "id": job_id,
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


# ─────────────────────────────────────────────────────────────────────────────
# Round-trip
# ─────────────────────────────────────────────────────────────────────────────


def test_a_job_survives_the_round_trip_through_firestore(jobs: JobStore) -> None:
    """Timestamps and nested stage objects are the parts most likely to be
    mangled by the client library, so this asserts on them specifically."""
    original = make_job()
    jobs.create(original)

    loaded = jobs.get("job-1")

    assert loaded is not None
    assert loaded.created_at == T0
    assert loaded.status is JobStatus.QUEUED
    assert [s.name for s in loaded.stages] == [StageName.ECHO_ONE, StageName.ECHO_TWO]
    assert loaded == original


def test_a_missing_job_reads_as_none(jobs: JobStore) -> None:
    assert jobs.get("nope") is None


def test_lease_timestamps_are_stored_as_real_timestamps(jobs: JobStore) -> None:
    """If leaseExpiresAt were stored as a string, the reaper's range query would
    compare lexicographically and silently miss jobs."""
    jobs.create(make_job())
    jobs.try_claim("job-1", now=T0)

    # A range query can only match if the field is a real timestamp.
    expired = jobs.expired(now=T0 + timedelta(seconds=1000))
    assert [j.id for j in expired] == ["job-1"]


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 2 — exactly one worker wins
# ─────────────────────────────────────────────────────────────────────────────


def test_two_workers_racing_for_one_job_produce_exactly_one_winner(
    jobs: JobStore, other_worker: JobStore
) -> None:
    """Phase 1, exit criterion 2.

    Both workers attempt the same document at the same moment. Firestore's
    optimistic transaction retries on conflict, so the loser re-reads, sees
    RUNNING with a live lease, and correctly abandons the claim.
    """
    jobs.create(make_job())

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(jobs.try_claim, "job-1", now=T0),
            pool.submit(other_worker.try_claim, "job-1", now=T0),
        ]
        results = [f.result() for f in futures]

    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"expected exactly one winner, got {len(winners)}"

    stored = jobs.get("job-1")
    assert stored is not None
    assert stored.status is JobStatus.RUNNING
    assert stored.worker_id == winners[0].worker_id


def test_many_workers_racing_still_produce_exactly_one_winner(
    client: firestore.Client, settings: Settings
) -> None:
    """The two-worker case can pass by luck of scheduling. Eight cannot."""
    stores = [
        JobStore(client, settings.model_copy(update={"worker_id": f"worker-{i}"})) for i in range(8)
    ]
    stores[0].create(make_job("race-job"))

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(s.try_claim, "race-job", now=T0) for s in stores]
        results = [f.result() for f in futures]

    assert len([r for r in results if r is not None]) == 1


def test_a_claimed_job_cannot_be_claimed_again_while_the_lease_is_live(
    jobs: JobStore, other_worker: JobStore
) -> None:
    jobs.create(make_job())
    assert jobs.try_claim("job-1", now=T0) is not None
    assert other_worker.try_claim("job-1", now=T0 + timedelta(seconds=30)) is None


def test_claiming_a_missing_job_returns_none(jobs: JobStore) -> None:
    assert jobs.try_claim("does-not-exist", now=T0) is None


def test_claim_next_takes_the_oldest_job_first(jobs: JobStore) -> None:
    jobs.create(make_job("newer", created_at=T0 + timedelta(minutes=5)))
    jobs.create(make_job("older", created_at=T0))

    claimed = jobs.claim_next(now=T0 + timedelta(minutes=10))

    assert claimed is not None
    assert claimed.id == "older"


def test_claim_next_returns_none_when_the_queue_is_empty(jobs: JobStore) -> None:
    assert jobs.claim_next(now=T0) is None


def test_claiming_appends_an_event(jobs: JobStore) -> None:
    jobs.create(make_job())
    jobs.try_claim("job-1", now=T0)

    events = jobs.events("job-1")

    assert [e["kind"] for e in events] == [JobEventKind.CLAIMED.value]
    assert events[0]["workerId"] == "worker-a"


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 3 — the reaper
# ─────────────────────────────────────────────────────────────────────────────


def test_an_expired_lease_is_returned_to_the_queue_with_attempts_incremented(
    jobs: JobStore,
) -> None:
    """Phase 1, exit criterion 3."""
    jobs.create(make_job())
    jobs.try_claim("job-1", now=T0)

    reaped = jobs.reap(now=T0 + timedelta(seconds=200))

    assert [j.id for j in reaped] == ["job-1"]
    stored = jobs.get("job-1")
    assert stored is not None
    assert stored.status is JobStatus.QUEUED
    assert stored.attempts == 1
    assert stored.worker_id is None
    assert stored.lease_expires_at is None


def test_the_reaper_leaves_a_healthy_job_alone(jobs: JobStore) -> None:
    jobs.create(make_job())
    jobs.try_claim("job-1", now=T0)

    assert jobs.reap(now=T0 + timedelta(seconds=30)) == []

    stored = jobs.get("job-1")
    assert stored is not None
    assert stored.status is JobStatus.RUNNING
    assert stored.attempts == 0


def test_the_reaper_leaves_the_callers_own_job_alone(jobs: JobStore) -> None:
    """An expired lease does not always mean the owner is gone.

    It can also mean the owner is this process and Firestore was briefly
    unreachable — a stalled network is enough to miss every renewal in the
    window. Reclaiming then makes the worker steal a job it is still running,
    and the work is done twice: a re-render and a re-upload of clips that were
    already finished.
    """
    jobs.create(make_job())
    jobs.try_claim("job-1", now=T0)

    late = T0 + timedelta(seconds=600)
    assert jobs.reap(now=late, skip=["job-1"]) == []

    stored = jobs.get("job-1")
    assert stored is not None
    assert stored.status is JobStatus.RUNNING, "still ours"
    assert stored.attempts == 0, "and not charged an attempt"

    # Without the guard, the very same call reclaims it.
    assert [job.id for job in jobs.reap(now=late)] == ["job-1"]


def test_a_renewed_lease_survives_the_reaper(jobs: JobStore) -> None:
    """The heartbeat's entire purpose, end to end."""
    jobs.create(make_job())
    jobs.try_claim("job-1", now=T0)

    # Renew at T+60, extending the lease to T+150.
    assert jobs.renew("job-1", now=T0 + timedelta(seconds=60)) is not None

    # A reaper pass at T+100 would have reclaimed the original T+90 lease.
    assert jobs.reap(now=T0 + timedelta(seconds=100)) == []

    stored = jobs.get("job-1")
    assert stored is not None
    assert stored.status is JobStatus.RUNNING


def test_a_renewal_carries_the_running_stages_note_into_the_document(jobs: JobStore) -> None:
    """The one thing a stage can say while it is still running.

    Against a real document because the note has to survive two things that only
    exist here: `renew` re-reads the stored job and writes it whole, so a note
    set anywhere else would be read straight back over, and the document then has
    to validate on the way back in — a field the reader rejects would make the
    job unloadable rather than merely uninformative.
    """
    jobs.create(make_job())
    claimed = jobs.try_claim("job-1", now=T0)
    assert claimed is not None

    stages = list(claimed.stages)
    stages[0] = stages[0].model_copy(update={"status": StageStatus.RUNNING})
    jobs.apply(Transition(job=claimed.model_copy(update={"stages": stages}), events=()))

    jobs.renew("job-1", now=T0 + timedelta(seconds=30), progress="Fetching the track")

    stored = jobs.get("job-1")
    assert stored is not None
    assert stored.stages[0].progress == "Fetching the track"
    assert stored.stages[1].progress is None, "only the stage that is running is talking"


def test_a_stage_that_has_gone_quiet_stops_appearing_to_talk(jobs: JobStore) -> None:
    """A note is what the stage is saying *now*. The heartbeat that follows a
    cleared note has to take the old one back out, or a stage that finished
    fetching would go on reporting the fetch for as long as it ran."""
    jobs.create(make_job())
    claimed = jobs.try_claim("job-1", now=T0)
    assert claimed is not None
    stages = list(claimed.stages)
    stages[0] = stages[0].model_copy(update={"status": StageStatus.RUNNING})
    jobs.apply(Transition(job=claimed.model_copy(update={"stages": stages}), events=()))
    jobs.renew("job-1", now=T0 + timedelta(seconds=30), progress="Fetching the track")

    jobs.renew("job-1", now=T0 + timedelta(seconds=60))

    stored = jobs.get("job-1")
    assert stored is not None
    assert stored.stages[0].progress is None


def test_reaping_exhausts_attempts_and_fails_the_job(jobs: JobStore) -> None:
    jobs.create(make_job(attempts=2, max_attempts=3))
    jobs.try_claim("job-1", now=T0)

    jobs.reap(now=T0 + timedelta(seconds=200))

    stored = jobs.get("job-1")
    assert stored is not None
    assert stored.status is JobStatus.FAILED
    assert stored.error is not None
    assert stored.error.type == "LeaseExpired"


def test_a_reaped_job_can_be_claimed_by_another_worker(
    jobs: JobStore, other_worker: JobStore
) -> None:
    """The recovery path a crashed worker depends on."""
    jobs.create(make_job())
    jobs.try_claim("job-1", now=T0)
    jobs.reap(now=T0 + timedelta(seconds=200))

    reclaimed = other_worker.try_claim("job-1", now=T0 + timedelta(seconds=201))

    assert reclaimed is not None
    assert reclaimed.worker_id == "worker-b"


def test_reaping_records_both_events(jobs: JobStore) -> None:
    jobs.create(make_job())
    jobs.try_claim("job-1", now=T0)
    jobs.reap(now=T0 + timedelta(seconds=200))

    kinds = [e["kind"] for e in jobs.events("job-1")]

    assert kinds == [
        JobEventKind.CLAIMED.value,
        JobEventKind.LEASE_EXPIRED.value,
        JobEventKind.REQUEUED.value,
    ]


def test_the_reaper_handles_several_jobs_in_one_pass(jobs: JobStore) -> None:
    for i in range(3):
        jobs.create(make_job(f"job-{i}"))
        jobs.try_claim(f"job-{i}", now=T0)

    reaped = jobs.reap(now=T0 + timedelta(seconds=200))

    assert sorted(j.id for j in reaped) == ["job-0", "job-1", "job-2"]


# ─────────────────────────────────────────────────────────────────────────────
# Losing a lease
# ─────────────────────────────────────────────────────────────────────────────


def test_renewing_a_lease_that_was_stolen_returns_none(
    jobs: JobStore, other_worker: JobStore
) -> None:
    """The signal a worker must act on: it no longer owns the job and has to stop
    working on it, or two workers would write conflicting stage results."""
    jobs.create(make_job())
    jobs.try_claim("job-1", now=T0)
    jobs.reap(now=T0 + timedelta(seconds=200))
    other_worker.try_claim("job-1", now=T0 + timedelta(seconds=201))

    assert jobs.renew("job-1", now=T0 + timedelta(seconds=210)) is None


def test_renewing_a_missing_job_returns_none(jobs: JobStore) -> None:
    assert jobs.renew("gone", now=T0) is None


# ─────────────────────────────────────────────────────────────────────────────
# Heartbeats
# ─────────────────────────────────────────────────────────────────────────────


def test_a_heartbeat_round_trips(workers: WorkerStore) -> None:
    heartbeat = WorkerHeartbeat(
        worker_id="worker-a",
        uid="user-1",
        status=WorkerStatus.ONLINE,
        capabilities=WorkerCapabilities(whisper=True, llm=True, render=True, publish=False),
        version="0.0.1",
        last_seen_at=T0,
    )
    workers.announce(heartbeat)

    loaded = workers.get("worker-a")

    assert loaded == heartbeat
