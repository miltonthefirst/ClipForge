"""The worker process: claim, run, renew, reap, shut down cleanly.

Concurrency model, from docs/PLAN.md §2.1. Jobs run in a small thread pool sized
by ``CLIPFORGE_CPU_LANE_DEPTH``, so a download or a render proceeds while another
job transcribes. The **GPU lane is not a separate pool** — it is the
:class:`ModelBroker`'s lock. Any stage wanting the GPU acquires the broker, and
because at most one model may be resident in 6 GB, that lock *is* the depth-1 GPU
lane. Modelling it as a lock rather than a second queue means a job never has to
be handed between pools mid-flight.

## Nothing here touches Firestore on a timer while idle

Every read and every write is billed, and every write is billed again as a read
on each listener open on that document. A worker that polled its queue every
second and re-announced itself every thirty spent tens of thousands of reads a
day doing nothing at all. So each concern is driven by something happening, and
an idle worker with nothing changing makes no request whatsoever.
See docs/adr/0030-status-on-events-and-pings.md.

- **Claiming.** A listener on the waiting jobs keeps a local view of the queue.
  The claim loop looks at that view when it changes, when a job ends, on a ping,
  and when a scheduled job comes due — a local alarm, not a read. A claim that
  was refused is not tried again until the listener says that job changed.
- **Lease renewal.** Every ``heartbeat_seconds``, and only while a job is
  running: that is work happening, and the lease is what protects it. The
  interval must be comfortably shorter than the lease or every job would be
  reaped mid-flight; ``Settings.heartbeat_fits_in_lease()`` checks it at
  startup. A renewal round also refreshes ``workers/{workerId}`` when a running
  stage has said something new — so at most once per interval, and never idle.
- **Reaper.** The same pure ``lease.reap``, run at startup, on a ping, after
  each job ends, and at the moment a lease this worker has seen is due to lapse.
  See docs/adr/0006-lease-based-job-claiming.md.
- **Research schedules.** A listener on the enabled schedules and a local alarm
  at the earliest due time.
- **Pings.** The agent relays a client's ping over loopback (:meth:`ping`). The
  answer is a write of ``workers/{workerId}`` carrying the token, then a reap and
  a real sweep of the queue — the one moment a listener that had silently stopped
  delivering would otherwise go unnoticed.
- **Listener health.** Checked locally every ``listener_check_seconds``; a dead
  listener is replaced, and the replacement's first delivery is the sweep.
- **Shutdown.** SIGINT/SIGTERM asks running stages to stop, returns their jobs to
  the queue, and writes OFFLINE — so another worker can pick the work up
  immediately instead of waiting out a 90-second lease.
"""

from __future__ import annotations

import contextlib
import signal
import socket
import threading
import time
from collections.abc import Callable, Collection, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import FrameType

from clipforge_contracts import (
    GpuInfo,
    Job,
    JobStatus,
    JobType,
    ResearchSchedule,
    StageError,
    WorkerCapabilities,
    WorkerHeartbeat,
    WorkerStatus,
)

from clipforge.config import Settings
from clipforge.media.trash import Trash
from clipforge.models.broker import ModelBroker
from clipforge.models.vram import probe_vram
from clipforge.observability import bind_job, get_logger
from clipforge.scheduler import lease
from clipforge.scheduler.runner import LeaseLostError, StageRunner
from clipforge.scheduler.schedule import Attempt, ResearchScheduler, may_fire, next_wake
from clipforge.scheduler.tidy import tidy_reviewed_clips
from clipforge.stages.base import StageProgress, StageRegistry
from clipforge.stages.echo import registry_for
from clipforge.store.firestore import ClipStore, JobStore, QueuedJob, Subscription, WorkerStore
from clipforge.version import __version__

log = get_logger(__name__)

__all__ = ["Worker", "claimable", "next_lapse", "next_not_before"]

# How long to leave a claim that *failed* — the network, not a lost race —
# before trying it again. A failure is the one case where the view says there is
# work and the loop must not simply ask again on its next pass.
CLAIM_RETRY = timedelta(seconds=15)

