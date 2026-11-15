"""What the agent notices on its own (clipforge/notify).

The agent is the one process that is always on, so it tells people about the
things no job will: a job waiting because no worker is running, a worker that
will not stay up, someone signing up, and the Settings page's test.

Event-driven, like everything the agent does (ADR-0030): two listeners that
deliver nothing while nothing changes (queued jobs, pending accounts), and the
agent's own tick, which is local. What has been announced is remembered in a
small file, so restarting the agent does not say it all again.
"""

from __future__ import annotations

import contextlib
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from clipforge_contracts import AgentState, NotificationKind

from clipforge.notify import Notifier
from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = ["WAIT_BEFORE_TELLING", "AgentNotices"]

# How long a job may sit QUEUED with no worker before its owner hears about it.
# Long enough that starting the worker from the phone, which takes seconds,
# never triggers it.
WAIT_BEFORE_TELLING = timedelta(minutes=10)

# The worker states in which a queued job will be picked up by itself.
_WORKING = {AgentState.RUNNING, AgentState.STARTING, AgentState.FOREIGN}

# Remembered ids are pruned to this many, newest kept.
_REMEMBER = 500


class AgentNotices:
    def __init__(
        self,
        notifier: Notifier,
        client: Any,
        *,
        state_path: Path,
        clock: Any = lambda: datetime.now(UTC),
    ) -> None:
        self._notifier = notifier
        self._db = client
        self._path = state_path.expanduser()
        self._clock = clock
        self._lock = threading.Lock()
        self._queued: dict[str, tuple[datetime, str, str]] = {}  # id -> (since, uid, type)
        self._pending: set[str] = set()
        self._state = self._load()
        self._last_worker: AgentState | None = None
        self._watches: list[Any] = []

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        from google.cloud import firestore as gcf

        queued = self._db.collection("jobs").where(filter=gcf.FieldFilter("status", "==", "QUEUED"))
        pending = self._db.collection("users").where(
            filter=gcf.FieldFilter("status", "==", "PENDING")
        )
        try:
            self._watches = [
                queued.on_snapshot(self._on_queued),
                pending.on_snapshot(self._on_pending),
            ]
        except Exception as exc:  # noqa: BLE001 - notices are a nicety; the agent carries on
            log.warning("notices.listen_failed", error=str(exc))

    def stop(self) -> None:
        for watch in self._watches:
            with contextlib.suppress(Exception):  # shutting down
                watch.unsubscribe()
        self._watches = []

    # ── Deliveries (on the client library's thread: record and return) ───────

    def _on_queued(self, snapshots: Any, _changes: Any, _read_time: Any) -> None:
        now = self._clock()
        current: dict[str, tuple[datetime, str, str]] = {}
        for snapshot in snapshots:
            data = snapshot.to_dict() or {}
            not_before = _as_datetime(data.get("notBefore"))
            # A scheduled publish is meant to wait; its clock starts when it is due.
            since = max(now, not_before) if not_before and not_before > now else now
            with self._lock:
                known = self._queued.get(snapshot.id)
            current[snapshot.id] = known or (
                since,
                str(data.get("uid") or ""),
                str(data.get("type") or ""),
            )
        with self._lock:
            self._queued = current

    def record_queued(self, job_id: str, uid: str, job_type: str, since: datetime) -> None:
        """Test seam: what a listener delivery would record."""
        with self._lock:
            self._queued[job_id] = (since, uid, job_type)

    def _on_pending(self, snapshots: Any, _changes: Any, _read_time: Any) -> None:
        arrived = {snapshot.id: snapshot.to_dict() or {} for snapshot in snapshots}
        for uid, profile in arrived.items():
            if uid in self._state["signups"]:
                continue
            who = profile.get("displayName") or profile.get("email") or "Someone"
            self._notifier.notify(
                NotificationKind.SIGNUP,
                title="Someone signed up",
                body=f"{who} is waiting for approval.",
                link="/admin/users",
                fold=f"signup-{uid}",
            )
            self._remember("signups", uid)

    # ── The agent's tick and its document ─────────────────────────────────────

    def step(self, worker: AgentState, detail: str | None) -> None:
        """Called on every agent tick with the worker's state."""
        if worker is AgentState.FAILED and self._last_worker is not AgentState.FAILED:
            self._notifier.notify(
                NotificationKind.WORKER_DOWN,
                title="The worker will not stay up",
                body=detail or "The agent stopped restarting it. See the Worker page.",
                link="/settings/worker",
                fold="worker-down",
            )
        self._last_worker = worker
        if worker in _WORKING:
            return
        now = self._clock()
        with self._lock:
            waiting = list(self._queued.items())
        for job_id, (since, uid, job_type) in waiting:
            if job_id in self._state["waiting"] or now - since < WAIT_BEFORE_TELLING or not uid:
                continue
            minutes = int((now - since).total_seconds() // 60)
            self._notifier.notify(
                NotificationKind.JOB_WAITING,
                title="A job is waiting for the worker",
                body=f"Your {job_type.lower() or 'job'} has waited {minutes} minutes: "
                "the worker is not running. Start it from the Worker page.",
                link="/settings/worker",
                to=[uid],
                fold=f"waiting-{job_id}",
            )
            self._remember("waiting", job_id)

    def take(self, document: dict[str, Any] | None) -> None:
        """A delivery of the agent's own document: answer a test request."""
        document = document or {}
        by = document.get("notifyTestRequestedBy")
        at = _as_datetime(document.get("notifyTestRequestedAt"))
        if not isinstance(by, str) or at is None:
            return
        stamp = at.isoformat()
        if stamp == self._state.get("testAnswered"):
            return
        self._state["testAnswered"] = stamp
        self._save()
        self._notifier.notify(
            NotificationKind.TEST,
            title="Notifications work",
            body="This is how ClipForge will reach this device.",
            link="/notifications",
            to=[by],
        )

    # ── Memory ───────────────────────────────────────────────────────────────

    def _remember(self, key: str, item: str) -> None:
        with self._lock:
            items = [i for i in self._state[key] if i != item] + [item]
            self._state[key] = items[-_REMEMBER:]
        self._save()

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return {
            "waiting": list(data.get("waiting") or []),
            "signups": list(data.get("signups") or []),
            "testAnswered": data.get("testAnswered"),
        }

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(".tmp")
            with self._lock:
                temporary.write_text(json.dumps(self._state), encoding="utf-8")
            temporary.replace(self._path)
        except OSError as exc:
            log.warning("notices.save_failed", error=str(exc))


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None
