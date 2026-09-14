"""A supervisor that starts and stops the worker on behalf of someone who is not here.

## Why this exists

ClipForge has no server-side executor — there are no Cloud Functions, the claim
loop and the reaper both live inside the worker process, and a job therefore sits
QUEUED until something *on the machine* picks it up
(docs/adr/0006-lease-based-job-claiming.md). Until now the only things that could
pick it up were a terminal and the desktop shell, both of which require being at
the machine. So submitting work from a phone was possible and running it was not,
which made the phone a remote control with no batteries: it could ask for a clip
and then had to wait for someone to walk to the PC.

The agent is the missing half. It starts with Windows, holds a listener on one
Firestore document, and does what that document says. The PWA writes the wish;
the agent is the only party that can make it true, because it is the only one
with a process table.

## Why a wish in Firestore rather than a port on this machine

The obvious design is to let the phone call the worker's control API directly.
It cannot, and should not be able to: :mod:`clipforge.localapi` binds loopback
and refuses anything else, deliberately and with a threat model written down
(docs/adr/0011-local-control-api.md). Reaching it from a phone would mean
exposing a token-authenticated write surface to the LAN, or a tunnel, or a
dynamic DNS name — real infrastructure, guarding a button.

Firestore is already there, already authenticated, already has rules that say who
is approved, and already works from anywhere the PWA works. The agent holds the
connection *outbound*, so nothing on this machine listens for the internet. The
cost is a document write a minute and a listener that delivers nothing while
nothing changes.

## What the states mean

``STOPPED``   nothing is running here, and nothing is supposed to be.
``STARTING``  a worker has been spawned; its control API has not answered yet.
``RUNNING``   a worker this agent started is up.
``FOREIGN``   a worker is up that this agent did *not* start — from a terminal,
              from the desktop app, or from an earlier run of this agent. It is
              reported rather than duplicated: two workers on one machine are
              safe by the lease protocol but would fight over one GPU lane.
``STOPPING``  a clean stop has been asked for and not finished.
``FAILED``    the worker will not stay up. The agent has stopped retrying,
              because a fifth identical crash is not more information than the
              fourth. A fresh request from a person clears it.

## Why stopping is asked for, not done

The same reason the desktop shell asks (see
:mod:`clipforge.scheduler.control`): the worker's shutdown path returns its
in-flight jobs to the queue and flips its heartbeat to OFFLINE, and a killed
worker does neither — it leaves a job RUNNING behind a lease nobody is renewing,
recoverable only by the reaper, which lives inside the worker that is no longer
running. So the agent asks over loopback and kills only what it started, only
after the grace period, and only when asking did not work.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Any

import httpx
from clipforge_contracts import AgentDesired, AgentReport, AgentState

from clipforge.config import Settings
from clipforge.localapi import read_or_create_token
from clipforge.observability import get_logger
from clipforge.store.firestore import AgentStore
from clipforge.version import __version__

log = get_logger(__name__)

__all__ = [
    "Agent",
    "Desire",
    "Snapshot",
    "Supervisor",
    "WorkerProcess",
    "discover_uv",
    "redirect_output",
]

# The repository root, found relative to this file: apps/worker/clipforge/agent.py
REPO_ROOT = Path(__file__).resolve().parents[3]

UV_EXE = "uv.exe" if os.name == "nt" else "uv"

# How long a worker must stay up before its exit counts as "it ran, then
# something happened" rather than as another turn of a crash loop. Below this,
# restarts accumulate towards FAILED; above it, the counter resets. Without the
# distinction a worker that runs happily for six hours and then dies would be
# one step closer to being given up on, for no reason anybody could see.
HEALTHY_SECONDS = 120

# How long a freshly spawned worker is given to answer its own control API
# before the agent calls it RUNNING anyway. Generous, because a cold start
# imports a lot of Python; bounded, because the likely reason it never
# answers is a port clash, and sitting at "Starting…" forever describes that
# worse than saying so.
STARTUP_GRACE_SECONDS = 60


# ─────────────────────────────────────────────────────────────────────────────
# Finding the things that start a worker
# ─────────────────────────────────────────────────────────────────────────────


def discover_uv() -> Path | None:
    """Locate ``uv``, which is how every other part of ClipForge runs the worker.

    PATH first, then the two places its installers put it. Searching beyond PATH
    is not paranoia here: this process is started by Task Scheduler at logon,
    which hands it a smaller environment than a shell would, and "the worker
    starts when I run it but not when the agent does" is a confusing way to
    discover that.
    """
    on_path = shutil.which("uv")
    if on_path:
        return Path(on_path)

    candidates = [Path.home() / ".local" / "bin" / UV_EXE, Path.home() / ".cargo" / "bin" / UV_EXE]

    # winget installs into a versioned package directory whose name carries the
    # version, so it has to be searched for rather than named.
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        packages = Path(local_appdata) / "Microsoft" / "WinGet" / "Packages"
        if packages.is_dir():
            candidates += [
                entry / UV_EXE
                for entry in packages.iterdir()
                if entry.name.startswith("astral-sh.uv")
            ]

    return next((candidate for candidate in candidates if candidate.is_file()), None)


def worker_command(repo_root: Path = REPO_ROOT) -> list[str]:
    """The argv that starts a worker, preferring ``uv`` and falling back to us.

    ``uv run`` is the documented way (docs/adr/0003-uv-managed-python-toolchain.md)
    and is what tools/worker.ps1 and the desktop shell both use, so preferring it
    keeps one answer to "how does a worker start". The fallback is this very
    interpreter, which by construction is already inside the worker's
    environment — the agent imports :mod:`clipforge`, so a worker can too. It
    exists because the alternative to a fallback is an agent that autostarts
    perfectly and then cannot do the one thing it is for.
    """
    uv = discover_uv()
    if uv is not None:
        return [
            str(uv),
            "run",
            "--directory",
            str(repo_root / "apps" / "worker"),
            "clipforge-worker",
            "run",
        ]

    # pythonw.exe has no console and cannot be given pipes usefully on Windows;
    # its console-attached sibling sits next to it in the same environment.
    interpreter = Path(sys.executable)
    if interpreter.name.lower() == "pythonw.exe":
        console = interpreter.with_name("python.exe")
        if console.is_file():
            interpreter = console
    return [str(interpreter), "-m", "clipforge", "run"]


def redirect_output(path: Path, *, max_bytes: int = 5 * 1024 * 1024) -> Path:
    """Send this process's output to a file, because nobody is watching a console.

    Task Scheduler starts the agent with ``pythonw.exe``, which has no console at
    all: ``sys.stdout`` is ``None``, and the logging configured a few lines later
    would fail on it. So the redirect has to happen before anything logs, and the
    file has to exist, because an agent that will not start is invisible without
    one.

    Rotation is one generation and a size check at startup, which is all this
    needs: the agent logs a handful of lines a minute, and a real rotating
    handler would be a scheduled thread and a locking story in aid of a file
    nobody reads until something breaks.
    """
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.stat().st_size > max_bytes:
        with contextlib.suppress(OSError):
            previous = path.with_name(path.name + ".1")
            previous.unlink(missing_ok=True)
            path.rename(previous)
    stream = path.open("a", encoding="utf-8", buffering=1)
    sys.stdout = stream
    sys.stderr = stream
    return path


def probe_worker(settings: Settings, *, timeout: float = 3.0) -> dict[str, Any] | None:
    """Ask the local control API what worker is running here, if any.

    This is how a worker nobody in this process started becomes visible. It is
    also the only *confirmation* available that a worker which was just spawned
    actually came up: the process being alive says the interpreter started, not
    that the scheduler did.

    Returns ``None`` for every kind of no-answer. A refused connection, a
    timeout and a 401 are different problems for a human, but they are the same
    answer to the question being asked here — nothing is usefully running — and
    the detail goes to the log rather than into a return type the caller would
    have to branch on.
    """
    if not settings.local_api_enabled:
        return None
    try:
        token = read_or_create_token(settings.local_api_token_file)
        response = httpx.get(
            f"http://127.0.0.1:{settings.local_api_port}/worker",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except (httpx.HTTPError, OSError) as exc:
        log.debug("agent.probe_failed", error=str(exc))
        return None

    if response.status_code != 200:
        log.debug("agent.probe_refused", status=response.status_code)
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def ask_worker_to_stop(settings: Settings, *, timeout: float = 5.0) -> bool:
    """Ask whatever worker is listening here to shut down cleanly."""
    if not settings.local_api_enabled:
        return False
    try:
        token = read_or_create_token(settings.local_api_token_file)
        response = httpx.post(
            f"http://127.0.0.1:{settings.local_api_port}/worker/shutdown",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except (httpx.HTTPError, OSError) as exc:
        log.debug("agent.shutdown_unreachable", error=str(exc))
        return False
    return response.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# The child process
# ─────────────────────────────────────────────────────────────────────────────


class WorkerProcess:
    """One worker, spawned and watched.

    Owns the pipes as well as the process, and that is not incidental: a pipe
    nobody drains fills up and blocks the process writing into it, so a worker
    whose output was merely redirected would wedge partway through its first
    noisy job and look exactly like a hang. Draining also gives the agent the
    last few lines to publish, which is what turns "it did not start" on a phone
    into a reason.
    """

    def __init__(
        self, *, settings: Settings, repo_root: Path = REPO_ROOT, log_lines: int = 20
    ) -> None:
        self._settings = settings
        self._repo_root = repo_root
        self._log: deque[str] = deque(maxlen=max(log_lines, 1))
        self._log_lock = threading.Lock()
        self._child: subprocess.Popen[str] | None = None
        self._started_at: datetime | None = None
        self._exit_code: int | None = None
        self._ran_seconds: float | None = None

    # ── What it is ───────────────────────────────────────────────────────────

    @property
    def pid(self) -> int | None:
        return None if self._child is None else self._child.pid

    @property
    def started_at(self) -> datetime | None:
        return self._started_at

    def alive(self) -> bool:
        if self._child is None:
            return False
        if self._child.poll() is None:
            return True
        # Reaped here rather than in the caller, so the exit code — and how
        # long the run lasted — survive the handle being cleared and can still
        # be reported afterwards. The supervisor needs the duration to tell a
        # crash loop from a worker that ran all afternoon and then died.
        self._exit_code = self._child.returncode
        if self._started_at is not None:
            self._ran_seconds = (datetime.now(UTC) - self._started_at).total_seconds()
        self._child = None
        self._started_at = None
        return False

    def exit_code(self) -> int | None:
        return self._exit_code

    def ran_seconds(self) -> float | None:
        """How long the last finished run lasted, in seconds."""
        return self._ran_seconds

    def log(self) -> list[str]:
        with self._log_lock:
            return list(self._log)

    def note(self, line: str) -> None:
        """Add the agent's own commentary to the worker's output.

        One stream rather than two, because the interleaving is the story:
        "starting", then three lines of Python traceback, then "exited with code
        1" reads as an explanation, while the same lines in two panes read as a
        puzzle.
        """
        with self._log_lock:
            self._log.append(line)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn a worker pointed at the same backend this agent is.

        The environment is pinned from *this process's* settings rather than
        left to `.env`, for the reason the desktop shell pins it too: `.env`
        deliberately holds ``CLIPFORGE_USE_EMULATORS=true`` so routine
        development cannot touch the real project, and a worker started without
        an override would poll an emulator while the phone that asked for it
        watched production. The queue would then sit there looking broken, with
        a healthy worker running beside it.
        """
        if self.alive():
            raise RuntimeError("a worker started by this agent is already running")

        argv = worker_command(self._repo_root)
        environment = dict(os.environ)
        environment["CLIPFORGE_USE_EMULATORS"] = "true" if self._settings.use_emulators else "false"
        # Without the control API the agent cannot confirm a start or ask for a
        # clean stop, so it is switched on regardless of what the file says.
        environment["CLIPFORGE_LOCAL_API_ENABLED"] = "true"
        # Python buffers stdout when it is a pipe, and an unflushed log looks
        # from the outside exactly like a worker that has hung.
        environment["PYTHONUNBUFFERED"] = "1"
        # Uploading clips is part of going live, not a separate switch — the
        # same pairing tools/worker.ps1 -Live makes.
        if not self._settings.use_emulators and self._settings.firebase_storage_bucket:
            environment["CLIPFORGE_BLOB_STORE"] = "firebase"

        # CREATE_NO_WINDOW, without which every start flashes a console window
        # on the desktop of whoever happens to be logged in. Read by name rather
        # than referenced directly so this module still imports on a platform
        # where the constant does not exist.
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        creation_flags = no_window if sys.platform == "win32" else 0

        try:
            child = subprocess.Popen(  # noqa: S603 - argv is built here, never from input
                argv,
                cwd=self._repo_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creation_flags,
            )
        except OSError as exc:
            raise RuntimeError(f"could not start the worker: {exc}") from exc

        self._child = child
        self._started_at = datetime.now(UTC)
        self._exit_code = None
        self._ran_seconds = None
        with self._log_lock:
            self._log.clear()
        self.note(f"starting: {' '.join(argv)}")
        self.note(
            "target: local Emulator Suite"
            if self._settings.use_emulators
            else f"target: the live project ({self._settings.firebase_project_id})"
        )

        # stderr is folded into stdout above, so one reader drains everything.
        # structlog writes to stdout and tracebacks to stderr, and keeping them
        # in one stream keeps a crash next to the line that preceded it.
        reader = threading.Thread(target=self._drain, args=(child,), name="worker-log", daemon=True)
        reader.start()

    def _drain(self, child: subprocess.Popen[str]) -> None:
        stream = child.stdout
        if stream is None:
            return
        with contextlib.suppress(ValueError, OSError):
            # ValueError: the pipe was closed under us by a kill.
            for line in stream:
                self.note(line.rstrip())

    def ask_stop(self) -> bool:
        """Ask for a clean shutdown. ``False`` if the worker could not be asked."""
        return ask_worker_to_stop(self._settings)

    def kill(self) -> None:
        """The backstop, for a worker too wedged to answer its own control API."""
        if self._child is None:
            return
        with contextlib.suppress(OSError):
            self._child.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self._child.wait(timeout=5)
        self.note("worker terminated")
        self._exit_code = self._child.returncode
        if self._started_at is not None:
            self._ran_seconds = (datetime.now(UTC) - self._started_at).total_seconds()
        self._child = None
        self._started_at = None


