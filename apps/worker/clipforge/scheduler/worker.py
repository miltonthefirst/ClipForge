"""The worker process: claim, run, heartbeat, reap, shut down cleanly.

Concurrency model, from docs/PLAN.md §2.1. Jobs run in a small thread pool sized
by ``CLIPFORGE_CPU_LANE_DEPTH``, so a download or a render proceeds while another
job transcribes. The **GPU lane is not a separate pool** — it is the
:class:`ModelBroker`'s lock. Any stage wanting the GPU acquires the broker, and
because at most one model may be resident in 6 GB, that lock *is* the depth-1 GPU
lane. Modelling it as a lock rather than a second queue means a job never has to
be handed between pools mid-flight.

Three background concerns run alongside:

- **Heartbeat.** Every ``heartbeat_seconds`` the worker renews the lease on each
  job it is running and refreshes ``workers/{workerId}``. The heartbeat interval
  must be comfortably shorter than the lease or every job would be reaped
  mid-flight; ``Settings.heartbeat_fits_in_lease()`` checks it at startup.
- **Reaper.** The same pure ``lease.reap`` bound to a periodic task rather than a
  Cloud Function. See docs/adr/0006-lease-based-job-claiming.md.
- **Shutdown.** SIGINT/SIGTERM asks running stages to stop, returns their jobs to
  the queue, and flips the heartbeat to OFFLINE — so another worker can pick the
  work up immediately instead of waiting out a 90-second lease.
"""

from __future__ import annotations

import contextlib
import signal
import socket
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from types import FrameType

from clipforge_contracts import (
    GpuInfo,
    Job,
    JobStatus,
    JobType,
    StageError,
    WorkerCapabilities,
    WorkerHeartbeat,
    WorkerStatus,
)

from clipforge.config import Settings
from clipforge.models.broker import ModelBroker
from clipforge.models.vram import probe_vram
from clipforge.observability import bind_job, get_logger
from clipforge.scheduler import lease
from clipforge.scheduler.runner import LeaseLostError, StageRunner
from clipforge.stages.base import StageRegistry
from clipforge.stages.echo import registry_for
from clipforge.store.firestore import JobStore, WorkerStore
from clipforge.version import __version__

log = get_logger(__name__)

__all__ = ["Worker"]