# How long a reaper pass that failed waits before the next attempt. It is the
# only recovery for a job whose owner died, so it is retried rather than left
# for the next event, which on an idle machine may be hours away.
REAP_RETRY = timedelta(seconds=60)

# Slack added to a lease's expiry before reaping it, so the alarm lands after the
# lapse rather than a hair before it and finds nothing to do.
LAPSE_SLACK = timedelta(seconds=1)


# ─────────────────────────────────────────────────────────────────────────────
# What the local view says, as arithmetic
# ─────────────────────────────────────────────────────────────────────────────


def claimable(
    view: Iterable[QueuedJob], refused: Mapping[str, datetime | None], now: datetime
) -> list[QueuedJob]:
    """The waiting jobs worth a claim right now, oldest first.

    Due, and not refused at this version. A job this worker failed to claim —
    another worker won it, usually — stays in the view until the listener says
    it left, and trying it again on every pass would be a transaction per pass
    for an answer already known. A *new* version is a different question: the
    job was released, rescheduled or resubmitted since, so it is tried again.
    """
    return [
        queued
        for queued in view
        if lease.is_due(queued.job, now)
        and not (queued.job.id in refused and refused[queued.job.id] == queued.version)
    ]


def next_not_before(view: Iterable[QueuedJob], now: datetime) -> datetime | None:
    """When the earliest scheduled job in the view comes due, if one is waiting.

    The claim loop sleeps until then on its own clock. The poll it replaced
    found a scheduled publish by reading the queue once a second until it was
    time; the view already holds the time, so there is nothing to read.
    """
    return min(
        (
            queued.job.not_before
            for queued in view
            if queued.job.not_before is not None and queued.job.not_before > now
        ),
        default=None,
    )


def next_lapse(jobs: Iterable[Job], *, skip: Collection[str], now: datetime) -> datetime | None:
    """When the earliest lease held by someone else will lapse, if any will.

    The reaper's alarm. ``skip`` is this worker's own jobs, which it renews and
    never reaps; a lease already lapsed was just dealt with by the pass that is
    asking. While another worker is busy this costs one query per lease length,
    which is work happening on that machine; with no other worker it is the one
    pass that recovers jobs orphaned by a crash of this one.
    """
    return min(
        (
            job.lease_expires_at
            for job in jobs
            if job.id not in skip
            and job.lease_expires_at is not None
            and job.lease_expires_at > now
        ),
        default=None,
    )


