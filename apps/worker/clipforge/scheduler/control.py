"""The routes the desktop app calls to see and stop this worker.

Split from :mod:`clipforge.localapi` for the same reason
:mod:`clipforge.publish.channels` is: that module owns transport and trust and
knows nothing about workers; this one knows about the worker and nothing about
HTTP. Each can be read, and tested, without the other.

## Why stopping goes through here rather than through a signal

The desktop shell starts the worker as a child process, so killing it is a
system call away. It does not, because a kill is not a stop. The worker's
shutdown path asks running stages to checkpoint, returns their jobs to the queue
so the next worker can take them *immediately*, and flips the heartbeat to
OFFLINE — see :meth:`clipforge.scheduler.worker.Worker._shutdown`. A killed
worker skips all three, and its in-flight job then sits RUNNING behind a lease
nobody is renewing. Normally the reaper recovers that within ``lease_seconds``,
but the reaper *lives in the worker*, so on a single-worker deployment the job
stays stranded until someone starts a worker again. Stopping through this route
is the difference between "requeued now" and "requeued whenever you next
remember to start the worker".

A kill is still the backstop when this route cannot be reached, which is right:
a worker too wedged to answer HTTP is a worker that has to be killed.

## Why ``GET /worker`` reports the pid

The caller needs it to know *who it is talking to*. Two workers on one machine
are legal — the lease protocol is what makes them safe — but only one can hold
the control port, so an app that started worker B could otherwise send its
shutdown to worker A and stop the wrong one. The pid makes that mistake
detectable rather than silent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from clipforge.observability import get_logger
from clipforge.version import __version__

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime, types only
    from clipforge.scheduler.worker import Worker

log = get_logger(__name__)

__all__ = ["WorkerRoutes"]


class WorkerRoutes:
    """Describe and stop the running worker, from this machine only."""

    def __init__(self, worker: Worker, *, pid: int) -> None:
        self._worker = worker
        # Passed in rather than read from `os.getpid()` here so a test can state
        # which process it means without pretending to be one.
        self._pid = pid

    # ── Routing table ────────────────────────────────────────────────────────

    def table(self) -> dict[tuple[str, str], Any]:
        return {
            ("GET", "/worker"): self.status,
            ("POST", "/worker/shutdown"): self.shutdown,
        }

    # ── Handlers ─────────────────────────────────────────────────────────────

    def status(self, _payload: dict[str, Any]) -> dict[str, Any]:
        """What this process is, and what it is currently doing.

        Deliberately overlaps the Firestore heartbeat rather than replacing it.
        The heartbeat answers "is a worker serving my queue?" from anywhere,
        including a phone, and it is up to 30 seconds stale. This answers "is the
        process I started still alive?" instantly, and it keeps answering when
        Firestore is unreachable — which is exactly the failure a heartbeat
        cannot report on its own, because a worker that cannot write its
        heartbeat looks identical to a worker that is not running.
        """
        started = self._worker.started_at
        return {
            "pid": self._pid,
            "workerId": self._worker.worker_id,
            "version": __version__,
            "startedAt": started.isoformat(),
            "uptimeSeconds": int((datetime.now(UTC) - started).total_seconds()),
            "activeJobIds": self._worker.active_job_ids(),
        }

    def shutdown(self, _payload: dict[str, Any]) -> dict[str, Any]:
        """Ask the worker to stop cleanly, and return without waiting.

        Returning immediately is deliberate. Shutdown waits up to two seconds for
        running stages to checkpoint and then writes an OFFLINE heartbeat, and
        holding the HTTP response open for that would give the caller a request
        that looks hung at exactly the moment it most wants a clear answer. The
        caller watches the process exit instead, which is the fact it actually
        cares about.
        """
        active = self._worker.active_job_ids()
        log.info("worker.shutdown_requested", source="localapi", active_jobs=len(active))
        self._worker.request_stop()
        return {"stopping": True, "pid": self._pid, "activeJobIds": active}
