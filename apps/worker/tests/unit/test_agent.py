"""The supervisor that starts and stops the worker on request.

Tested against a stand-in process rather than a real worker, for the same reason
test_worker_routes.py uses a stand-in worker: what is under test is the
*decisions* — when to spawn, when to give up, what never to kill — and a real
worker would test Firestore and the scheduler instead.

The cases worth having are the ones where getting it wrong is expensive rather
than merely wrong: starting a second worker beside one that is already running
(two processes, one GPU lane), killing a worker instead of asking it to stop (a
job stranded behind a lease nobody renews), and restarting a broken install
forever (a log of identical failures with the reason scrolled off the top).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from clipforge.agent import Agent, Desire, Snapshot, Supervisor, desire_from, ping_from
from clipforge.config import Settings
from clipforge_contracts import AgentDesired, AgentReport, AgentState

pytestmark = pytest.mark.unit

START = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)


class FakeProcess:
    """A worker that can be started, asked to stop, and made to die on cue."""

    def __init__(self, *, fails_to_start: str | None = None) -> None:
        self.fails_to_start = fails_to_start
        self.starts = 0
        self.stops_asked = 0
        self.kills = 0
        self.pid: int | None = None
        self.started_at: datetime | None = None
        self._alive = False
        self._exit_code: int | None = None
        self._ran_seconds: float | None = None
        self._log: list[str] = []

    # ── The interface the supervisor uses ────────────────────────────────────

    def alive(self) -> bool:
        return self._alive

    def exit_code(self) -> int | None:
        return self._exit_code

    def ran_seconds(self) -> float | None:
        return self._ran_seconds

    def log(self) -> list[str]:
        return list(self._log)

    def note(self, line: str) -> None:
        self._log.append(line)

    def start(self) -> None:
        if self.fails_to_start:
            raise RuntimeError(self.fails_to_start)
        self.starts += 1
        self._alive = True
        self.pid = 1000 + self.starts
        self.started_at = START
        self._exit_code = None

    def ask_stop(self) -> bool:
        self.stops_asked += 1
        return True

    def kill(self) -> None:
        self.kills += 1
        self.die(exit_code=-9)

    # ── What a test drives ───────────────────────────────────────────────────

    def die(self, *, exit_code: int = 1, ran_seconds: float = 3.0) -> None:
        self._alive = False
        self._exit_code = exit_code
        self._ran_seconds = ran_seconds
        self.pid = None
        self.started_at = None


class DeafProcess(FakeProcess):
    """A worker whose control API never answers, so asking it to stop does nothing."""

    def ask_stop(self) -> bool:
        self.stops_asked += 1
        return False


def supervisor(
    process: FakeProcess,
    *,
    foreign: dict[str, Any] | None = None,
    answers: bool = True,
    restart_limit: int = 5,
) -> Supervisor:
    """A supervisor wired to a probe that behaves like the real control API.

    That API is one port, answered by whichever worker holds it — so the probe
    reports *our* process while it is alive and the ``foreign`` one otherwise.
    Getting this wrong in the fixture would be worth catching in itself: a probe
    that answered before anything was started is how a supervisor concludes a
    worker is already running and never starts one.

    ``answers=False`` is the worker whose control port was taken by something
    else: up, working, and unable to say so.
    """

    def probe() -> dict[str, Any] | None:
        if process.alive():
            return {"pid": process.pid} if answers else None
        return foreign

    return Supervisor(
        process=process,  # type: ignore[arg-type]
        probe=probe,
        restart_limit=restart_limit,
        backoff_seconds=10,
        shutdown_grace_seconds=15,
    )


def run(desire: AgentDesired) -> Desire:
    """The standing wish, stamped by value.

    Pressing Stop after a Start is a *different request*, and the supervisor is
    built to notice — so each value carries its own timestamp here. Asking for
    the same value twice therefore repeats the request rather than making a new
    one, which is exactly what a UI re-rendering a wish it already wrote does.
    """
    at = START if desire is AgentDesired.RUNNING else START + timedelta(seconds=1)
    return Desire(state=desire, requested_at=at, requested_by="alice")


# ── Starting ─────────────────────────────────────────────────────────────────


def test_a_wish_to_run_starts_a_worker() -> None:
    process = FakeProcess()
    result = supervisor(process).reconcile(run(AgentDesired.RUNNING), now=START)

    assert process.starts == 1
    assert result.state is AgentState.STARTING


def test_starting_becomes_running_once_the_worker_answers_for_itself() -> None:
    """A live process says the interpreter started, not that the worker did.

    The control API answering is the first moment anything has confirmed the
    scheduler is up, which is what the person who pressed Start actually wants
    to know.
    """
    process = FakeProcess()
    agent = supervisor(process)

    agent.reconcile(run(AgentDesired.RUNNING), now=START)
    result = agent.reconcile(run(AgentDesired.RUNNING), now=START + timedelta(seconds=2))

    assert result.state is AgentState.RUNNING
    assert result.detail is None


def test_a_worker_that_never_answers_is_still_reported_as_running() -> None:
    """Sitting at "Starting…" forever describes a port clash worse than saying so."""
    process = FakeProcess()
    agent = supervisor(process, answers=False)

    agent.reconcile(run(AgentDesired.RUNNING), now=START)
    waiting = agent.reconcile(run(AgentDesired.RUNNING), now=START + timedelta(seconds=10))
    assert waiting.state is AgentState.STARTING

    settled = agent.reconcile(run(AgentDesired.RUNNING), now=START + timedelta(seconds=120))
    assert settled.state is AgentState.RUNNING
    assert settled.detail is not None
    assert "control port" in settled.detail


def test_it_does_not_start_a_second_worker_beside_its_own() -> None:
    process = FakeProcess()
    agent = supervisor(process)

    for tick in range(5):
        agent.reconcile(run(AgentDesired.RUNNING), now=START + timedelta(seconds=tick))

    assert process.starts == 1


def test_a_worker_it_did_not_start_is_reported_rather_than_duplicated() -> None:
    """Two workers on one machine are safe, and would still fight over one GPU lane.

    The lease protocol makes a second worker *correct*; it does not make it a
    good idea, and the agent is the only party in a position to notice.
    """
    process = FakeProcess()
    agent = supervisor(process, foreign={"pid": 4242})

    result = agent.reconcile(run(AgentDesired.RUNNING), now=START)

    assert process.starts == 0
    assert result.state is AgentState.FOREIGN
    assert result.pid == 4242


def test_a_worker_that_cannot_be_spawned_at_all_gives_up_immediately() -> None:
    """No uv and no checkout is not a crash loop, and retrying it buries the reason."""
    process = FakeProcess(fails_to_start="cannot find uv")
    agent = supervisor(process)

    first = agent.reconcile(run(AgentDesired.RUNNING), now=START)
    assert first.state is AgentState.FAILED
    assert first.detail == "cannot find uv"

    later = agent.reconcile(run(AgentDesired.RUNNING), now=START + timedelta(minutes=5))
    assert later.state is AgentState.FAILED


# ── Stopping ─────────────────────────────────────────────────────────────────


def test_a_wish_to_stop_asks_rather_than_kills() -> None:
    """The difference between "requeued now" and "requeued whenever someone notices".

    A killed worker leaves its job RUNNING behind a lease nobody is renewing,
    and the reaper that would recover it lives inside the worker that is no
    longer running.
    """
    process = FakeProcess()
    agent = supervisor(process)
    agent.reconcile(run(AgentDesired.RUNNING), now=START)

    result = agent.reconcile(run(AgentDesired.STOPPED), now=START + timedelta(seconds=2))

    assert process.stops_asked == 1
    assert process.kills == 0
    assert result.state is AgentState.STOPPING


def test_a_worker_that_stops_when_asked_ends_up_stopped() -> None:
    process = FakeProcess()
    agent = supervisor(process)
    agent.reconcile(run(AgentDesired.RUNNING), now=START)
    agent.reconcile(run(AgentDesired.STOPPED), now=START + timedelta(seconds=2))

    process.die(exit_code=0)
    result = agent.reconcile(run(AgentDesired.STOPPED), now=START + timedelta(seconds=4))

    assert result.state is AgentState.STOPPED
    assert process.kills == 0


def test_a_worker_that_ignores_the_ask_is_terminated_after_the_grace() -> None:
    process = DeafProcess()
    agent = supervisor(process)
    agent.reconcile(run(AgentDesired.RUNNING), now=START)

    agent.reconcile(run(AgentDesired.STOPPED), now=START + timedelta(seconds=2))
    assert process.kills == 0

    result = agent.reconcile(run(AgentDesired.STOPPED), now=START + timedelta(seconds=30))
    assert process.kills == 1
    assert result.detail is not None
    assert "terminated" in result.detail


def test_a_worker_it_did_not_start_is_asked_and_never_killed() -> None:
    """Something else started it and may well be watching it.

    Saying so is the honest answer; reaching for a pid the agent never spawned
    is how a supervisor breaks somebody else's session.
    """
    process = FakeProcess()
    agent = supervisor(process, foreign={"pid": 4242})
    agent.reconcile(run(AgentDesired.RUNNING), now=START)

    agent.reconcile(run(AgentDesired.STOPPED), now=START + timedelta(seconds=1))
    result = agent.reconcile(run(AgentDesired.STOPPED), now=START + timedelta(seconds=60))

    assert process.kills == 0
    assert process.stops_asked >= 1
    assert result.detail is not None
    assert "still running" in result.detail


def test_a_standing_stop_leaves_a_worker_somebody_else_started_alone() -> None:
    """An agent restarting beside a healthy worker must not shut it down.

    The wish is a record of the last thing anyone asked for, and yesterday's
    STOPPED is not an instruction about a worker started at the machine ten
    minutes ago. Stopping that one takes a person pressing Stop — and then it
    does, which is the case above.
    """
    process = FakeProcess()
    here: dict[str, Any] | None = None
    agent = Supervisor(
        process=process,  # type: ignore[arg-type]
        probe=lambda: here,
        backoff_seconds=10,
        shutdown_grace_seconds=15,
    )

    # The standing wish, seen while nothing was running.
    agent.reconcile(run(AgentDesired.STOPPED), now=START)

    here = {"pid": 4242}
    result = agent.reconcile(run(AgentDesired.STOPPED), now=START + timedelta(minutes=5))

    assert result.state is AgentState.FOREIGN
    assert process.stops_asked == 0
    assert result.detail is not None
    assert "left alone" in result.detail


def test_nothing_running_and_nothing_wanted_is_simply_stopped() -> None:
    result = supervisor(FakeProcess()).reconcile(run(AgentDesired.STOPPED), now=START)
    assert result.state is AgentState.STOPPED
    assert result.detail is None


# ── Dying ────────────────────────────────────────────────────────────────────


def test_a_worker_that_dies_while_it_is_wanted_is_restarted() -> None:
    process = FakeProcess()
    agent = supervisor(process)
    agent.reconcile(run(AgentDesired.RUNNING), now=START)

    process.die(exit_code=1)
    crashed = agent.reconcile(run(AgentDesired.RUNNING), now=START + timedelta(seconds=5))
    assert crashed.state is AgentState.STOPPED
    assert crashed.last_exit_code == 1
    assert crashed.restarts == 1

    # Not immediately: the backoff is what stops a broken worker from spinning.
    assert process.starts == 1
    agent.reconcile(run(AgentDesired.RUNNING), now=START + timedelta(seconds=30))
    assert process.starts == 2


def test_restarts_back_off_further_each_time() -> None:
    process = FakeProcess()
    agent = supervisor(process)
    now = START

    waits: list[int] = []
    for attempt in range(1, 4):
        agent.reconcile(run(AgentDesired.RUNNING), now=now)
        assert process.starts == attempt
        process.die(exit_code=1)
        now += timedelta(seconds=1)
        agent.reconcile(run(AgentDesired.RUNNING), now=now)

        # Walk forward a second at a time until it starts again, which is the
        # wait the supervisor actually imposed rather than the one it announced.
        waited = 0
        while process.starts == attempt:
            now += timedelta(seconds=1)
            waited += 1
            agent.reconcile(run(AgentDesired.RUNNING), now=now)
        waits.append(waited)

    assert waits == sorted(waits)
    assert waits[0] < waits[-1]


def test_a_worker_that_will_not_stay_up_is_given_up_on() -> None:
    process = FakeProcess()
    agent = supervisor(process, restart_limit=2)
    now = START

    for _ in range(6):
        now += timedelta(minutes=1)
        result = agent.reconcile(run(AgentDesired.RUNNING), now=now)
        if result.state is AgentState.FAILED:
            break
        process.die(exit_code=1)
        now += timedelta(seconds=1)
        agent.reconcile(run(AgentDesired.RUNNING), now=now)

    assert result.state is AgentState.FAILED
    assert process.starts == 3  # the limit, plus the attempt that exceeded it
    assert result.detail is not None
    assert "Not retrying" in result.detail


def test_pressing_start_again_is_what_clears_a_worker_it_gave_up_on() -> None:
    """The whole recovery story: read the log, fix the thing, press Start.

    A separate reset control would be one more thing to explain, and the person
    who fixed the problem is already pressing this one.
    """
    process = FakeProcess(fails_to_start="cannot find uv")
    agent = supervisor(process)
    assert agent.reconcile(run(AgentDesired.RUNNING), now=START).state is AgentState.FAILED

    process.fails_to_start = None
    later = Desire(
        state=AgentDesired.RUNNING,
        requested_at=START + timedelta(minutes=10),
        requested_by="alice",
    )
    result = agent.reconcile(later, now=START + timedelta(minutes=10))

    assert result.state is AgentState.STARTING
    assert result.restarts == 0
    assert process.starts == 1


def test_a_long_healthy_run_does_not_count_towards_the_crash_limit() -> None:
    """A worker that ran all afternoon and then died is not in a crash loop.

    Without this it would be one step closer to being abandoned for a reason
    nobody watching could see.
    """
    process = FakeProcess()
    agent = supervisor(process, restart_limit=2)

    # Two quick failures, then a run that lasted.
    for tick in (0, 60):
        agent.reconcile(run(AgentDesired.RUNNING), now=START + timedelta(seconds=tick))
        process.die(exit_code=1, ran_seconds=3.0)
        agent.reconcile(run(AgentDesired.RUNNING), now=START + timedelta(seconds=tick + 1))

    agent.reconcile(run(AgentDesired.RUNNING), now=START + timedelta(minutes=5))
    process.die(exit_code=1, ran_seconds=4 * 3600)
    result = agent.reconcile(run(AgentDesired.RUNNING), now=START + timedelta(hours=4))

    assert result.state is AgentState.STOPPED
    assert result.restarts == 1


def test_a_stop_that_was_asked_for_is_not_mistaken_for_a_crash() -> None:
    process = FakeProcess()
    agent = supervisor(process)
    agent.reconcile(run(AgentDesired.RUNNING), now=START)
    agent.reconcile(run(AgentDesired.STOPPED), now=START + timedelta(seconds=2))

    process.die(exit_code=0)
    result = agent.reconcile(run(AgentDesired.STOPPED), now=START + timedelta(seconds=4))

    assert result.restarts == 0
    assert result.detail is None


# ── Reading the wish ─────────────────────────────────────────────────────────


def test_a_missing_document_means_nobody_has_asked_for_anything() -> None:
    assert desire_from(None).state is AgentDesired.STOPPED
    assert desire_from({}).state is AgentDesired.STOPPED


def test_a_wish_carries_who_asked_and_when() -> None:
    wish = desire_from(
        {
            "desired": "RUNNING",
            "requestedBy": "alice",
            "requestedAt": "2026-09-12T09:00:00.000Z",
        }
    )
    assert wish.state is AgentDesired.RUNNING
    assert wish.requested_by == "alice"
    assert wish.requested_at == START


def test_an_unreadable_wish_never_invents_a_stop() -> None:
    """The one thing this must not do.

    A field it cannot parse is *absent*, not STOPPED — the caller keeps the wish
    it already had. Treating a malformed document as "stop" would let one bad
    write kill a render.
    """
    for broken in ({"desired": "GO"}, {"desired": 7}, {"desired": None}):
        assert desire_from(broken).requested_at is None


def test_a_supervisor_keeps_running_when_the_wish_becomes_unreadable() -> None:
    process = FakeProcess()
    agent = supervisor(process)
    agent.reconcile(run(AgentDesired.RUNNING), now=START)
    agent.reconcile(run(AgentDesired.RUNNING), now=START + timedelta(seconds=2))

    # What the Agent loop would hand over after failing to read the document is
    # the wish it already had, so the worker is untouched.
    result = agent.reconcile(run(AgentDesired.RUNNING), now=START + timedelta(seconds=4))

    assert result.state is AgentState.RUNNING
    assert process.stops_asked == 0


# ── What gets written ────────────────────────────────────────────────────────


def test_the_log_does_not_drive_a_firestore_write() -> None:
    """A running worker emits a line every few seconds.

    A document written on each one would be thousands of writes an hour saying
    nothing. The lines still travel with every state change and every answered
    ping.
    """
    quiet = Snapshot(state=AgentState.RUNNING, log=("one",))
    noisy = Snapshot(state=AgentState.RUNNING, log=("one", "two", "three"))
    assert quiet.steady() == noisy.steady()

    stopped = Snapshot(state=AgentState.STOPPED, log=("one",))
    assert quiet.steady() != stopped.steady()


# ── When the agent writes ────────────────────────────────────────────────────
#
# The decision this class exists to get right since it stopped beating: a write
# when something happens and when somebody asks, and never otherwise. Every
# write is billed, and billed again as a read on every listener open on the
# document — so an idle agent that wrote "still here" once a minute cost reads
# all day, for every phone left open on the Jobs page.


class FakeSubscription:
    def __init__(self) -> None:
        self.dead = False
        self.closed = False

    def alive(self) -> bool:
        return not self.dead

    def close(self) -> None:
        self.closed = True


class FakeAgentStore:
    """The agent's document, as far as the agent can tell."""

    agent_id = "tower"

    def __init__(self, document: dict[str, Any] | None = None, *, failing: int = 0) -> None:
        self.document = document
        self.reports: list[AgentReport] = []
        self.reads = 0
        self.subscriptions: list[FakeSubscription] = []
        self.deliver: Callable[[dict[str, Any] | None], None] | None = None
        self._failing = failing

    def read(self) -> dict[str, Any] | None:
        self.reads += 1
        return self.document

    def publish(self, report: AgentReport, *, claim: bool = False) -> None:
        if self._failing:
            self._failing -= 1
            raise ConnectionError("503 UNAVAILABLE")
        self.reports.append(report)

    def watch(self, on_change: Callable[[dict[str, Any] | None], None]) -> FakeSubscription:
        self.deliver = on_change
        subscription = FakeSubscription()
        self.subscriptions.append(subscription)
        return subscription