class Worker:
    """One worker process."""

    def __init__(
        self,
        *,
        settings: Settings,
        jobs: JobStore,
        workers: WorkerStore,
        broker: ModelBroker | None = None,
        # Optional so every existing caller and test keeps working: without it
        # the worker simply does not tidy, which is the behaviour it had before.
        clips: ClipStore | None = None,
        trash: Trash | None = None,
        # Fires the standing research schedules. Optional for the same reason:
        # a worker built without one never starts work unasked.
        research_scheduler: ResearchScheduler | None = None,
        uid: str = "local",
        registry_factory: Callable[[JobType], StageRegistry] = registry_for,
    ) -> None:
        self._settings = settings
        self._jobs = jobs
        self._workers = workers
        self._broker = broker or ModelBroker(reserve_mb=settings.vram_reserve_mb)
        self._clips = clips
        self._trash = trash
        self._research_scheduler = research_scheduler
        self._uid = uid
        # Injected so tests can supply stage behaviour that is awkward to
        # provoke with the real stages, and so Phases 3-6 can register the CLIP
        # pipeline without editing the worker.
        self._registry_factory = registry_factory

        self._stop = threading.Event()
        # Set by anything the claim loop should look at: a delivery, a job
        # ending, a ping, a stop. The loop otherwise sleeps until its next local
        # alarm, and an alarm is arithmetic, not a request.
        self._wake = threading.Event()
        self._active: dict[str, Job] = {}
        # What each running job's current stage says it is doing. Keyed the same
        # way as `_active` and guarded by the same lock, because the thread that
        # writes a note is never the thread that publishes it.
        self._notes: dict[str, StageProgress] = {}
        self._active_lock = threading.Lock()
        self._started_at = datetime.now(UTC)
        self._threads: list[threading.Thread] = []

        # The queue as the listener last described it, in its order (oldest
        # first), and the version of each job a claim was refused at. Both are
        # written by the listener's thread and read by the claim loop.
        self._view_lock = threading.Lock()
        self._view: dict[str, QueuedJob] = {}
        self._refused: dict[str, datetime | None] = {}
        # `None` until the schedule listener has delivered at all, which is
        # different from an empty list: nothing must fire on a guess.
        self._schedules: list[ResearchSchedule] | None = None
        self._schedule_attempts: dict[str, Attempt] = {}
        self._queue_watch: Subscription | None = None
        self._schedule_watch: Subscription | None = None
        self._listeners_checked = time.monotonic()

        # A ping waiting for the claim loop, and the last one answered. The
        # answered token rides on every later write of the worker document, so
        # an event landing a moment after the answer does not erase it before
        # the client that asked has seen it.
        self._pinged: str | None = None
        self._answered: str | None = None
        self._sweep_due = False

        # When the reaper next needs to look, and when a failed claim may be
        # retried. Both are local alarms; `None` means nothing is pending.
        self._reap_at: datetime | None = None
        self._claim_retry_at: datetime | None = None

        # What the worker document last said, so a renewal round that has
        # nothing new to report writes nothing. Guarded because the claim loop
        # and the renewal thread both announce.
        self._announce_lock = threading.Lock()
        self._said: tuple[object, ...] | None = None

    # ── Introspection ────────────────────────────────────────────────────────
    #
    # Read-only views for the loopback control API (clipforge.scheduler.control).
    # Exposed as accessors rather than by making `_active` public: the set is
    # mutated from several threads, and a caller iterating it directly would be
    # one job completion away from a RuntimeError.

    @property
    def worker_id(self) -> str:
        return self._jobs.worker_id

    @property
    def started_at(self) -> datetime:
        return self._started_at

    def active_job_ids(self) -> list[str]:
        with self._active_lock:
            return list(self._active)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def install_signal_handlers(self) -> None:
        """Ask for a clean stop on Ctrl-C or a termination request.

        Windows delivers SIGINT for Ctrl-C but has no SIGTERM in the POSIX sense,
        so SIGTERM is registered defensively rather than assumed. Getting this
        wrong is a documented Phase 2 risk — the shutdown path is exercised on
        Windows now rather than discovered at release time.
        """

        def _handle(signum: int, _frame: FrameType | None) -> None:
            # Stop before logging, for the same reason as the control API.
            self.request_stop()
            log.info("worker.signal", signal=signum)

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            with contextlib.suppress(ValueError, OSError):
                # ValueError: not on the main thread. OSError: unsupported here.
                signal.signal(sig, _handle)

    def request_stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def ping(self, token: str) -> None:
        """Answer a client's ping, relayed by the agent over loopback.

        Returns at once: the answer — a write of this worker's document carrying
        the token, then a reap and a sweep of the queue — is made by the claim
        loop, which this wakes. The control API's thread must not be the one
        that talks to Firestore, for the reason its shutdown route returns
        without waiting: a caller should get a quick answer from loopback even
        when the network is having a bad minute.
        """
        self._pinged = token
        self._wake.set()

    def run_once(self) -> Job | None:
        """Claim and run a single job, then return it. ``None`` if the queue was
        empty. A smoke-test and debugging entry point — it skips the listeners,
        lease renewal and the reaper, so it must not be used for real work."""
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

        self._spawn(self._lease_loop, "lease")
        # Before the listener, so a job orphaned by this worker's previous run
        # is back in the queue by the time the first delivery arrives.
        self._housekeep()
        self._listen()

        pool = ThreadPoolExecutor(
            max_workers=self._settings.cpu_lane_depth, thread_name_prefix="job"
        )
        running: set[Future[None]] = set()

        try:
            while not self._stop.is_set():
                self._wake.clear()
                now = datetime.now(UTC)

                still = {f for f in running if not f.done()}
                if len(still) < len(running):
                    self._after_jobs()
                running = still

                pinged, self._pinged = self._pinged, None
                if pinged is not None:
                    self._answer(pinged)

                self._check_listeners(force=pinged is not None)

                if self._reap_at is not None and now >= self._reap_at:
                    self._housekeep()

                self._fire_due_schedules(now)

                while len(running) < self._settings.cpu_lane_depth and not self._stop.is_set():
                    claimed = self._claim(datetime.now(UTC))
                    if claimed is None:
                        break
                    running.add(self._start(claimed, pool))

                self._wake.wait(self._sleep_for(datetime.now(UTC)))
        finally:
            self._shutdown(pool, running)

    # ── Claiming ─────────────────────────────────────────────────────────────

    def _claim(self, now: datetime) -> Job | None:
        """One job from the view, or from a real sweep if a ping asked for one.

        The sweep is :meth:`JobStore.claim_next` — the old poll, run once. It
        exists for the listener that has stopped delivering without closing,
        which ``alive()`` cannot see: a ping is a person asking whether things
        are working, and that is the moment to find out for certain.
        """
        if self._claim_retry_at is not None and now < self._claim_retry_at:
            return None
        self._claim_retry_at = None

        with self._view_lock:
            candidates = claimable(self._view.values(), self._refused, now)

        for queued in candidates:
            try:
                claimed = self._jobs.try_claim(queued.job.id, now=now)
            except Exception as exc:  # noqa: BLE001 - the network may fail any way it likes
                log.warning("claim.failed", job_id=queued.job.id, error=str(exc))
                self._claim_retry_at = now + CLAIM_RETRY
                return None
            with self._view_lock:
                if claimed is not None:
                    # Ours now. The listener will agree in a moment; dropping it
                    # here stops the next pass offering it again meanwhile.
                    self._view.pop(queued.job.id, None)
                else:
                    self._refused[queued.job.id] = queued.version
            if claimed is not None:
                return claimed

        if self._sweep_due:
            self._sweep_due = False
            try:
                return self._jobs.claim_next(now=now)
            except Exception as exc:  # noqa: BLE001 - same
                log.warning("claim.sweep_failed", error=str(exc))
        return None

    def _start(self, job: Job, pool: ThreadPoolExecutor) -> Future[None]:
        with self._active_lock:
            self._active[job.id] = job
            active = list(self._active)
        future = pool.submit(self._execute, job)
        # A job ending is an event the claim loop acts on — a lane is free, and
        # the reaper runs — so it wakes the loop rather than waiting to be seen.
        future.add_done_callback(lambda _done: self._wake.set())
        self._announce(WorkerStatus.BUSY, active)
        return future

    def _after_jobs(self) -> None:
        """Say so, and look for anything the job's ending has left behind."""
        active = self.active_job_ids()
        self._announce(WorkerStatus.BUSY if active else WorkerStatus.ONLINE, active)
        self._housekeep()

    def _answer(self, token: str) -> None:
        """Answer a ping: write the token, reap, and sweep the queue."""
        self._answered = token
        active = self.active_job_ids()
        self._announce(WorkerStatus.BUSY if active else WorkerStatus.ONLINE, active)
        log.info("worker.ping_answered", token=token[:8])
        self._housekeep()
        self._sweep_due = True

    def _sleep_for(self, now: datetime) -> float:
        """Seconds until the next local alarm. Nothing here is a request."""
        with self._view_lock:
            alarms = [next_not_before(self._view.values(), now)]
            schedules = list(self._schedules or [])
        alarms += [
            self._reap_at,
            self._claim_retry_at,
            next_wake(schedules, now=now, attempts=self._schedule_attempts)
            if self._research_scheduler is not None
            else None,
        ]
        check = self._listeners_checked + self._settings.listener_check_seconds - time.monotonic()
        waits = [check] + [(alarm - now).total_seconds() for alarm in alarms if alarm is not None]
        return max(0.05, min(waits))

    # ── Listeners ────────────────────────────────────────────────────────────

    def _listen(self) -> None:
        if self._queue_watch is None:
            try:
                self._queue_watch = self._jobs.watch_queued(self._queue_changed)
            except Exception as exc:  # noqa: BLE001 - retried by the health check
                log.warning("listener.queue_unavailable", error=str(exc))
        wants_schedules = (
            self._research_scheduler is not None and self._settings.research_schedules_enabled
        )
        if wants_schedules and self._schedule_watch is None and self._research_scheduler:
            try:
                self._schedule_watch = self._research_scheduler.watch(self._schedules_changed)
            except Exception as exc:  # noqa: BLE001 - same
                log.warning("listener.schedules_unavailable", error=str(exc))

    def _check_listeners(self, *, force: bool = False) -> None:
        """Replace a listener that has died. Local unless one has.

        ``alive()`` reads the client library's own state, so this costs nothing
        while everything is healthy, and it runs on a timer for that reason. A
        replaced listener delivers the current documents once, which is the
        sweep that makes up for whatever it missed.
        """
        if not force and (
            time.monotonic() - self._listeners_checked < self._settings.listener_check_seconds
        ):
            return
        self._listeners_checked = time.monotonic()
        for name in ("_queue_watch", "_schedule_watch"):
            watch: Subscription | None = getattr(self, name)
            if watch is not None and not watch.alive():
                log.warning("listener.dead", listener=name.strip("_"))
                watch.close()
                setattr(self, name, None)
        self._listen()

    def _queue_changed(self, delivered: list[QueuedJob]) -> None:
        """The listener's callback. Runs on the client library's thread."""
        with self._view_lock:
            self._view = {queued.job.id: queued for queued in delivered}
            self._refused = {
                job_id: version for job_id, version in self._refused.items() if job_id in self._view
            }
        self._wake.set()

    def _schedules_changed(self, schedules: list[ResearchSchedule]) -> None:
        with self._view_lock:
            self._schedules = schedules
        self._wake.set()

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
            with self._active_lock:
                self._active.pop(job.id, None)
            return

        # Registered before the runner starts, so a renewal landing during the
        # first stage already has somewhere to read from rather than skipping a
        # renewal's worth of whatever that stage was saying.
        notes = StageProgress()
        with self._active_lock:
            self._notes[job.id] = notes

        runner = StageRunner(
            store=self._jobs,
            registry=registry,
            settings=self._settings,
            broker=self._broker,
            progress=notes,
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
            # job rather than this worker guessing at its state. The reaper runs
            # when this job ends and sets an alarm for the lapse, so "recovers"
            # still means within a lease length.
            log.exception("job.crashed", job_id=job.id)
        finally:
            if not still_ours:
                with self._active_lock:
                    self._active.pop(job.id, None)
                    self._notes.pop(job.id, None)

    def _reject(self, job: Job, exc: Exception) -> None:
        """Fail a job this worker cannot run, with the reason recorded."""
        error = StageError(
            type=type(exc).__name__, message=str(exc), traceback=None, retryable=False
        )
        with bind_job(job_id=job.id):
            log.warning("job.unrunnable", error=str(exc))
            with contextlib.suppress(Exception):
                self._jobs.apply(lease.fail_stage(job, error=error, now=datetime.now(UTC)))

    # ── Background work ──────────────────────────────────────────────────────

    def _spawn(self, target: object, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)  # type: ignore[arg-type]
        thread.start()
        self._threads.append(thread)

    def _lease_loop(self) -> None:
        """Renew the lease on everything this worker is running, and nothing else.

        Idle, a pass is a dictionary lookup and no request at all: renewing
        nothing costs nothing, and the worker document is not re-announced to
        say the same thing again. That was the old heartbeat, and it is gone.

        Every renewal is individually guarded, and that is the whole point of
        the shape of this loop. A renewal is a Firestore transaction, and
        Firestore is reachable over a network that is allowed to have a bad
        minute — a 503 or a deadline here is normal, not exceptional.

        An unguarded `renew()` raising killed this thread outright, and a dead
        renewal thread never comes back: every lease then lapses, and the
        reaper in this very worker reclaims jobs it is still running. Observed
        exactly once and expensively — a transient `UNAVAILABLE` during a slow
        clip upload cost a full re-render and re-upload of work that was already
        finished. The lease is what protects correctness, so the loop that
        renews it has to be the most robust thing here.
        """
        while not self._stop.wait(self._settings.heartbeat_seconds):
            active = self.active_job_ids()
            if not active:
                continue
            self.renew_active(active)
            self._report_progress(active)

    def renew_active(self, active: list[str]) -> None:
        """One round of lease renewals, each guarded independently.

        Extracted from the loop so the guarantee can be tested without threads
        or clocks — the guarantee being that this returns, whatever Firestore
        does.
        """
        for job_id in active:
            try:
                if self._jobs.renew(job_id, progress=self._note_for(job_id)) is None:
                    # Genuinely lost: someone else owns this now. The runner
                    # notices on its next checkpoint and abandons the job.
                    log.warning("lease.lost_during_heartbeat", job_id=job_id)
            except Exception as exc:  # noqa: BLE001 - the network may fail any way it likes
                # Not lost, just unconfirmed. The lease still has most of its
                # life left — `heartbeat_seconds` is a fraction of
                # `lease_seconds` precisely so a missed beat is survivable — so
                # the right response is to try again on the next tick.
                log.warning("lease.renew_failed", job_id=job_id, error=str(exc))

    def _report_progress(self, active: list[str]) -> None:
        """Refresh the worker document if a running stage has said something new.

        Called once per renewal round, so it writes at most once per
        ``heartbeat_seconds`` and only while busy. The job documents already
        carry the notes; what this adds is a worker document whose `lastSeenAt`
        moves while a long stage runs, which is the evidence a client watching
        it has that the machine is still there.
        """
        if self._signature(WorkerStatus.BUSY, active) == self._said:
            return
        self._announce(WorkerStatus.BUSY, active)

    def _note_for(self, job_id: str) -> str | None:
        """What this job's running stage last said, if it has said anything.

        Read as late as this call site allows, which is not as late as the
        write: `JobStore.renew` opens its transaction and re-reads the document
        after this has already returned, so a note the stage changes during that
        round trip goes out on the next renewal instead of this one. One beat
        late, once, on a sentence describing a step measured in minutes — and
        closing the gap would mean handing the store a callback to call inside
        its transaction, which is machinery bought for nothing.

        Reading it here rather than taking it as a parameter still earns its
        keep: a round walks every active job in turn, so a note captured when
        the round started would be stale by however long the jobs ahead of this
        one took.
        """
        with self._active_lock:
            notes = self._notes.get(job_id)
        return notes.current() if notes else None

    def _housekeep(self) -> None:
        """Reap what has lapsed, set an alarm for what will, and tidy.

        Guarded end to end: this is the only recovery for a job whose owner
        died, and an exception escaping it would take the claim loop down with
        it. A failed pass sets a retry alarm rather than waiting for the next
        event, which on an idle machine may not come for hours.
        """
        now = datetime.now(UTC)
        self._reap_at = None
        try:
            # Never our own. An expired lease usually means the owner is gone;
            # it can also mean the owner is this process and Firestore was
            # briefly unreachable, and reclaiming then means doing the work
            # twice.
            mine = self.active_job_ids()
            running = self._jobs.running()
            reaped = self._jobs.reap(now=now, skip=mine, candidates=running)
            if reaped:
                log.info("reaper.reclaimed", jobs=[j.id for j in reaped])
            lapse = next_lapse(running, skip=mine, now=now)
            if lapse is not None:
                self._reap_at = lapse + LAPSE_SLACK
        except Exception:
            log.exception("reaper.failed")
            self._reap_at = now + REAP_RETRY

        self._tidy_once()

    def _fire_due_schedules(self, now: datetime) -> None:
        """Fire what the schedule listener says is due. Reads only when one is.

        Each schedule is tried once per due time: a firing advances it and the
        listener delivers the new time, and one that did not fire — another
        worker won it, or the network failed — is left until its retry rather
        than tried on every pass of the loop.

        Guarded like the renewal thread: a bad minute on the network must not
        take the claim loop down, and the symptom of research that never fires —
        a morning with no list — is indistinguishable from a worker that was off.
        """
        if self._research_scheduler is None or not self._settings.research_schedules_enabled:
            return
        with self._view_lock:
            held = list(self._schedules or [])
        due = [s for s in held if may_fire(s, now=now, attempts=self._schedule_attempts)]
        if not due:
            return
        self._schedule_attempts = {
            schedule_id: attempt
            for schedule_id, attempt in self._schedule_attempts.items()
            if any(s.id == schedule_id for s in held)
        }
        for schedule in due:
            self._schedule_attempts[schedule.id] = Attempt.after(schedule, now=now)
        try:
            self._research_scheduler.tick(due)
        except Exception:
            log.exception("schedule.tick_failed")

    def _tidy_once(self) -> None:
        """Collect the clips a review decision has already settled.

        On the reaper's occasions rather than a thread of its own: both are
        housekeeping, neither is urgent, and a second trigger would be a second
        thing to reason about when the worker is shutting down.

        Swallows everything. A failure to tidy is untidy, and taking the reaper
        down with it would cost the thing that actually matters.
        """
        if self._clips is None or self._trash is None:
            return
        try:
            settled = self._clips.settled()
            if not settled:
                return
            tidy_reviewed_clips(
                self._clips.lineage_peers(settled),
                trash=self._trash,
                delete_record=self._clips.forget,
                grace_days=self._settings.superseded_grace_days,
            )
        except Exception:
            log.exception("tidy.failed")

    # ── The worker document ──────────────────────────────────────────────────

    def _signature(self, status: WorkerStatus, active: list[str]) -> tuple[object, ...]:
        """What a write of the worker document would say, minus the clock."""
        return (
            status,
            tuple(active),
            tuple(self._note_for(job_id) for job_id in active),
            self._answered,
        )

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

        with self._announce_lock:
            self._said = self._signature(status, active or [])
            try:
                self._workers.announce(
                    WorkerHeartbeat(
                        worker_id=self._jobs.worker_id,
                        uid=self._uid,
                        status=status,
                        capabilities=WorkerCapabilities(
                            # Advertised from configuration rather than probed:
                            # the PWA uses this to explain a stalled job, and
                            # "the worker believes it can transcribe" is the
                            # useful claim.
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
                        answered_ping=self._answered,
                    )
                )
            except Exception:
                # A failed write must never take the worker down; the lease is
                # what actually protects correctness.
                log.exception("heartbeat.failed")

    # ── Shutdown ─────────────────────────────────────────────────────────────

    def _shutdown(self, pool: ThreadPoolExecutor, running: set[Future[None]]) -> None:
        log.info("worker.stopping")
        self._stop.set()
        self._unlisten()

        deadline = time.monotonic() + 2.0
        for future in list(running):
            remaining = max(0.0, deadline - time.monotonic())
            with contextlib.suppress(Exception):
                future.result(timeout=remaining)

        pool.shutdown(wait=False, cancel_futures=True)
        self._release_active()
        self._announce(WorkerStatus.OFFLINE)
        log.info("worker.stopped")

    def _unlisten(self) -> None:
        """Close both listeners without waiting for them.

        The client library joins its consumer thread for up to a second per
        listener, and shutdown has a two-second budget that belongs to the
        stages checkpointing. Closed on a daemon thread instead: the process is
        about to exit, which ends the streams regardless.
        """
        watches = [w for w in (self._queue_watch, self._schedule_watch) if w is not None]
        self._queue_watch = self._schedule_watch = None

        def _close() -> None:
            for watch in watches:
                watch.close()

        if watches:
            threading.Thread(target=_close, name="unlisten", daemon=True).start()

    def _release_active(self) -> None:
        """Return still-held jobs to the queue so another worker can take them.

        Re-read before releasing: the in-memory copy is from claim time and its
        stage results are stale, so writing it back would erase everything the
        run just accomplished.
        """
        with self._active_lock:
            job_ids = list(self._active)
            self._active.clear()
            self._notes.clear()

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
