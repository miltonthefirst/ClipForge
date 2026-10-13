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

## Why there is a ping route

A client asks whether this machine is there by writing a token to the agent's
document; the agent hears it and passes it on here, and the worker answers by
writing its own document with the same token
(docs/adr/0030-status-on-events-and-pings.md). Relaying over loopback rather
than having the worker listen to the agent's document as well keeps the worker
to the listeners its own work needs, and costs nothing: the agent already talks
to this API every few seconds. It changes nothing about what the worker does —
it is asked to *say* something — so it does not widen what this surface can do
to the worker, which is the property docs/adr/0011-local-control-api.md guards.

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

from clipforge.config import get_settings
from clipforge.observability import get_logger
from clipforge.version import __version__

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime, types only
    from clipforge.scheduler.worker import Worker

log = get_logger(__name__)

__all__ = ["PING_TOKEN_MAX", "WorkerRoutes"]

#: The longest ping token the rules let a client write; see firestore.rules.
PING_TOKEN_MAX = 64


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
            ("GET", "/worker/settings"): self.settings,
            ("POST", "/worker/shutdown"): self.shutdown,
            ("POST", "/worker/ping"): self.ping,
        }

    # ── Handlers ─────────────────────────────────────────────────────────────

    def status(self, _payload: dict[str, Any]) -> dict[str, Any]:
        """What this process is, and what it is currently doing.

        Deliberately overlaps the worker document in Firestore rather than
        replacing it. That document answers "is a worker serving my queue?"
        from anywhere, including a phone, when it is pinged. This answers "is
        the process I started still alive?" instantly, and it keeps answering
        when Firestore is unreachable — which is exactly the failure the
        document cannot report on its own, because a worker that cannot write
        looks identical to a worker that is not running.
        """
        started = self._worker.started_at
        settings = get_settings()
        return {
            "pid": self._pid,
            "workerId": self._worker.worker_id,
            "version": __version__,
            "startedAt": started.isoformat(),
            "uptimeSeconds": int((datetime.now(UTC) - started).total_seconds()),
            "activeJobIds": self._worker.active_job_ids(),
            # Which database this worker is actually talking to. The app spawns
            # workers with its own mode pinned, so these agree by construction —
            # but a worker started from a terminal takes whatever .env says, and
            # a mismatch is otherwise invisible: the app reads one database and
            # the worker writes the other, so the worker panel shows "no worker
            # has ever reported in" while a healthy worker is running beside it.
            "useEmulators": settings.use_emulators,
            "projectId": settings.firebase_project_id,
        }

    def settings(self, _payload: dict[str, Any]) -> dict[str, Any]:
        """What this worker is configured with, in groups the app can just render.

        Returned as labelled rows rather than a field map, so the page does not
        carry a second copy of the settings model that goes stale the moment one
        is added here. The app renders what it is handed.

        Paths are included; contents never are. A path to a credential file is
        the thing an operator needs when it is pointed at the wrong one, and it
        is exactly what `doctor` prints — but nothing here reads a secret, and
        the values below are the same ones the worker logs at startup.
        """
        s = get_settings()

        def group(title: str, rows: list[tuple[str, Any]]) -> dict[str, Any]:
            return {
                "title": title,
                "rows": [{"label": label, "value": str(value)} for label, value in rows],
            }

        return {
            "groups": [
                group(
                    "Where it writes",
                    [
                        ("Project", s.firebase_project_id),
                        ("Database", "local emulators" if s.use_emulators else "live"),
                        ("Clip storage", s.blob_store),
                        ("Bucket", s.firebase_storage_bucket or "—"),
                        ("Clip retention", f"{s.clip_retention_days} days"),
                    ],
                ),
                group(
                    "This machine",
                    [
                        ("Worker id", s.worker_id or "(hostname)"),
                        ("Workspace", s.workspace_dir),
                        ("Workspace cap", f"{s.workspace_max_gb} GB"),
                        ("File server", s.local_server_origin if s.local_server_enabled else "off"),
                        ("Control API", f"127.0.0.1:{s.local_api_port}"),
                    ],
                ),
                group(
                    "Models",
                    [
                        ("Whisper", f"{s.whisper_model} on {s.whisper_device}"),
                        ("Compute type", s.whisper_compute_type),
                        ("Ollama", s.ollama_host),
                        ("Analysis model", s.ollama_model),
                        ("VRAM held back", f"{s.vram_reserve_mb} MiB"),
                    ],
                ),
                group(
                    "Media",
                    [
                        ("ffmpeg", s.ffmpeg_bin),
                        ("Encoder", s.video_encoder),
                        ("Render profile", s.render_profile),
                        ("Loudness target", f"{s.loudness_target_lufs} LUFS"),
                        ("Longest source", f"{int(s.max_source_duration_sec // 60)} min"),
                    ],
                ),
                group(
                    "Scheduling",
                    [
                        ("GPU lane", f"{s.gpu_lane_depth} job at a time"),
                        ("CPU lanes", f"{s.cpu_lane_depth} jobs at a time"),
                        ("Lease", f"{s.lease_seconds}s"),
                        ("Lease renewal, while busy", f"{s.heartbeat_seconds}s"),
                        ("Attempts before giving up", s.max_attempts),
                    ],
                ),
                group(
                    "Publishing",
                    [
                        ("Enabled", "yes" if s.publishing_enabled else "no"),
                        ("Default privacy", s.youtube_default_privacy),
                        ("OAuth client file", s.youtube_client_secrets),
                        ("Token file", s.youtube_token_store),
                        ("Authorisation port", s.youtube_auth_port),
                    ],
                ),
            ]
        }

    def ping(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Answer a client's ping, relayed by the agent. Returns without waiting.

        The token is checked here rather than trusted: it is echoed into a
        document every approved member reads, and the rules cap what a client
        may write at 64 characters, so anything longer did not come from one.
        """
        token = payload.get("token")
        if not isinstance(token, str) or not 1 <= len(token) <= PING_TOKEN_MAX:
            raise ValueError(f"token must be a string of 1 to {PING_TOKEN_MAX} characters")
        self._worker.ping(token)
        return {"pinged": True, "pid": self._pid}

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
        # Stop first, say so second: a request that cannot be logged must still
        # be honoured. Logging first is how a worker whose console had gone away
        # refused every Stop for hours.
        self._worker.request_stop()
        log.info("worker.shutdown_requested", source="localapi", active_jobs=len(active))
        return {"stopping": True, "pid": self._pid, "activeJobIds": active}