def agent_with(
    store: FakeAgentStore,
    *,
    process: FakeProcess | None = None,
    foreign: dict[str, Any] | None = None,
    relayed: list[str] | None = None,
    listener_check_seconds: float = 3600.0,
) -> Agent:
    def relay(token: str) -> bool:
        if relayed is not None:
            relayed.append(token)
        return True

    agent = Agent(
        settings=Settings(use_emulators=True),
        store=store,  # type: ignore[arg-type]
        supervisor=supervisor(process or FakeProcess(), foreign=foreign),
        listener_check_seconds=listener_check_seconds,
        relay=relay,
    )
    agent._claim()
    agent._subscribe()
    return agent


def doc(**fields: Any) -> dict[str, Any]:
    return {"desired": "STOPPED", **fields}


def deliver(store: FakeAgentStore, document: dict[str, Any]) -> None:
    assert store.deliver is not None, "the agent never subscribed"
    store.deliver(document)


def test_starting_up_is_itself_worth_one_report() -> None:
    """The process starting is an event: a client already waiting on a ping, or
    reading the panel, should learn that this machine is back."""
    store = FakeAgentStore(doc())
    agent = agent_with(store)

    agent.step(START)

    assert len(store.reports) == 1


def test_an_idle_agent_writes_nothing_and_reads_nothing() -> None:
    """The whole point. A thousand ticks of nothing changing cost nothing."""
    store = FakeAgentStore(doc())
    agent = agent_with(store)
    agent.step(START)
    reads = store.reads

    for tick in range(1, 1000):
        agent.step(START + timedelta(seconds=2 * tick))

    assert len(store.reports) == 1
    assert store.reads == reads


