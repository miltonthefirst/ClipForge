"""The worker touches Firestore when something happens, and not otherwise.

Every read and write is billed, and every write is billed again as a read on
each listener open on the document. The worker used to poll its queue every
second and re-announce itself every thirty, which cost tens of thousands of
reads a day on a machine doing nothing. These tests pin the replacement: a
listener feeds a local view, local alarms stand in for timers, and an idle
worker makes no request at all.
See docs/adr/0030-status-on-events-and-pings.md.

Run against stores that record every call, rather than the emulator: what is
under test is *whether* the worker asks, which a fake answers exactly. The
emulator tier proves the same listener against real Firestore.
"""

# The fakes below stand in for the stores by shape, not by type.
# mypy: disable-error-code="arg-type"

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Collection, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from clipforge.config import Settings
from clipforge.models.broker import ModelBroker
from clipforge.scheduler import lease
from clipforge.scheduler.worker import Worker, claimable, next_lapse, next_not_before
from clipforge.stages.echo import new_echo_job
from clipforge.store.firestore import QueuedJob
from clipforge_contracts import Job, JobStatus, JobType, WorkerHeartbeat, WorkerStatus

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
V1 = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)
V2 = datetime(2026, 10, 13, 8, 5, tzinfo=UTC)


def queued(
    job_id: str, *, version: datetime | None = V1, not_before: datetime | None = None
) -> QueuedJob:
    job = new_echo_job(uid="user-1", job_id=job_id).model_copy(update={"not_before": not_before})
    return QueuedJob(job=job, version=version)


def running(job_id: str, *, expires: datetime | None) -> Job:
    job = lease.claim(
        new_echo_job(uid="user-1", job_id=job_id), worker_id="other", now=NOW, lease_seconds=90
    ).job
    return job.model_copy(update={"lease_expires_at": expires})


# ── The view, as arithmetic ──────────────────────────────────────────────────


def test_a_due_job_is_claimable() -> None:
    assert [q.job.id for q in claimable([queued("a")], {}, NOW)] == ["a"]


def test_a_scheduled_job_is_not_claimable_before_its_time() -> None:
    later = queued("later", not_before=NOW + timedelta(minutes=5))
    assert claimable([later], {}, NOW) == []
    assert claimable([later], {}, NOW + timedelta(minutes=5)) == [later]


def test_a_refused_job_is_not_tried_again_until_it_changes() -> None:
    """Another worker won it; it stays in the view until the listener says it
    left. Trying it on every pass would be a transaction per pass for an
    answer already known. A new version — released, resubmitted — is a new
    question."""
    refused = {"a": V1}
    assert claimable([queued("a", version=V1)], refused, NOW) == []
    assert [q.job.id for q in claimable([queued("a", version=V2)], refused, NOW)] == ["a"]


def test_the_queue_order_is_kept() -> None:
    view = [queued("old"), queued("new")]
    assert [q.job.id for q in claimable(view, {}, NOW)] == ["old", "new"]


def test_the_alarm_for_a_scheduled_job_is_its_time() -> None:
    view = [
        queued("now"),
        queued("soon", not_before=NOW + timedelta(minutes=2)),
        queued("later", not_before=NOW + timedelta(hours=2)),
    ]
    assert next_not_before(view, NOW) == NOW + timedelta(minutes=2)


def test_no_scheduled_job_means_no_alarm() -> None:
    assert next_not_before([queued("now")], NOW) is None


def test_the_reaper_alarm_is_the_earliest_lease_someone_else_holds() -> None:
    jobs = [
        running("mine", expires=NOW + timedelta(seconds=10)),
        running("theirs", expires=NOW + timedelta(seconds=60)),
        running("lapsed", expires=NOW - timedelta(seconds=1)),
    ]
    assert next_lapse(jobs, skip={"mine"}, now=NOW) == NOW + timedelta(seconds=60)


def test_no_lease_to_wait_for_means_no_reaper_alarm() -> None:
    """The common case on a one-machine install: nothing is running anywhere,
    so the reaper has no reason to look again until something happens."""
    assert next_lapse([], skip=(), now=NOW) is None


# ── A worker against recording stores ───────────────────────────────────────