# ─────────────────────────────────────────────────────────────────────────────
# The state machine
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Desire:
    """What the people asked for, and when they asked.

    ``requested_at`` is carried rather than just the value, because pressing
    Start on a worker that is already wanted has to be distinguishable from the
    wish simply still being RUNNING — that press is how a person clears FAILED,
    and a supervisor comparing only the value could not see it.
    """

    state: AgentDesired = AgentDesired.STOPPED
    requested_at: datetime | None = None
    requested_by: str | None = None


@dataclass(frozen=True)
class Snapshot:
    """Everything the agent knows about this machine, ready to publish."""

    state: AgentState
    detail: str | None = None
    pid: int | None = None
    started_at: datetime | None = None
    last_exit_code: int | None = None
    restarts: int = 0
    log: tuple[str, ...] = field(default_factory=tuple)

    def steady(self) -> tuple[Any, ...]:
        """The part of a snapshot whose changing is worth a Firestore write.

        Everything except the log, and that exclusion is the point: a running
        worker emits a line every few seconds, and a document written on each
        one would be thousands of writes an hour telling nobody anything. The
        log still reaches the document — every state change carries the current
        lines with it, and so does each heartbeat — it just does not *drive* the
        write.
        """
        return (
            self.state,
            self.detail,
            self.pid,
            self.started_at,
            self.last_exit_code,
            self.restarts,
        )


