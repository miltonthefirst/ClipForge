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

import pytest
from clipforge.scheduler.control import WorkerRoutes
from clipforge.version import __version__

# Every test in this file is the unit tier. Without this marker CI's
# `pytest -m unit` silently deselects the whole file — the tests pass locally,
# run nowhere, and protect nothing.
pytestmark = pytest.mark.unit


class FakeWorker:
    """A worker that records having been asked to stop, without stopping."""

    def __init__(self, *, active: list[str] | None = None, uptime_s: int = 90) -> None:
        self.worker_id = "worker-abc"
        self.started_at = datetime.now(UTC) - timedelta(seconds=uptime_s)
        self._active = active or []
        self.stop_requested = 0
        self.pings: list[str] = []

    def active_job_ids(self) -> list[str]:
        return list(self._active)

    def request_stop(self) -> None:
        self.stop_requested += 1

    def ping(self, token: str) -> None:
        self.pings.append(token)


def routes(worker: FakeWorker, *, pid: int = 4242) -> WorkerRoutes:
    return WorkerRoutes(worker, pid=pid)  # type: ignore[arg-type]


# ── The routing table ────────────────────────────────────────────────────────


def test_the_table_offers_exactly_the_routes_it_documents() -> None:
    """Reads, one stop, and one ping. Nothing else that could change how the
    worker behaves."""
    assert set(routes(FakeWorker()).table()) == {
        ("GET", "/worker"),
        ("GET", "/worker/settings"),
        ("POST", "/worker/shutdown"),
        ("POST", "/worker/ping"),
    }


def test_stopping_is_the_only_thing_this_surface_can_do() -> None:
    """The property the route list is really guarding.

    Read routes may grow — describing the worker more fully is the point of it.
    A second way to *change* the worker is what would make the threat model in
    docs/adr/0011-local-control-api.md stop describing this surface, so that is
    what is pinned, rather than a count that has to be edited every time
    something new becomes visible.

    The ping is a POST because it makes the worker *write* — its own document,
    with the token — but it changes nothing about what the worker does, and it
    is named here so that a third POST still has to argue its way in.
    """
    writes = {path for method, path in routes(FakeWorker()).table() if method != "GET"}
    assert writes == {"/worker/shutdown", "/worker/ping"}


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


def test_shutdown_is_honoured_even_when_it_cannot_be_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stop comes before the log line, not after it.

    A worker whose console had gone away raised on every log call, and this
    handler logged first — so every Stop failed before it asked for anything,
    and the worker could only be killed.
    """

    class DeadLog:
        def info(self, *_args: object, **_kwargs: object) -> None:
            raise OSError(22, "Invalid argument")

    monkeypatch.setattr("clipforge.scheduler.control.log", DeadLog())
    worker = FakeWorker()

    with pytest.raises(OSError):
        routes(worker).shutdown({})

    assert worker.stop_requested == 1


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


# ── Ping ─────────────────────────────────────────────────────────────────────


def test_a_ping_is_handed_to_the_worker_and_answered_at_once() -> None:
    """The agent relays a client's ping here; the worker answers it by writing
    its own document, from its own loop. The route only hands the token over —
    a loopback caller must get a quick answer even when Firestore is slow."""
    worker = FakeWorker()

    result = routes(worker, pid=77).ping({"token": "tok-123"})

    assert worker.pings == ["tok-123"]
    assert result == {"pinged": True, "pid": 77}


@pytest.mark.parametrize("payload", [{}, {"token": ""}, {"token": 7}, {"token": "x" * 65}])
def test_a_ping_without_a_usable_token_is_refused(payload: dict[str, object]) -> None:
    """The token is echoed into a document every member reads, and the rules
    never let a client write one longer than 64 characters — so anything else
    did not come from a client, and is not passed on."""
    worker = FakeWorker()

    with pytest.raises(ValueError, match="token"):
        routes(worker).ping(payload)

    assert worker.pings == []