class Worker:
    """One worker process."""

    def __init__(
        self,
        *,
        settings: Settings,
        jobs: JobStore,
        workers: WorkerStore,
        broker: ModelBroker | None = None,
        uid: str = "local",
        poll_interval_s: float = 1.0,
        registry_factory: Callable[[JobType], StageRegistry] = registry_for,
    ) -> None:
        self._settings = settings
        self._jobs = jobs
        self._workers = workers
        self._broker = broker or ModelBroker(reserve_mb=settings.vram_reserve_mb)
        self._uid = uid
        self._poll_interval_s = poll_interval_s
        # Injected so tests can supply stage behaviour that is awkward to
        # provoke with the real stages, and so Phases 3-6 can register the CLIP
        # pipeline without editing the worker.
        self._registry_factory = registry_factory

        self._stop = threading.Event()
        self._active: dict[str, Job] = {}
        self._active_lock = threading.Lock()
        self._started_at = datetime.now(UTC)
        self._threads: list[threading.Thread] = []

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def install_signal_handlers(self) -> None:
        """Ask for a clean stop on Ctrl-C or a termination request.

        Windows delivers SIGINT for Ctrl-C but has no SIGTERM in the POSIX sense,
        so SIGTERM is registered defensively rather than assumed. Getting this
        wrong is a documented Phase 2 risk — the shutdown path is exercised on
        Windows now rather than discovered at release time.
        """

        def _handle(signum: int, _frame: FrameType | None) -> None:
            log.info("worker.signal", signal=signum)
            self.request_stop()

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            with contextlib.suppress(ValueError, OSError):
                # ValueError: not on the main thread. OSError: unsupported here.
                signal.signal(sig, _handle)

    def request_stop(self) -> None:
        self._stop.set()

    def run_once(self) -> Job | None:
        """Claim and run a single job, then return it. ``None`` if the queue was
        empty. A smoke-test and debugging entry point — it skips the heartbeat
        and reaper loops, so it must not be used for real work."""
        claimed = self._jobs.claim_next()
        if claimed is None:
            return None
        with self._active_lock:
            self._active[claimed.id] = claimed
        self._execute(claimed)
        return self._jobs.get(claimed.id)

    def run_forever(self) -> None:
        """Claim and run jobs until asked to stop."""
        if not self._settings.heartbeat_fits_in_lease():
            log.warning(
                "worker.heartbeat_too_slow",
                heartbeat_seconds=self._settings.heartbeat_seconds,
                lease_seconds=self._settings.lease_seconds,
                hint="every job will be reaped mid-flight; shorten the heartbeat",
            )

        self._announce(WorkerStatus.ONLINE)
        log.info(
            "worker.started",
            worker_id=self._jobs.worker_id,
            cpu_lane_depth=self._settings.cpu_lane_depth,
            version=__version__,
        )

        self._spawn(self._heartbeat_loop, "heartbeat")
        self._spawn(self._reaper_loop, "reaper")

        pool = ThreadPoolExecutor(
            max_workers=self._settings.cpu_lane_depth, thread_name_prefix="job"
        )
        running: set[Future[None]] = set()

        try:
            while not self._stop.is_set():
                running = {f for f in running if not f.done()}

                if len(running) >= self._settings.cpu_lane_depth:
                    self._stop.wait(self._poll_interval_s)
                    continue

                claimed = self._jobs.claim_next()
                if claimed is None:
                    self._stop.wait(self._poll_interval_s)
                    continue

                with self._active_lock:
                    self._active[claimed.id] = claimed
                running.add(pool.submit(self._execute, claimed))
        finally:
            self._shutdown(pool, running)

    # ── Job execution ────────────────────────────────────────────────────────

    def _execute(self, job: Job) -> None:
        try:
            registry = self._registry_factory(job.type)
        except Exception as exc:  # noqa: BLE001 - a registry factory may raise anything
            # A job type this worker cannot run at all — a CLIP job before the
            # pipeline exists, say. Fail it non-retryably rather than letting the
            # lease lapse: retrying would produce the identical error twice more
            # and leave the PWA showing a job that looks merely slow.
            self._reject(job, exc)
            return

        runner = StageRunner(
            store=self._jobs,
            registry=registry,
            settings=self._settings,
            broker=self._broker,
        )
        # A job stays in `_active` only while this worker is still responsible
        # for it. Popping unconditionally would be wrong on the shutdown path:
        # a stage that stopped early leaves the job RUNNING and still ours, and
        # forgetting it here is exactly how it would get stranded until the
        # reaper noticed 90 seconds later.
        still_ours = False
        try:
            final = runner.run(job, self._stop)
            still_ours = final.status is JobStatus.RUNNING
            log.info("job.finished", job_id=job.id, status=final.status.value)
        except LeaseLostError:
            # Already logged by the runner. Another worker owns this now, so it
            # must not be released — that would take it away from them.
            pass
        except Exception:
            # A failure the runner could not attribute to a stage — a bug in the
            # scheduler itself. Let the lease lapse so the reaper recovers the
            # job rather than this worker guessing at its state.
            log.exception("job.crashed", job_id=job.id)
        finally:
            if not still_ours:
                with self._active_lock:
                    self._active.pop(job.id, None)

    def _reject(self, job: Job, exc: Exception) -> None:
        """Fail a job this worker cannot run, with the reason recorded."""
        error = StageError(
            type=type(exc).__name__, message=str(exc), traceback=None, retryable=False
        )
        with bind_job(job_id=job.id):
            log.warning("job.unrunnable", error=str(exc))
            with contextlib.suppress(Exception):
                self._jobs.apply(lease.fail_stage(job, error=error, now=datetime.now(UTC)))

    # ── Background loops ─────────────────────────────────────────────────────

    def _spawn(self, target: object, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)  # type: ignore[arg-type]
        thread.start()
        self._threads.append(thread)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self._settings.heartbeat_seconds):
            with self._active_lock:
                active = list(self._active)

            for job_id in active:
                if self._jobs.renew(job_id) is None:
                    log.warning("lease.lost_during_heartbeat", job_id=job_id)

            self._announce(WorkerStatus.BUSY if active else WorkerStatus.ONLINE, active)

    def _reaper_loop(self) -> None:
        while not self._stop.wait(self._settings.reaper_interval_seconds):
            try:
                reaped = self._jobs.reap()
            except Exception:
                log.exception("reaper.failed")
                continue
            if reaped:
                log.info("reaper.reclaimed", jobs=[j.id for j in reaped])

    # ── Heartbeat document ───────────────────────────────────────────────────

    def _announce(self, status: WorkerStatus, active: list[str] | None = None) -> None:
        snapshot = probe_vram()
        gpu = (
            GpuInfo(
                name=snapshot.device_name,
                vram_total_mb=snapshot.total_mb,
                vram_free_mb=snapshot.free_mb,
            )
            if snapshot
            else None
        )

        try:
            self._workers.announce(
                WorkerHeartbeat(
                    worker_id=self._jobs.worker_id,
                    uid=self._uid,
                    status=status,
                    capabilities=WorkerCapabilities(
                        # Advertised from configuration rather than probed: the
                        # PWA uses this to explain a stalled job, and "the worker
                        # believes it can transcribe" is the useful claim.
                        whisper=snapshot is not None,
                        llm=snapshot is not None,
                        render=True,
                        publish=False,
                    ),
                    gpu=gpu,
                    version=__version__,
                    hostname=socket.gethostname(),
                    active_job_ids=active or [],
                    last_seen_at=datetime.now(UTC),
                    started_at=self._started_at,
                )
            )
        except Exception:
            # A heartbeat failure must never take the worker down; the lease is
            # what actually protects correctness.
            log.exception("heartbeat.failed")

    # ── Shutdown ─────────────────────────────────────────────────────────────

    def _shutdown(self, pool: ThreadPoolExecutor, running: set[Future[None]]) -> None:
        log.info("worker.stopping")
        self._stop.set()

        deadline = time.monotonic() + 2.0
        for future in list(running):
            remaining = max(0.0, deadline - time.monotonic())
            with contextlib.suppress(Exception):
                future.result(timeout=remaining)

        pool.shutdown(wait=False, cancel_futures=True)
        self._release_active()
        self._announce(WorkerStatus.OFFLINE)
        log.info("worker.stopped")

    def _release_active(self) -> None:
        """Return still-held jobs to the queue so another worker can take them.

        Re-read before releasing: the in-memory copy is from claim time and its
        stage results are stale, so writing it back would erase everything the
        run just accomplished.
        """
        with self._active_lock:
            job_ids = list(self._active)
            self._active.clear()

        for job_id in job_ids:
            current = self._jobs.get(job_id)
            if current is None or current.status is not JobStatus.RUNNING:
                continue
            if current.worker_id != self._jobs.worker_id:
                continue
            with bind_job(job_id=job_id):
                try:
                    self._jobs.apply(lease.release(current, now=datetime.now(UTC)))
                    log.info("job.released")
                except Exception:
                    log.exception("job.release_failed")