def test_a_change_of_state_is_reported_once() -> None:
    store = FakeAgentStore(doc())
    agent = agent_with(store, process=FakeProcess())
    agent.step(START)

    deliver(store, doc(desired="RUNNING", requestedAt="2026-09-12T09:00:00Z"))
    for tick in range(1, 6):
        agent.step(START + timedelta(seconds=tick))

    states = [report.state for report in store.reports]
    assert states == [AgentState.STOPPED, AgentState.STARTING, AgentState.RUNNING]


def test_a_ping_is_answered_with_its_token_and_passed_on_to_the_worker() -> None:
    """The agent answers for the machine, the worker answers for itself, and the
    client needs both: an agent saying RUNNING says a worker was running when it
    last looked, not that it is answering now."""
    store = FakeAgentStore(doc())
    relayed: list[str] = []
    agent = agent_with(store, foreign={"pid": 4242}, relayed=relayed)
    agent.step(START)

    deliver(store, doc(pingToken="tok-1"))
    agent.step(START + timedelta(seconds=30))

    assert relayed == ["tok-1"]
    answer = store.reports[-1]
    assert answer.answered_ping == "tok-1"
    assert answer.last_seen_at == START + timedelta(seconds=30)


def test_a_ping_is_answered_once() -> None:
    """Every delivery of the document carries the token — including the echo of
    the agent's own answer. Only a token it has not answered is a new question."""
    store = FakeAgentStore(doc())
    agent = agent_with(store)
    agent.step(START)

    deliver(store, doc(pingToken="tok-1"))
    agent.step(START + timedelta(seconds=1))
    deliver(store, doc(pingToken="tok-1", answeredPing="tok-1"))
    for tick in range(2, 20):
        agent.step(START + timedelta(seconds=tick))

    assert [report.answered_ping for report in store.reports] == [None, "tok-1"]


