"""The control routes the desktop shell uses to see and stop this worker.

Tested against a stand-in worker rather than a real one, because what is under
test is the *contract with the caller*: what the shell is told, and that asking
for a stop asks rather than kills. Starting a real worker would need Firestore
and would test the scheduler instead.

Transport and trust live in :mod:`clipforge.localapi` and are tested in
test_localapi.py. Nothing here knows about HTTP, which is the point of the split.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from clipforge.scheduler.control import WorkerRoutes
from clipforge.version import __version__


class FakeWorker:
    """A worker that records having been asked to stop, without stopping."""

    def __init__(self, *, active: list[str] | None = None, uptime_s: int = 90) -> None:
        self.worker_id = "worker-abc"
        self.started_at = datetime.now(UTC) - timedelta(seconds=uptime_s)
        self._active = active or []
        self.stop_requested = 0

    def active_job_ids(self) -> list[str]:
        return list(self._active)

    def request_stop(self) -> None:
        self.stop_requested += 1


def routes(worker: FakeWorker, *, pid: int = 4242) -> WorkerRoutes:
    return WorkerRoutes(worker, pid=pid)  # type: ignore[arg-type]


# ── The routing table ────────────────────────────────────────────────────────


def test_the_table_offers_exactly_the_two_routes_it_documents() -> None:
    """Read and stop, and nothing that could change how the worker behaves.

    A control surface that grows write routes quietly is one whose threat model
    (docs/adr/0011-local-control-api.md) has stopped describing it.
    """
    assert set(routes(FakeWorker()).table()) == {
        ("GET", "/worker"),
        ("POST", "/worker/shutdown"),
    }


# ── Status ───────────────────────────────────────────────────────────────────


def test_status_identifies_which_process_is_answering() -> None:
    """The pid is the point.

    Two workers on one machine are legal — the lease protocol is what makes
    them safe — but only one can hold the control port, so an app that started
    worker B could otherwise send its shutdown to worker A. The pid makes that
    mistake detectable instead of silent.
    """
    status = routes(FakeWorker(), pid=1234).status({})

    assert status["pid"] == 1234
    assert status["workerId"] == "worker-abc"
    assert status["version"] == __version__


def test_status_reports_uptime_and_what_is_running() -> None:
    status = routes(FakeWorker(active=["job-1", "job-2"], uptime_s=120)).status({})

    assert status["activeJobIds"] == ["job-1", "job-2"]
    # Not asserted exactly: the clock moves between construction and the call,
    # and a test that demanded 120 would fail on a slow machine for no reason.
    assert 110 <= int(str(status["uptimeSeconds"])) <= 130
    assert str(status["startedAt"]).endswith("+00:00")


def test_an_idle_worker_reports_an_empty_list_not_an_absent_one() -> None:
    """ "Nothing running" and "cannot tell" must not look the same to the caller."""
    assert routes(FakeWorker()).status({})["activeJobIds"] == []


# ── Shutdown ─────────────────────────────────────────────────────────────────


def test_shutdown_asks_the_worker_rather_than_killing_it() -> None:
    """A kill is not a stop.

    The shutdown path asks running stages to checkpoint, returns their jobs to
    the queue so the next worker can take them immediately, and writes an
    OFFLINE heartbeat. A killed worker does none of that, and its in-flight job
    sits RUNNING behind a lease nobody is renewing — recovered by the reaper,
    which lives in the worker, so on a single-worker install it stays stranded
    until somebody starts one again.

    Also asserts the *asking* rather than the exiting: request_stop sets a flag
    for the run loop and returns, so the response does not wait on a shutdown
    that takes up to two seconds. Holding it open would give the caller a
    request that looks hung at the moment it most wants a clear answer.
    """
    worker = FakeWorker(active=["job-1"])

    result = routes(worker).shutdown({})

    assert worker.stop_requested == 1
    assert result["stopping"] is True


def test_shutdown_returns_what_was_in_flight_when_it_was_asked() -> None:
    """So the caller can say what is being interrupted, before it is gone."""
    result = routes(FakeWorker(active=["job-7"]), pid=99).shutdown({})

    assert result["activeJobIds"] == ["job-7"]
    assert result["pid"] == 99


def test_a_second_shutdown_request_is_not_an_error() -> None:
    """An impatient double-tap must not produce a 500.

    Stopping is idempotent by nature: the flag is already set and the loop is
    already winding down.
    """
    worker = FakeWorker()
    first = routes(worker).shutdown({})
    second = routes(worker).shutdown({})

    assert first["stopping"] is True
    assert second["stopping"] is True
    assert worker.stop_requested == 2
