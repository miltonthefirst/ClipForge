"""Which file "play clip X" (or "play track Y") means, and whether the caller may have it.

The listener's ``media_link`` for ClipForge (the home-route blueprint, section
5b). Three checks, in the order that costs least when it says no:

1. **The kind.** ``clip`` or ``track`` (generated music), nothing else.
   Posters and filmstrips travel in Firestore already, and a source video is
   someone else's footage that never leaves this machine.
2. **The account.** ``users/{uid}`` must say APPROVED, the same rule
   firestore.rules applies to every read (``isApproved``). Cached for a minute
   per account, so a card list of twenty clips costs one read rather than
   twenty, and a revoked account is out within the minute.
3. **The file.** The item's own record names it (``localPath``, written by the
   worker), and it is served only if it resolves inside the worker's
   ``workspace/clips`` (video) or ``workspace/music`` (audio), by kind. Resolved paths are compared, as in
   :mod:`clipforge.localserver`, so ``..`` and symlinks cannot reach outside.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clipforge.config import Settings
from clipforge.home.auth import Caller

__all__ = ["APPROVAL_TTL_SECONDS", "KINDS", "WorkspaceMedia", "worker_workspace"]

# The repository root, found relative to this file: apps/worker/clipforge/home/clips.py
_REPO_ROOT = Path(__file__).resolve().parents[4]

APPROVAL_TTL_SECONDS = 60.0

# What each kind may serve, and from where under the worker's workspace.
VIDEO_TYPES = {".mp4": "video/mp4", ".webm": "video/webm"}
AUDIO_TYPES = {
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
}
KINDS: dict[str, tuple[str, dict[str, str]]] = {
    "clip": ("clips", VIDEO_TYPES),
    # Generated music, imported by `clipforge-worker import-tracks`
    # (docs/MUSIC-PLAN.md). Under music/, where the MUSIC job's downloads live
    # too; only a track's own record can name a file, so those stay unreachable.
    "track": ("music", AUDIO_TYPES),
}


def worker_workspace(settings: Settings) -> Path:
    """The workspace as the WORKER resolves it, from whichever process asks.

    The worker runs from apps/worker (``uv run --directory apps/worker``), so a
    relative ``workspace_dir`` means apps/worker/workspace. The agent runs from
    the repository root, where ``./workspace`` is a different folder entirely;
    resolving it there would confine links to the wrong place and refuse every
    clip.
    """
    configured = Path(settings.workspace_dir).expanduser()
    if configured.is_absolute():
        return configured.resolve()
    return (_REPO_ROOT / "apps" / "worker" / configured).resolve()


class WorkspaceMedia:
    """``media_link`` for the listener: (kind, id, caller) -> (path, content type)."""

    def __init__(
        self,
        settings: Settings,
        *,
        approved: Callable[[str], bool],
        local_path: Callable[[str, str], str | None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._workspace = worker_workspace(settings)
        self._approved = approved
        self._local_path = local_path
        self._clock = clock
        self._lock = threading.Lock()
        self._approval: dict[str, tuple[bool, float]] = {}

    @classmethod
    def from_firestore(cls, settings: Settings, client: Any) -> WorkspaceMedia:
        from clipforge_contracts import UserStatus

        from clipforge.store.firestore import ClipStore, TrackStore
        from clipforge.store.users import UserStore

        users = UserStore(client, settings)
        clips = ClipStore(client, settings)
        tracks = TrackStore(client, settings)

        def approved(uid: str) -> bool:
            profile = users.get(uid)
            return profile is not None and profile.status == UserStatus.APPROVED

        def local_path(kind: str, item_id: str) -> str | None:
            record = clips.get(item_id) if kind == "clip" else tracks.get(item_id)
            return record.local_path if record is not None else None

        return cls(settings, approved=approved, local_path=local_path)

    def __call__(self, kind: str, item_id: str, caller: Caller) -> tuple[str, str]:
        if kind not in KINDS:
            raise LookupError("Only clips and tracks play over the home wifi.")
        if not self._is_approved(caller.uid):
            raise PermissionError("This account has not been approved.")
        recorded = self._local_path(kind, item_id)
        if not recorded:
            raise LookupError(f"No such {kind}.")
        folder, types = KINDS[kind]
        path = self._confined(recorded, self._workspace / folder, types)
        if path is None:
            raise LookupError(f"That {kind}'s file is not on this PC any more.")
        return str(path), types[path.suffix.lower()]

    def _is_approved(self, uid: str) -> bool:
        now = self._clock()
        with self._lock:
            cached = self._approval.get(uid)
        if cached is not None and now - cached[1] < APPROVAL_TTL_SECONDS:
            return cached[0]
        verdict = self._approved(uid)
        with self._lock:
            self._approval[uid] = (verdict, now)
        return verdict

    @staticmethod
    def _confined(recorded: str, root: Path, types: dict[str, str]) -> Path | None:
        # Windows paths may arrive with either slash.
        candidate = Path(recorded.replace("\\", "/")).expanduser()
        try:
            resolved = candidate.resolve()
        except OSError:
            return None
        if root not in resolved.parents:
            return None
        if resolved.suffix.lower() not in types:
            return None
        if not resolved.is_file():
            return None
        return resolved