class Recorder:
    """Every call a store received, in order.

    Counted by position rather than by clock: Windows' monotonic clock ticks
    every 15 ms or so, and a call made in the same tick as a mark would count
    as both before and after it.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def note(self, name: str) -> None:
        with self.lock:
            self.calls.append(name)

    def mark(self) -> int:
        with self.lock:
            return len(self.calls)

    def since(self, mark: int) -> list[str]:
        with self.lock:
            return self.calls[mark:]

    def names(self) -> list[str]:
        with self.lock:
            return list(self.calls)


class FakeSubscription:
    def __init__(self) -> None:
        self.dead = False

    def alive(self) -> bool:
        return not self.dead

    def close(self) -> None:
        pass


class FakeJobStore:
    worker_id = "worker-1"

    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder
        self.deliver: Callable[[list[QueuedJob]], None] | None = None
        self.subscriptions: list[FakeSubscription] = []
        self.lost: set[str] = set()
        self.running_jobs: list[Job] = []

    def watch_queued(self, on_change: Callable[[list[QueuedJob]], None]) -> FakeSubscription:
        self.recorder.note("watch_queued")
        self.deliver = on_change
        subscription = FakeSubscription()
        self.subscriptions.append(subscription)
        return subscription

    def try_claim(self, job_id: str, *, now: datetime | None = None) -> Job | None:
        self.recorder.note(f"try_claim:{job_id}")
        if job_id in self.lost:
            return None
        job = new_echo_job(uid="user-1", job_id=job_id)
        return lease.claim(job, worker_id=self.worker_id, now=NOW, lease_seconds=90).job

    def claim_next(self, *, now: datetime | None = None) -> Job | None:
        self.recorder.note("claim_next")
        return None

    def running(self, limit: int = 50) -> list[Job]:
        self.recorder.note("running")
        return list(self.running_jobs)

    def reap(
        self,
        *,
        now: datetime | None = None,
        skip: Collection[str] = (),
        candidates: Sequence[Job] | None = None,
    ) -> list[Job]:
        self.recorder.note("reap")
        return []

    def apply(self, transition: Any) -> None:
        self.recorder.note("apply")

    def get(self, job_id: str) -> Job | None:
        self.recorder.note("get")
        return None

    def renew(
        self, job_id: str, *, now: datetime | None = None, progress: str | None = None
    ) -> Job | None:
        self.recorder.note("renew")
        return None


class FakeWorkerStore:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder
        self.beats: list[WorkerHeartbeat] = []

    def announce(self, heartbeat: WorkerHeartbeat) -> None:
        self.recorder.note(f"announce:{heartbeat.status.value}")
        self.beats.append(heartbeat)


def unrunnable(_job_type: JobType) -> Any:
    """A job type this worker cannot run: claimed, rejected, over in a moment."""
    raise NotImplementedError("not in this test")


def build(recorder: Recorder, **settings: Any) -> tuple[Worker, FakeJobStore, FakeWorkerStore]:
    jobs = FakeJobStore(recorder)
    workers = FakeWorkerStore(recorder)
    # model_copy rather than the constructor: these intervals are below what
    # the settings validator allows, which is the point of a fast test.
    tuned = Settings(use_emulators=True).model_copy(
        update={"listener_check_seconds": 0.1, "heartbeat_seconds": 0.1, **settings}
    )
    worker = Worker(
        settings=tuned,
        jobs=jobs,
        workers=workers,
        broker=ModelBroker(reserve_mb=0),
        registry_factory=unrunnable,
    )
    return worker, jobs, workers


def wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


@pytest.fixture
def started() -> Any:
    recorder = Recorder()
    worker, jobs, workers = build(recorder)
    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()
    wait_for(lambda: jobs.deliver is not None)
    # The listener's first delivery: an empty queue.
    assert jobs.deliver is not None
    jobs.deliver([])
    time.sleep(0.2)
    yield recorder, worker, jobs, workers
    worker.request_stop()
    thread.join(timeout=10)
    assert not thread.is_alive()


def test_starting_up_announces_reaps_and_listens(started: Any) -> None:
    recorder, *_ = started
    assert recorder.names()[:4] == ["announce:ONLINE", "running", "reap", "watch_queued"]


def test_an_idle_worker_asks_firestore_nothing(started: Any) -> None:
    """The bill this exists to stop. With the listener check, the lease loop and
    the claim loop all running every tenth of a second, a second of idling must
    still produce no call at all — every one of those is local."""
    recorder, *_ = started
    quiet_from = recorder.mark()

    time.sleep(1.0)

    assert recorder.since(quiet_from) == []


def test_a_job_arriving_is_claimed_from_the_delivery_without_a_query(started: Any) -> None:
    recorder, _worker, jobs, workers = started
    from_now = recorder.mark()

    jobs.deliver([queued("job-1")])
    wait_for(lambda: "reap" in recorder.since(from_now))

    calls = recorder.since(from_now)
    assert calls[0] == "try_claim:job-1"
    assert "claim_next" not in calls, "the queue was polled rather than read from the delivery"
    assert "announce:BUSY" in calls, "a claim is an event and should say so"
    # The job ended (rejected as unrunnable): the reaper ran, and the worker
    # said it was idle again.
    assert "running" in calls
    assert workers.beats[-1].status is WorkerStatus.ONLINE

    settled = recorder.mark()
    time.sleep(0.5)
    assert recorder.since(settled) == [], "the worker kept asking after the job ended"


def test_a_job_another_worker_won_is_not_tried_on_every_pass(started: Any) -> None:
    recorder, _worker, jobs, _workers = started
    jobs.lost.add("job-1")
    from_now = recorder.mark()

    jobs.deliver([queued("job-1")])
    time.sleep(0.5)

    assert recorder.since(from_now) == ["try_claim:job-1"]


def test_a_ping_is_answered_with_the_token_then_reaps_and_sweeps(started: Any) -> None:
    """The answer is the worker document carrying the token. The sweep is the
    old poll, run once: a ping is the moment to be sure the listener has not
    quietly stopped delivering."""
    recorder, worker, _jobs, workers = started
    from_now = recorder.mark()

    worker.ping("tok-1")
    wait_for(lambda: "claim_next" in recorder.since(from_now))

    assert recorder.since(from_now) == ["announce:ONLINE", "running", "reap", "claim_next"]
    assert workers.beats[-1].answered_ping == "tok-1"


def test_a_dead_listener_is_replaced(started: Any) -> None:
    recorder, _worker, jobs, _workers = started
    from_now = recorder.mark()

    jobs.subscriptions[0].dead = True
    wait_for(lambda: len(jobs.subscriptions) == 2)

    assert recorder.since(from_now) == ["watch_queued"]


def test_a_lease_someone_else_holds_sets_an_alarm_not_a_timer() -> None:
    """Another machine is mid-job: the reaper looks again when its lease is due
    to lapse — not every minute in between, and not at all if none is held."""
    recorder = Recorder()
    worker, jobs, _workers = build(recorder)
    jobs.running_jobs = [running("theirs", expires=datetime.now(UTC) + timedelta(seconds=30))]

    worker._housekeep()

    assert worker._reap_at is not None
    assert worker._reap_at > datetime.now(UTC) + timedelta(seconds=29)

    jobs.running_jobs = []
    worker._housekeep()
    assert worker._reap_at is None


def test_progress_is_announced_only_when_a_stage_says_something_new() -> None:
    """At most once per renewal round, and only when there is news: a long
    stage repeating itself is not a reason to write the worker document."""
    recorder = Recorder()
    worker, _jobs, workers = build(recorder)
    job = new_echo_job(uid="user-1", job_id="job-1")
    worker._active[job.id] = job

    worker._announce(WorkerStatus.BUSY, ["job-1"])
    worker._report_progress(["job-1"])
    worker._report_progress(["job-1"])
    assert len(workers.beats) == 1

    from clipforge.stages.base import StageProgress

    notes = StageProgress()
    worker._notes[job.id] = notes
    notes.set("transcribing 40%")
    worker._report_progress(["job-1"])
    worker._report_progress(["job-1"])
    assert len(workers.beats) == 2


def test_a_job_the_worker_cannot_run_does_not_stay_active() -> None:
    """Rejected jobs used to stay in the active set, so the worker renewed a
    lease it no longer held — and called itself BUSY — for as long as it ran."""
    recorder = Recorder()
    worker, _jobs, _workers = build(recorder)
    job = new_echo_job(uid="user-1", job_id="job-1")
    worker._active[job.id] = job

    worker._execute(job)

    assert worker.active_job_ids() == []
    assert JobStatus.QUEUED is job.status
