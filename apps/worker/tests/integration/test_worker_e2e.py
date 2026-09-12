"""The worker, end to end against the emulator.

Phase 2, exit criteria 1, 2, 3 and 6. These are the tests that justify the
architecture: a job that survives its worker being killed, two workers that
cannot double-execute, and a Ctrl-C that hands work back instead of stranding it.

Criterion 2 uses a real killed subprocess rather than a simulated one. That
matters — the whole claim is that the worker survives `taskkill /F`, and a
simulated crash would still unwind the stack, run `finally` blocks and release
the lease, proving nothing. The ECHO stage calls `os._exit()` from inside itself,
which is the only way to reproduce a genuine mid-stage power cut without racing
an external kill against the stage's own progress.
"""

from __future__ import annotations

import collections
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from clipforge.config import Settings
from clipforge.models.broker import ModelBroker
from clipforge.models.vram import VramSnapshot
from clipforge.scheduler.worker import Worker
from clipforge.stages.base import StageContext, StageOutcome, StageRegistry
from clipforge.stages.echo import CRASH_ENV, RAN_ENV, EchoStage, new_echo_job
from clipforge.store.firestore import JobStore, WorkerStore
from clipforge_contracts import (
    JobEventKind,
    JobStatus,
    JobType,
    Lane,
    StageName,
    StageStatus,
    WorkerStatus,
)
from google.cloud import firestore

pytestmark = pytest.mark.integration

WORKER_ROOT = Path(__file__).resolve().parents[2]


def no_gpu_broker() -> ModelBroker:
    """A broker on a machine with no NVIDIA device — the CI configuration."""
    probe: object = lambda: None  # noqa: E731
    return ModelBroker(reserve_mb=700, probe=probe)  # type: ignore[arg-type]


