"""The lease renewal round, and why it must never raise.

The heartbeat is what tells the rest of the system that a job still has an
owner. It runs on its own thread, and a thread that dies from an unhandled
exception does not come back — so a single transient Firestore error would stop
every future renewal, for every job, for the life of the process.

That is not hypothetical. A `503 UNAVAILABLE` during a slow clip upload killed
the thread; every lease then lapsed, the reaper in the same worker reclaimed the
job it was still running, and a finished render was done again from the top —
re-encoding and re-uploading two clips that already existed.

So these are about one guarantee: this returns, whatever the store does.
"""

from __future__ import annotations

import pytest
from clipforge.config import Settings
from clipforge.models.broker import ModelBroker
from clipforge.scheduler.worker import Worker


class ExplodingJobStore:
    """A store that fails renewals the way a bad network does."""

    worker_id = "worker-1"

    def __init__(self, *, error: Exception | None = None, lost: bool = False) -> None:
        self._error = error
        self._lost = lost
        self.attempts: list[str] = []

    def renew(self, job_id: str) -> object | None:
        self.attempts.append(job_id)
        if self._error is not None:
            raise self._error
        return None if self._lost else object()


class SilentWorkerStore:
    def announce(self, heartbeat: object) -> None:
        return None


def build(jobs: object) -> Worker:
    return Worker(
        settings=Settings(use_emulators=True),
        jobs=jobs,  # type: ignore[arg-type]
        workers=SilentWorkerStore(),  # type: ignore[arg-type]
        broker=ModelBroker(reserve_mb=0),
    )


@pytest.mark.unit
def test_a_transient_failure_does_not_end_the_renewal_round() -> None:
    """The failure that cost a duplicate render. One 503 must not be fatal."""
    jobs = ExplodingJobStore(error=ConnectionError("failed to connect to all addresses"))
    worker = build(jobs)

    worker.renew_active(["job-1"])  # must not raise

    assert jobs.attempts == ["job-1"]


@pytest.mark.unit
def test_one_jobs_failure_does_not_skip_the_others() -> None:
    """Guarded per job, not per round: two jobs share this thread, and the
    second must still get its lease renewed when the first cannot."""
    jobs = ExplodingJobStore(error=TimeoutError("deadline exceeded"))
    worker = build(jobs)

    worker.renew_active(["job-1", "job-2", "job-3"])

    assert jobs.attempts == ["job-1", "job-2", "job-3"]


@pytest.mark.unit
def test_a_genuinely_lost_lease_is_still_reported_rather_than_raised() -> None:
    """`None` means someone else owns this job now — a real answer, not an
    error. It is logged and the round continues; the runner abandons the job at
    its next checkpoint."""
    jobs = ExplodingJobStore(lost=True)
    worker = build(jobs)

    worker.renew_active(["job-1", "job-2"])

    assert jobs.attempts == ["job-1", "job-2"]


@pytest.mark.unit
def test_nothing_active_is_not_a_special_case() -> None:
    jobs = ExplodingJobStore(error=RuntimeError("should never be called"))
    worker = build(jobs)

    worker.renew_active([])

    assert jobs.attempts == []