class Supervisor:
    """Turn a wish into a process, one tick at a time.

    Deliberately knows nothing about Firestore. Everything it needs from the
    outside arrives as a :class:`WorkerProcess` and a probe callable, so the
    interesting behaviour — restart backoff, giving up, not spawning a second
    worker next to one that is already there — is testable without a network,
    an emulator or a real worker.
    """

    def __init__(
        self,
        *,
        process: WorkerProcess,
        probe: Callable[[], dict[str, Any] | None],
        control_port: int = 8767,
        restart_limit: int = 5,
        backoff_seconds: int = 15,
        shutdown_grace_seconds: int = 15,
        probe_seconds: int = 10,
        startup_grace_seconds: int = STARTUP_GRACE_SECONDS,
    ) -> None:
        self._process = process
        self._probe = probe
        self._control_port = control_port
        self._restart_limit = restart_limit
        self._backoff_seconds = backoff_seconds
        self._shutdown_grace = timedelta(seconds=shutdown_grace_seconds)
        self._probe_seconds = probe_seconds
        self._startup_grace = timedelta(seconds=startup_grace_seconds)

        self._state = AgentState.STOPPED
        self._detail: str | None = None
        self._restarts = 0
        self._last_exit: int | None = None
        self._retry_at: datetime | None = None
        self._asked_to_stop = False
        self._stop_authorised = False
        self._stop_deadline: datetime | None = None
        self._seen_request: datetime | None = None
        self._foreign_pid: int | None = None
        # What the worker says about itself, whoever started it. Recorded from
        # every probe because it is the better answer than either alternative:
        # the agent's own child is `uv run`, a wrapper whose pid is not the
        # worker's, and a foreign worker has no child pid here at all.
        self._reported_pid: int | None = None
        self._reported_started_at: datetime | None = None
        self._probed_at: datetime | None = None

    # ── What to publish ──────────────────────────────────────────────────────

    def snapshot(self) -> Snapshot:
        # The worker's own answer when there is one. `uv run` is a wrapper, so
        # the child pid this agent holds belongs to uv rather than to the worker
        # — and the pid is only useful to someone about to go looking for that
        # process in a task list.
        running = self._state in (AgentState.RUNNING, AgentState.FOREIGN, AgentState.STOPPING)
        return Snapshot(
            state=self._state,
            detail=self._detail,
            pid=(self._reported_pid if running else None) or self._process.pid,
            started_at=(self._reported_started_at if running else None) or self._process.started_at,
            last_exit_code=self._last_exit,
            restarts=self._restarts,
            log=tuple(self._process.log()),
        )

    # ── One tick ─────────────────────────────────────────────────────────────

    def reconcile(self, desire: Desire, *, now: datetime | None = None) -> Snapshot:
        """Move one step towards the wish, and say where that leaves things."""
        now = now or datetime.now(UTC)
        self._accept(desire, now)

        mine_alive = self._process.alive()
        if not mine_alive and self._state in (AgentState.STARTING, AgentState.RUNNING):
            self._mourn(now)

        foreign = None if mine_alive else self._foreign(now)
        running = mine_alive or foreign is not None

        if desire.state is AgentDesired.RUNNING:
            self._towards_running(now, mine_alive=mine_alive, foreign=foreign)
        elif running and (mine_alive or self._stop_authorised):
            self._towards_stopped(now, mine_alive=mine_alive)
        elif running:
            # Something is running here that this agent did not start, under a
            # wish nobody has restated since. Left alone deliberately: a standing
            # STOPPED from yesterday must not shut down a worker somebody started
            # at the machine ten minutes ago — which is exactly what an agent
            # restarting beside a healthy worker would otherwise do. Stopping it
            # takes a person pressing Stop, and then it does.
            self._state = AgentState.FOREIGN
            self._detail = (
                f"A worker this agent did not start is running here (pid {foreign}). "
                "Nothing has asked for it to stop, so it is being left alone."
            )

        elif self._state is not AgentState.FAILED:
            self._settle()

        return self.snapshot()

    # ── Reading the wish ─────────────────────────────────────────────────────

    def _accept(self, desire: Desire, now: datetime) -> None:
        """Notice a *fresh* request, as opposed to the standing one.

        A new request is a person acting, and it earns a clean slate: the
        restart counter goes back to zero and FAILED is cleared. That is the
        whole recovery story for a worker that would not stay up — look at the
        log, fix the thing, press Start — and it needs no separate reset button
        because pressing Start *is* the reset.
        """
        if desire.requested_at is None or desire.requested_at == self._seen_request:
            return
        self._seen_request = desire.requested_at
        self._restarts = 0
        self._retry_at = None
        # A person pressing Stop is what authorises stopping a worker this agent
        # never started. The standing wish alone does not.
        self._stop_authorised = desire.state is AgentDesired.STOPPED
        if self._state is AgentState.FAILED:
            self._state = AgentState.STOPPED
            self._detail = None
        self._process.note(f"requested: {desire.state.value.lower()}")

    def _foreign(self, now: datetime) -> int | None:
        """The pid of a worker running here that this agent did not start.

        Throttled, because it is an HTTP request on every tick otherwise and the
        answer changes rarely. Not throttled while STARTING: there the probe is
        the confirmation being waited for, and waiting an extra ten seconds to
        report a worker that is already up is ten seconds of a phone showing
        "Starting…" for no reason.
        """
        fresh_enough = (
            self._probed_at is not None
            and now - self._probed_at < timedelta(seconds=self._probe_seconds)
            and self._state is not AgentState.STARTING
        )
        if fresh_enough:
            return self._foreign_pid

        self._probed_at = now
        self._ask()
        self._foreign_pid = self._reported_pid
        return self._foreign_pid

    def _ask(self) -> dict[str, Any] | None:
        """Put the question to whatever worker holds the control port.

        One place, so that every answer updates what is reported. The pid and
        start time here are the *worker's* own, which is what someone reading
        the panel is going to look for in a task list.
        """
        report = self._probe()
        pid = None if report is None else report.get("pid")
        self._reported_pid = pid if isinstance(pid, int) else None
        self._reported_started_at = (
            None if report is None else _as_datetime(report.get("startedAt"))
        )
        return report

    # ── Towards running ──────────────────────────────────────────────────────

    def _towards_running(self, now: datetime, *, mine_alive: bool, foreign: int | None) -> None:
        if mine_alive:
            if self._state is AgentState.STOPPING:
                # On its way out, and now wanted again. Let it finish: a stop
                # that is half-done is not a state to start a second worker from,
                # and the next tick after it exits will start a fresh one.
                return
            if self._state is AgentState.STARTING and self._ask() is None:
                started = self._process.started_at
                if started is not None and now - started < self._startup_grace:
                    return
                # Up, but its control API never answered. Reported rather than
                # waited on forever: the likely cause is a port clash, which
                # costs the agent its clean stop but costs the worker nothing.
                self._detail = (
                    f"Running, but nothing answers on the control port "
                    f"(127.0.0.1:{self._control_port}). Stopping it may need a terminal."
                )
            else:
                self._detail = None
            self._state = AgentState.RUNNING
            self._asked_to_stop = False
            return

        if foreign is not None:
            self._state = AgentState.FOREIGN
            self._detail = (
                f"A worker this agent did not start is running here (pid {foreign}). "
                "Leaving it alone rather than starting a second one."
            )
            return

        if self._state is AgentState.FAILED:
            return
        if self._retry_at is not None and now < self._retry_at:
            return
        self._start(now)

    def _start(self, now: datetime) -> None:
        self._retry_at = None
        self._asked_to_stop = False
        try:
            self._process.start()
        except RuntimeError as exc:
            # A worker that cannot be spawned at all — no uv, no checkout — is
            # not a crash loop, and retrying it every fifteen seconds would bury
            # the reason in a log of identical failures.
            self._state = AgentState.FAILED
            self._detail = str(exc)
            self._process.note(str(exc))
            log.error("agent.start_failed", error=str(exc))
            return
        self._state = AgentState.STARTING
        self._detail = None
        self._probed_at = None
        log.info("agent.worker_started", pid=self._process.pid)

    def _mourn(self, now: datetime) -> None:
        """Account for a worker of ours that is no longer running."""
        code = self._process.exit_code()
        self._last_exit = code

        if self._asked_to_stop:
            self._asked_to_stop = False
            self._state = AgentState.STOPPED
            self._detail = None
            self._process.note("worker stopped")
            log.info("agent.worker_stopped", exit_code=code)
            return

        ran_for = self._process.ran_seconds()
        if ran_for is not None and ran_for >= HEALTHY_SECONDS:
            self._restarts = 0

        self._restarts += 1
        self._process.note(f"worker exited unexpectedly with code {code}")
        log.warning("agent.worker_exited", exit_code=code, restarts=self._restarts)

        if self._restarts > self._restart_limit:
            self._state = AgentState.FAILED
            self._detail = (
                f"The worker exited {self._restarts} times without staying up "
                f"(last exit code {code}). Not retrying — the log below is the last "
                "thing it said."
            )
            return

        wait = self._backoff_seconds * self._restarts
        self._state = AgentState.STOPPED
        self._retry_at = now + timedelta(seconds=wait)
        self._detail = f"The worker exited with code {code}. Restarting in {wait}s."

    # ── Towards stopped ──────────────────────────────────────────────────────

    def _towards_stopped(self, now: datetime, *, mine_alive: bool) -> None:
        if self._state is not AgentState.STOPPING:
            asked = self._process.ask_stop()
            self._asked_to_stop = asked
            self._state = AgentState.STOPPING
            self._stop_deadline = now + self._shutdown_grace
            self._detail = (
                "Asked to stop; waiting for the stage in flight to check-point."
                if asked
                else "Its control API did not answer."
            )
            self._process.note(
                "stop requested — waiting for stages to check-point"
                if asked
                else "could not reach the worker's control API"
            )
            return

        if self._stop_deadline is not None and now < self._stop_deadline:
            return

        if mine_alive:
            # Ours, and it would not go quietly. A kill strands the job it was
            # holding until a future worker's reaper notices, which is worse
            # than a clean stop and better than a supervisor that cannot stop
            # anything.
            self._process.kill()
            self._asked_to_stop = True
            self._detail = "It did not stop when asked, so it was terminated."
            return

        # Foreign and still there. Not ours to kill: something else started it
        # and may well be watching it. Said plainly instead, and asked again on
        # the next deadline — a render that was mid-encode when the stop was
        # asked for can legitimately take longer than the grace period.
        self._detail = (
            f"A worker this agent did not start (pid {self._foreign_pid}) has been asked "
            "to stop and is still running."
        )
        self._stop_deadline = now + self._shutdown_grace
        self._process.ask_stop()

    def _settle(self) -> None:
        if self._state is not AgentState.STOPPED:
            self._state = AgentState.STOPPED
            self._detail = None
        self._asked_to_stop = False
        self._stop_authorised = False
        self._stop_deadline = None


