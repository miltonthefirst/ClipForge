"""Status on events and pings, against the emulator.

The unit tier pins *whether* the worker and the agent ask Firestore anything;
this tier proves the same paths against real listeners, real transactions and a
real loopback relay. Three properties, each the reason for a piece of
docs/adr/0030-status-on-events-and-pings.md:

- a queued job is picked up from the listener's delivery, with no poll;
- a client's ping is answered by the agent *and* by the worker, each echoing the
  token into its own document;
- an idle worker sends nothing at all.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from clipforge.agent import Agent, Supervisor, probe_worker
from clipforge.config import Settings
from clipforge.localapi import LocalControlApi, read_or_create_token
from clipforge.models.broker import ModelBroker
from clipforge.scheduler.control import WorkerRoutes
from clipforge.scheduler.worker import Worker
from clipforge.stages.echo import new_echo_job
from clipforge.store.firestore import AgentStore, JobStore, WorkerStore
from clipforge_contracts import AgentState, JobStatus
from google.cloud import firestore

pytestmark = pytest.mark.integration


def until(condition: Callable[[], bool], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


class CountingJobStore(JobStore):
    """The real store, with every method that reaches Firestore counted."""

    def __init__(self, client: firestore.Client, settings: Settings) -> None:
        super().__init__(client, settings)
        self.calls: list[str] = []

    def _count(self, name: str) -> None:
        self.calls.append(name)

    def claim_next(self, **kwargs: Any) -> Any:
        self._count("claim_next")
        return super().claim_next(**kwargs)

    def queued(self, limit: int = 25) -> Any:
        self._count("queued")
        return super().queued(limit)

    def try_claim(self, job_id: str, **kwargs: Any) -> Any:
        self._count("try_claim")
        return super().try_claim(job_id, **kwargs)

    def running(self, limit: int = 50) -> Any:
        self._count("running")
        return super().running(limit)

    def renew(self, job_id: str, **kwargs: Any) -> Any:
        self._count("renew")
        return super().renew(job_id, **kwargs)


class CountingWorkerStore(WorkerStore):
    def __init__(self, client: firestore.Client, settings: Settings) -> None:
        super().__init__(client, settings)
        self.calls: list[str] = []

    def announce(self, heartbeat: Any) -> None:
        self.calls.append("announce")
        super().announce(heartbeat)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def scoped(settings: Settings, tmp_path: Path) -> Settings:
    # A port and a token of the test's own, so nothing here can reach — or be
    # reached by — a worker running on this machine for real.
    return settings.model_copy(
        update={
            "worker_id": "presence",
            "local_api_port": free_port(),
            "local_api_token_file": tmp_path / "token",
            "listener_check_seconds": 1.0,
        }
    )


@pytest.fixture
def running_worker(
    client: firestore.Client, scoped: Settings
) -> Iterator[tuple[Worker, CountingJobStore, CountingWorkerStore]]:
    jobs = CountingJobStore(client, scoped)
    workers = CountingWorkerStore(client, scoped)
    worker = Worker(
        settings=scoped,
        jobs=jobs,
        workers=workers,
        broker=ModelBroker(reserve_mb=700, probe=lambda: None),
    )
    api = LocalControlApi(
        WorkerRoutes(worker, pid=4242).table(),
        token=read_or_create_token(scoped.local_api_token_file),
        port=scoped.local_api_port,
    )
    api.start()
    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()
    until(lambda: "running" in jobs.calls)
    time.sleep(1.0)
    try:
        yield worker, jobs, workers
    finally:
        worker.request_stop()
        thread.join(timeout=30)
        api.stop()


def test_a_queued_job_is_picked_up_from_the_listener_without_polling(
    running_worker: tuple[Worker, CountingJobStore, CountingWorkerStore],
) -> None:
    _worker, jobs, _workers = running_worker
    before = len(jobs.calls)

    jobs.create(new_echo_job(uid="user-1", job_id="listened"))
    until(lambda: (job := jobs.get("listened")) is not None and job.status is JobStatus.COMPLETED)

    after = jobs.calls[before:]
    assert "try_claim" in after
    assert "claim_next" not in after and "queued" not in after, after


def test_an_idle_worker_sends_nothing(
    running_worker: tuple[Worker, CountingJobStore, CountingWorkerStore],
) -> None:
    """Five seconds is five listener checks at this test's setting — all local."""
    _worker, jobs, workers = running_worker
    jobs_before, workers_before = len(jobs.calls), len(workers.calls)

    time.sleep(5.0)

    assert jobs.calls[jobs_before:] == []
    assert workers.calls[workers_before:] == []


class NoChild:
    """The agent's own child process, which in this test it never started."""

    pid = None
    started_at = None

    def alive(self) -> bool:
        return False

    def exit_code(self) -> int | None:
        return None

    def ran_seconds(self) -> float | None:
        return None

    def log(self) -> list[str]:
        return []

    def note(self, line: str) -> None:
        pass

    def ask_stop(self) -> bool:
        return False


def test_a_ping_is_answered_by_the_agent_and_relayed_to_the_worker(
    running_worker: tuple[Worker, CountingJobStore, CountingWorkerStore],
    client: firestore.Client,
    scoped: Settings,
) -> None:
    """The full path a phone's ping takes: a write to the agent's document, the
    agent's listener, the agent's answer, the loopback relay, the worker's
    answer. The client half of the write is the rules tier's business; here it
    is written as the client would leave it."""
    agents = AgentStore(client, scoped)
    supervisor = Supervisor(
        process=NoChild(),  # type: ignore[arg-type]
        probe=lambda: probe_worker(scoped),
        control_port=scoped.local_api_port,
    )
    agent = Agent(settings=scoped, store=agents, supervisor=supervisor, tick_seconds=0.2)
    thread = threading.Thread(target=agent.run, daemon=True)
    thread.start()
    try:
        until(lambda: (agents.read() or {}).get("state") == AgentState.FOREIGN.value)
        ref = client.collection("agents").document("presence")
        ref.update(
            {
                "pingToken": "tok-presence",
                "pingRequestedAt": firestore.SERVER_TIMESTAMP,
                "pingRequestedBy": "user-1",
            }
        )

        until(lambda: (agents.read() or {}).get("answeredPing") == "tok-presence")
        until(
            lambda: (
                (snapshot := client.collection("workers").document("presence").get()).exists
                and (snapshot.to_dict() or {}).get("answeredPing") == "tok-presence"
            )
        )

        # The answer left the client's half alone.
        stored = agents.read() or {}
        assert stored["pingToken"] == "tok-presence"
        assert stored["pingRequestedBy"] == "user-1"
    finally:
        agent.request_stop()
        thread.join(timeout=10)


def test_the_agent_report_never_overwrites_the_clients_ping(
    client: firestore.Client, scoped: Settings
) -> None:
    """The model declares the ping fields — the PWA reads this document as an
    AgentReport — and a report written with their defaults would answer a ping
    by deleting it."""
    agents = AgentStore(client, scoped)
    supervisor = Supervisor(process=NoChild(), probe=lambda: None)  # type: ignore[arg-type]
    agent = Agent(settings=scoped, store=agents, supervisor=supervisor)
    agent._claim()
    client.collection("agents").document("presence").update(
        {"pingToken": "tok-1", "pingRequestedBy": "user-1"}
    )

    agent._claim()
    agent.step()

    stored = agents.read() or {}
    assert stored["pingToken"] == "tok-1"
    assert stored["pingRequestedBy"] == "user-1"
    assert stored["answeredPing"] == "tok-1"