def make_worker(
    client: firestore.Client,
    settings: Settings,
    workers: WorkerStore,
    worker_id: str,
    **kwargs: object,
) -> Worker:
    """Build a worker whose store claims jobs under the same id.

    Going through one helper is deliberate: a Worker whose JobStore claims under
    a different id would silently fail to recognise its own jobs on shutdown.
    """
    scoped = settings.model_copy(update={"worker_id": worker_id})
    return Worker(
        settings=scoped,
        jobs=JobStore(client, scoped),
        workers=workers,
        broker=no_gpu_broker(),
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.fixture
def worker(jobs: JobStore, workers: WorkerStore, settings: Settings) -> Worker:
    return Worker(
        settings=settings,
        jobs=jobs,
        workers=workers,
        broker=no_gpu_broker(),
        poll_interval_s=0.05,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 1 — a job flows QUEUED -> RUNNING -> COMPLETED
# ─────────────────────────────────────────────────────────────────────────────


def test_an_echo_job_runs_to_completion(jobs: JobStore, worker: Worker) -> None:
    """Phase 2, exit criterion 1."""
    jobs.create(new_echo_job(uid="user-1", job_id="echo-1"))

    finished = worker.run_once()

    assert finished is not None
    assert finished.status is JobStatus.COMPLETED
    assert finished.ended_at is not None
    assert [s.status for s in finished.stages] == [StageStatus.DONE] * 3


def test_every_stage_records_a_timing(jobs: JobStore, worker: Worker) -> None:
    jobs.create(new_echo_job(uid="user-1", job_id="echo-1"))

    finished = worker.run_once()

    assert finished is not None
    for stage in finished.stages:
        assert stage.duration_ms is not None, f"{stage.name.value} has no timing"
        assert stage.duration_ms >= 0
        assert stage.started_at is not None
        assert stage.ended_at is not None


def test_every_stage_persists_its_checkpoint(jobs: JobStore, worker: Worker) -> None:
    jobs.create(new_echo_job(uid="user-1", job_id="echo-1"))

    finished = worker.run_once()

    assert finished is not None
    assert [s.checkpoint for s in finished.stages] == [
        {"echoed": "ECHO_ONE"},
        {"echoed": "ECHO_TWO"},
        {"echoed": "ECHO_THREE"},
    ]


def test_the_event_log_tells_the_whole_story(jobs: JobStore, worker: Worker) -> None:
    """Append-only, and readable from the PWA — this is what makes a job that
    failed overnight diagnosable without worker logs."""
    jobs.create(new_echo_job(uid="user-1", job_id="echo-1"))

    worker.run_once()

    kinds = [e["kind"] for e in jobs.events("echo-1")]
    assert kinds == [
        JobEventKind.CLAIMED.value,
        JobEventKind.STAGE_STARTED.value,
        JobEventKind.STAGE_COMPLETED.value,
        JobEventKind.STAGE_STARTED.value,
        JobEventKind.STAGE_COMPLETED.value,
        JobEventKind.STAGE_STARTED.value,
        JobEventKind.STAGE_COMPLETED.value,
        JobEventKind.COMPLETED.value,
    ]


def test_running_with_an_empty_queue_is_not_an_error(worker: Worker) -> None:
    assert worker.run_once() is None


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 2 — survive being killed, resume where it stopped
# ─────────────────────────────────────────────────────────────────────────────


def _spawn_crashing_worker(ran_file: Path, crash_at: StageName) -> subprocess.CompletedProcess[str]:
    """Run a real worker in a real subprocess that dies inside a stage."""
    env = {
        **os.environ,
        "CLIPFORGE_USE_EMULATORS": "true",
        "CLIPFORGE_FIREBASE_PROJECT_ID": "demo-clipforge",
        "CLIPFORGE_FIRESTORE_EMULATOR_HOST": "127.0.0.1:8080",
        "CLIPFORGE_WORKER_ID": "crash-worker",
        # A short lease so the test does not wait 90 seconds for the corpse to
        # be reclaimable.
        "CLIPFORGE_LEASE_SECONDS": "5",
        "CLIPFORGE_LOG_FORMAT": "json",
        CRASH_ENV: crash_at.value,
        RAN_ENV: str(ran_file),
    }
    return subprocess.run(
        [sys.executable, "-m", "clipforge.cli", "run", "--once"],
        cwd=WORKER_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_a_killed_worker_resumes_without_repeating_finished_stages(
    jobs: JobStore,
    tmp_path: Path,
    settings: Settings,
    workers: WorkerStore,
    client: firestore.Client,
) -> None:
    """Phase 2, exit criterion 2.

    A worker is killed hard partway through stage 2. A second worker reclaims the
    job once the lease lapses and finishes it — and stage 1, which cost real time
    the first time round, is never re-executed.
    """
    ran_file = tmp_path / "ran.txt"
    jobs.create(new_echo_job(uid="user-1", job_id="crash-job"))

    crashed = _spawn_crashing_worker(ran_file, StageName.ECHO_TWO)

    # os._exit(137) — a genuine abrupt death, not a clean shutdown.
    assert crashed.returncode == 137, crashed.stderr[-2000:]

    mid_flight = jobs.get("crash-job")
    assert mid_flight is not None
    assert mid_flight.status is JobStatus.RUNNING, "a killed worker cannot tidy up after itself"
    assert mid_flight.worker_id == "crash-worker"
    assert mid_flight.stages[0].status is StageStatus.DONE
    assert mid_flight.stages[1].status is StageStatus.RUNNING

    ran_before = ran_file.read_text(encoding="utf-8").split()
    assert ran_before == [StageName.ECHO_ONE.value, StageName.ECHO_TWO.value]

    # Wait out the 5s lease the crashed worker took, then let a second worker in.
    resumed_at = mid_flight.lease_expires_at
    assert resumed_at is not None
    deadline = time.monotonic() + 30
    while datetime.now(UTC) <= resumed_at and time.monotonic() < deadline:
        time.sleep(0.2)

    os.environ[RAN_ENV] = str(ran_file)
    try:
        survivor = make_worker(client, settings, workers, "survivor")
        finished = survivor.run_once()
    finally:
        os.environ.pop(RAN_ENV, None)

    assert finished is not None
    assert finished.status is JobStatus.COMPLETED
    assert finished.worker_id is None

    ran = collections.Counter(ran_file.read_text(encoding="utf-8").split())
    assert ran[StageName.ECHO_ONE.value] == 1, "a DONE stage was re-executed on resume"
    assert ran[StageName.ECHO_TWO.value] == 2, "the interrupted stage should run again"
    assert ran[StageName.ECHO_THREE.value] == 1


def test_the_reclaim_costs_exactly_one_attempt(
    jobs: JobStore, tmp_path: Path, settings: Settings, workers: WorkerStore
) -> None:
    """A crash is a retry; three orderly restarts must not exhaust a job."""
    jobs.create(new_echo_job(uid="user-1", job_id="crash-job"))
    _spawn_crashing_worker(tmp_path / "ran.txt", StageName.ECHO_TWO)

    mid_flight = jobs.get("crash-job")
    assert mid_flight is not None
    assert mid_flight.attempts == 0, "the first claim of a QUEUED job is not a retry"

    lease_end = mid_flight.lease_expires_at
    assert lease_end is not None
    while datetime.now(UTC) <= lease_end:
        time.sleep(0.2)

    reclaimed = jobs.try_claim("crash-job")
    assert reclaimed is not None
    assert reclaimed.attempts == 1


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 3 — two workers, no double execution
# ─────────────────────────────────────────────────────────────────────────────


def test_two_workers_on_one_queue_never_double_execute(
    client: firestore.Client, settings: Settings, workers: WorkerStore, jobs: JobStore
) -> None:
    """Phase 2, exit criterion 3.

    Six jobs, two workers, run concurrently. Every job must complete exactly once
    — proven from the event log rather than from the final status, because a
    doubly-executed job would still *end* COMPLETED.
    """
    for i in range(6):
        jobs.create(new_echo_job(uid="user-1", job_id=f"job-{i}"))

    def make(worker_id: str) -> Worker:
        return Worker(
            settings=settings.model_copy(update={"worker_id": worker_id}),
            jobs=JobStore(client, settings.model_copy(update={"worker_id": worker_id})),
            workers=workers,
            broker=no_gpu_broker(),
            poll_interval_s=0.01,
        )

    def drain(worker: Worker) -> None:
        while worker.run_once() is not None:
            pass

    threads = [
        threading.Thread(target=drain, args=(make("worker-a"),)),
        threading.Thread(target=drain, args=(make("worker-b"),)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    for i in range(6):
        job = jobs.get(f"job-{i}")
        assert job is not None
        assert job.status is JobStatus.COMPLETED, f"job-{i} is {job.status.value}"

        kinds = collections.Counter(e["kind"] for e in jobs.events(f"job-{i}"))
        assert kinds[JobEventKind.CLAIMED.value] == 1, f"job-{i} was claimed twice"
        assert kinds[JobEventKind.STAGE_COMPLETED.value] == 3, f"job-{i} ran stages twice"
        assert kinds[JobEventKind.COMPLETED.value] == 1


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 6 — Ctrl-C hands the work back
# ─────────────────────────────────────────────────────────────────────────────


class BlockingStage:
    """A stage that runs until asked to stop, then checkpoints and yields."""

    name = StageName.ECHO_ONE
    lane = Lane.CPU

    def __init__(self) -> None:
        self.entered = threading.Event()

    def run(self, context: StageContext) -> StageOutcome:
        self.entered.set()
        while not context.stopping():
            time.sleep(0.01)
        # Incomplete: it stopped on request rather than finishing, so the runner
        # must leave it PENDING for the next attempt.
        return StageOutcome(checkpoint={"progress": "partial"}, incomplete=True)


@pytest.fixture
def blocking_registry() -> Iterator[tuple[StageRegistry, BlockingStage]]:
    blocking = BlockingStage()
    registry = StageRegistry()
    registry.register(blocking)
    registry.register(EchoStage(name=StageName.ECHO_TWO, lane=Lane.CPU))
    registry.register(EchoStage(name=StageName.ECHO_THREE, lane=Lane.GPU))
    yield registry, blocking


def test_stopping_releases_the_lease_and_goes_offline(
    jobs: JobStore,
    workers: WorkerStore,
    settings: Settings,
    client: firestore.Client,
    blocking_registry: tuple[StageRegistry, BlockingStage],
) -> None:
    """Phase 2, exit criterion 6.

    Returning the job to QUEUED rather than letting the lease lapse is the point:
    another worker can take it immediately instead of after 90 seconds of nothing
    appearing to happen.
    """
    registry, blocking = blocking_registry
    jobs.create(new_echo_job(uid="user-1", job_id="stop-job"))

    worker = make_worker(
        client,
        settings,
        workers,
        "stopper",
        poll_interval_s=0.01,
        registry_factory=lambda _job_type: registry,
    )

    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()

    assert blocking.entered.wait(timeout=30), "the worker never started the job"

    started = time.monotonic()
    worker.request_stop()
    thread.join(timeout=30)
    elapsed = time.monotonic() - started

    assert not thread.is_alive(), "the worker did not shut down"
    assert elapsed < 2.0, f"shutdown took {elapsed:.2f}s; the budget is 2s"

    released = jobs.get("stop-job")
    assert released is not None
    assert released.status is JobStatus.QUEUED, "the job was stranded rather than handed back"
    assert released.worker_id is None
    assert released.lease_expires_at is None
    assert released.attempts == 0, "a clean shutdown is a handover, not a failed attempt"

    # The partial work is retained, so the next attempt does not start over.
    assert released.stages[0].status is StageStatus.PENDING
    assert released.stages[0].checkpoint == {"progress": "partial"}

    heartbeat = workers.get("stopper")
    assert heartbeat is not None
    assert heartbeat.status is WorkerStatus.OFFLINE


def test_a_released_job_is_immediately_claimable_by_another_worker(
    jobs: JobStore,
    workers: WorkerStore,
    settings: Settings,
    client: firestore.Client,
    blocking_registry: tuple[StageRegistry, BlockingStage],
) -> None:
    registry, blocking = blocking_registry
    jobs.create(new_echo_job(uid="user-1", job_id="stop-job"))

    worker = make_worker(
        client,
        settings,
        workers,
        "stopper",
        poll_interval_s=0.01,
        registry_factory=lambda _job_type: registry,
    )
    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()
    assert blocking.entered.wait(timeout=30)
    worker.request_stop()
    thread.join(timeout=30)

    # No waiting for a lease to lapse.
    assert jobs.try_claim("stop-job") is not None


# ─────────────────────────────────────────────────────────────────────────────
# Heartbeat
# ─────────────────────────────────────────────────────────────────────────────


def test_the_worker_advertises_itself_and_its_capabilities(
    workers: WorkerStore, settings: Settings, client: firestore.Client
) -> None:
    worker = make_worker(client, settings, workers, "announcer", poll_interval_s=0.01)
    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()

    deadline = time.monotonic() + 30
    heartbeat = None
    while time.monotonic() < deadline:
        heartbeat = workers.get("announcer")
        if heartbeat is not None:
            break
        time.sleep(0.05)

    worker.request_stop()
    thread.join(timeout=30)

    assert heartbeat is not None
    assert heartbeat.hostname
    assert heartbeat.capabilities.render is True
    assert heartbeat.capabilities.publish is False, "publishing is off by default"


# ─────────────────────────────────────────────────────────────────────────────
# Failure handling
# ─────────────────────────────────────────────────────────────────────────────


class ExplodingStage:
    name = StageName.ECHO_TWO
    lane = Lane.CPU

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls = 0

    def run(self, context: StageContext) -> StageOutcome:
        self.calls += 1
        raise self._exc


def test_a_retryable_stage_failure_requeues_the_job_and_keeps_earlier_work(
    jobs: JobStore, workers: WorkerStore, settings: Settings
) -> None:
    exploding = ExplodingStage(TimeoutError("ollama did not respond"))
    registry = StageRegistry()
    registry.register(EchoStage(name=StageName.ECHO_ONE, lane=Lane.CPU))
    registry.register(exploding)
    registry.register(EchoStage(name=StageName.ECHO_THREE, lane=Lane.GPU))

    jobs.create(new_echo_job(uid="user-1", job_id="fail-job"))
    worker = Worker(
        settings=settings,
        jobs=jobs,
        workers=workers,
        broker=no_gpu_broker(),
        registry_factory=lambda _t: registry,
    )
    worker.run_once()

    job = jobs.get("fail-job")
    assert job is not None
    assert job.status is JobStatus.QUEUED
    assert job.attempts == 1
    assert job.stages[0].status is StageStatus.DONE, "completed work must survive a later failure"
    assert job.stages[1].status is StageStatus.FAILED
    assert job.stages[1].error is not None
    assert job.stages[1].error.type == "TimeoutError"
    assert job.stages[1].error.retryable is True


def test_a_non_retryable_failure_fails_the_job_immediately(
    jobs: JobStore, workers: WorkerStore, settings: Settings
) -> None:
    """Burning two more attempts on a malformed input wastes time to learn
    nothing."""
    registry = StageRegistry()
    registry.register(EchoStage(name=StageName.ECHO_ONE, lane=Lane.CPU))
    registry.register(ExplodingStage(ValueError("not a YouTube URL")))
    registry.register(EchoStage(name=StageName.ECHO_THREE, lane=Lane.GPU))

    jobs.create(new_echo_job(uid="user-1", job_id="fail-job"))
    Worker(
        settings=settings,
        jobs=jobs,
        workers=workers,
        broker=no_gpu_broker(),
        registry_factory=lambda _t: registry,
    ).run_once()

    job = jobs.get("fail-job")
    assert job is not None
    assert job.status is JobStatus.FAILED
    assert job.attempts == 1
    assert job.error is not None
    assert job.error.retryable is False


def test_an_unimplemented_job_type_is_reported_clearly(
    jobs: JobStore, workers: WorkerStore, settings: Settings, worker: Worker
) -> None:
    """A CLIP job today should say so, not fail with a KeyError inside the
    runner."""
    clip_job = new_echo_job(uid="user-1", job_id="clip-job").model_copy(
        update={"type": JobType.CLIP}
    )
    jobs.create(clip_job)

    worker.run_once()

    job = jobs.get("clip-job")
    assert job is not None
    assert job.status is JobStatus.FAILED
    assert job.error is not None
    assert job.error.type == "NotImplementedError"
    assert "CLIP" in job.error.message
    assert job.error.retryable is False, "retrying would produce the identical error twice more"


def test_vram_snapshot_reaches_the_heartbeat_when_a_gpu_exists(
    workers: WorkerStore,
    settings: Settings,
    client: firestore.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The PWA uses this to explain a stalled job, so it has to be populated."""
    monkeypatch.setattr(
        "clipforge.scheduler.worker.probe_vram",
        lambda: VramSnapshot(
            device_name="NVIDIA GeForce RTX 3050",
            total_mb=6144,
            free_mb=2386,
            used_mb=3758,
        ),
    )
    worker = make_worker(client, settings, workers, "gpu-worker", poll_interval_s=0.01)
    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while workers.get("gpu-worker") is None and time.monotonic() < deadline:
        time.sleep(0.05)
    worker.request_stop()
    thread.join(timeout=30)

    heartbeat = workers.get("gpu-worker")
    assert heartbeat is not None
    assert heartbeat.gpu is not None
    assert heartbeat.gpu.vram_total_mb == 6144
    assert heartbeat.gpu.vram_free_mb == 2386