# ─────────────────────────────────────────────────────────────────────────────
# The loop
# ─────────────────────────────────────────────────────────────────────────────


def desire_from(document: dict[str, Any] | None) -> Desire:
    """Read the wish out of a raw agent document, forgivingly.

    Every coercion here is chosen so that a malformed field cannot stop a worker
    that is running: an unreadable ``desired`` is not treated as STOPPED, it is
    treated as *absent*, and the caller keeps the wish it already had. The one
    thing this function must never do is invent a stop.

    ``requestedAt`` arrives as three different types depending on who wrote it —
    an ISO string from the PWA, a Firestore timestamp from a previous agent
    write, and a ``datetime`` when the emulator round-trips one — so all three
    are accepted rather than the one that happened to be seen first.
    """
    if not document:
        return Desire()

    raw = document.get("desired")
    if not isinstance(raw, str):
        log.warning("agent.unreadable_wish", desired=raw)
        return Desire()
    try:
        state = AgentDesired(raw)
    except ValueError:
        log.warning("agent.unknown_wish", desired=raw)
        return Desire()

    requested_by = document.get("requestedBy")
    return Desire(
        state=state,
        requested_at=_as_datetime(document.get("requestedAt")),
        requested_by=requested_by if isinstance(requested_by, str) else None,
    )


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
    # A Firestore Timestamp, which the Python client hands back as its own type.
    converter = getattr(value, "ToDatetime", None) or getattr(value, "timestamp_pb", None)
    if converter is not None:
        with contextlib.suppress(Exception):
            converted = converter()
            if isinstance(converted, datetime):
                return converted if converted.tzinfo else converted.replace(tzinfo=UTC)
    return None