def test_a_ping_with_no_worker_running_is_not_relayed() -> None:
    """Asking a port nobody holds is a timeout spent on nothing. The agent's own
    answer — state STOPPED — already tells the client there is no worker."""
    store = FakeAgentStore(doc())
    relayed: list[str] = []
    agent = agent_with(store, relayed=relayed)
    agent.step(START)

    deliver(store, doc(pingToken="tok-1"))
    agent.step(START + timedelta(seconds=1))

    assert relayed == []
    assert store.reports[-1].answered_ping == "tok-1"
    assert store.reports[-1].state is AgentState.STOPPED


def test_the_answer_survives_later_reports() -> None:
    """A state change landing a moment after the answer must not erase it before
    the client that asked has seen it."""
    store = FakeAgentStore(doc())
    agent = agent_with(store)
    agent.step(START)
    deliver(store, doc(pingToken="tok-1"))
    agent.step(START + timedelta(seconds=1))

    deliver(store, doc(pingToken="tok-1", desired="RUNNING", requestedAt="2026-09-12T09:01:00Z"))
    agent.step(START + timedelta(seconds=2))

    assert store.reports[-1].state is AgentState.STARTING
    assert store.reports[-1].answered_ping == "tok-1"


def test_a_ping_made_while_the_machine_was_off_is_answered_on_startup() -> None:
    store = FakeAgentStore(doc(pingToken="tok-early", answeredPing="tok-older"))
    agent = agent_with(store)

    agent.step(START)

    assert store.reports[-1].answered_ping == "tok-early"