class Agent:
    """The listener, the tick, and the heartbeat.

    Three cadences, each for its own reason:

    - **The listener** delivers a change the moment it is written, so tapping
      Start on a phone is not waiting out a poll interval.
    - **The tick** is local and cheap — two seconds, no network — and is what
      notices the child process dying, because nothing external tells it.
    - **The heartbeat** is both the "this PC is on" signal and the backstop:
      it re-reads the wish once a minute, so a listener that has silently died
      costs a minute of lag rather than a supervisor that stops hearing
      anything. A read a minute is well inside the free daily quota and is the
      cheapest possible insurance against the one failure that would make this
      whole thing unreliable.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        store: AgentStore,
        supervisor: Supervisor,
        tick_seconds: float = 2.0,
        heartbeat_seconds: int = 60,
    ) -> None:
        self._settings = settings
        self._store = store
        self._supervisor = supervisor
        self._tick_seconds = tick_seconds
        self._heartbeat = timedelta(seconds=heartbeat_seconds)

        self._stop = threading.Event()
        self._wish = Desire()
        self._wish_lock = threading.Lock()
        self._started_at = datetime.now(UTC)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def install_signal_handlers(self) -> None:
        def _handle(signum: int, _frame: FrameType | None) -> None:
            log.info("agent.signal", signal=signum)
            self._stop.set()

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, _handle)

    def request_stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        """Reconcile until asked to stop.

        The worker is deliberately *not* stopped on the way out. The agent
        supervises the worker, it does not own it: killing a render halfway
        through because the supervisor was restarted would be a worse outcome
        than the one it is avoiding, and the next agent to start adopts what it
        finds as FOREIGN rather than duplicating it.
        """
        self._claim()
        unsubscribe = self._subscribe()

        published: tuple[Any, ...] | None = None
        last_write = datetime.min.replace(tzinfo=UTC)
        last_read = datetime.now(UTC)

        log.info(
            "agent.started",
            agent_id=self._store.agent_id,
            project=self._settings.firebase_project_id,
            emulators=self._settings.use_emulators,
        )
        try:
            while True:
                now = datetime.now(UTC)

                if now - last_read >= self._heartbeat:
                    last_read = now
                    self._reread()

                with self._wish_lock:
                    wish = self._wish
                snapshot = self._supervisor.reconcile(wish, now=now)

                steady = snapshot.steady()
                due = steady != published or now - last_write >= self._heartbeat
                if due and self._publish(wish, snapshot, now):
                    published = steady
                    last_write = now

                if self._stop.wait(self._tick_seconds):
                    break
        finally:
            with contextlib.suppress(Exception):
                unsubscribe()
            log.info("agent.stopped")

    # ── Firestore ────────────────────────────────────────────────────────────

    def _claim(self) -> None:
        """Make sure this machine has a document, without overwriting its wish."""
        existing = self._store.read()
        if existing is not None:
            with self._wish_lock:
                self._wish = desire_from(existing)
            return

        log.info("agent.first_run", agent_id=self._store.agent_id)
        self._store.publish(
            self._report(Desire(), self._supervisor.snapshot(), datetime.now(UTC)), claim=True
        )

    def _subscribe(self) -> Callable[[], None]:
        def _changed(document: dict[str, Any] | None) -> None:
            wish = desire_from(document)
            with self._wish_lock:
                changed = wish != self._wish
                self._wish = wish
            if changed:
                log.info("agent.wish", desired=wish.state.value, by=wish.requested_by)

        try:
            return self._store.watch(_changed)
        except Exception as exc:  # noqa: BLE001 - a listener is an optimisation, not a requirement
            # Without it the agent is a poller on the heartbeat interval, which
            # is slower but entirely correct. Worth degrading to rather than
            # refusing to start.
            log.warning("agent.listener_unavailable", error=str(exc))
            return lambda: None

    def _reread(self) -> None:
        try:
            document = self._store.read()
        except Exception as exc:  # noqa: BLE001 - the client raises many shapes of transient
            log.warning("agent.read_failed", error=str(exc))
            return
        wish = desire_from(document)
        with self._wish_lock:
            self._wish = wish

    def _publish(self, wish: Desire, snapshot: Snapshot, now: datetime) -> bool:
        try:
            self._store.publish(self._report(wish, snapshot, now))
        except Exception as exc:  # noqa: BLE001 - same
            # Not fatal, and deliberately not retried here: the next tick will
            # try again, and a supervisor that stopped supervising because it
            # could not file a report would have its priorities backwards.
            log.warning("agent.publish_failed", error=str(exc))
            return False
        return True

    def _report(self, wish: Desire, snapshot: Snapshot, now: datetime) -> AgentReport:
        return AgentReport(
            agent_id=self._store.agent_id,
            hostname=socket.gethostname(),
            version=__version__,
            desired=wish.state,
            requested_by=wish.requested_by,
            requested_at=wish.requested_at,
            state=snapshot.state,
            detail=snapshot.detail,
            worker_pid=snapshot.pid,
            worker_started_at=snapshot.started_at,
            last_exit_code=snapshot.last_exit_code,
            restarts=snapshot.restarts,
            log=list(snapshot.log),
            use_emulators=self._settings.use_emulators,
            project_id=self._settings.firebase_project_id,
            started_at=self._started_at,
            last_seen_at=now,
        )