def test_a_ping_already_answered_by_an_earlier_run_is_not_a_new_question() -> None:
    store = FakeAgentStore(doc(pingToken="tok-1", answeredPing="tok-1"))
    relayed: list[str] = []
    agent = agent_with(store, foreign={"pid": 1}, relayed=relayed)

    agent.step(START)

    assert relayed == []


def test_a_report_that_could_not_be_written_is_tried_again() -> None:
    """Nothing is recorded as said until it has been said, so a failed answer to
    a ping is not mistaken for one that arrived."""
    store = FakeAgentStore(doc(), failing=1)
    agent = agent_with(store)

    agent.step(START)
    agent.step(START + timedelta(seconds=2))

    assert len(store.reports) == 1


def test_a_dead_listener_is_replaced_and_a_live_one_is_left_alone() -> None:
    """The backstop for a listener that died quietly used to be a read a minute.
    It is a local look at the listener's own state now, and a read only when it
    is actually dead — the replacement's first delivery."""
    store = FakeAgentStore(doc())
    agent = agent_with(store, listener_check_seconds=0.0)

    for tick in range(5):
        agent.step(START + timedelta(seconds=tick))
    assert len(store.subscriptions) == 1

    store.subscriptions[0].dead = True
    agent.step(START + timedelta(seconds=10))

    assert len(store.subscriptions) == 2
    assert store.subscriptions[0].closed


def test_a_ping_token_is_a_short_string_or_nothing() -> None:
    assert ping_from({"pingToken": "abc"}) == "abc"
    assert ping_from({"pingToken": ""}) is None
    assert ping_from({"pingToken": 12}) is None
    assert ping_from({"pingToken": "x" * 65}) is None
    assert ping_from(None) is None


def test_a_refresh_makes_the_next_tick_ask_the_worker_again() -> None:
    """A ping wants what is running now; the probe is otherwise throttled to ten
    seconds, which is exactly the staleness a ping exists to remove."""
    asked: list[int] = []

    def probe() -> dict[str, Any] | None:
        asked.append(1)
        return None

    watcher = Supervisor(process=FakeProcess(), probe=probe)  # type: ignore[arg-type]
    watcher.reconcile(Desire(), now=START)
    watcher.reconcile(Desire(), now=START + timedelta(seconds=1))
    assert len(asked) == 1

    watcher.refresh()
    watcher.reconcile(Desire(), now=START + timedelta(seconds=2))
    assert len(asked) == 2
