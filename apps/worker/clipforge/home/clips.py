"""Which file "play clip X" means, and whether the caller may have it.

The listener's ``media_link`` for ClipForge (the home-route blueprint, section
5b). Three checks, in the order that costs least when it says no:

1. **The kind.** Only ``clip``. A phone has no other reason to ask: posters
   and filmstrips travel in Firestore already, and a source video is someone
   else's footage that never leaves this machine.
2. **The account.** ``users/{uid}`` must say APPROVED, the same rule
   firestore.rules applies to every read (``isApproved``). Cached for a minute
   per account, so a card list of twenty clips costs one read rather than
   twenty, and a revoked account is out within the minute.
3. **The file.** The clip's own record names it (``localPath``, written by the
   worker), and it is served only if it resolves inside the worker's
   ``workspace/clips`` with a video suffix. Resolved paths are compared, as in
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

__all__ = ["APPROVAL_TTL_SECONDS", "ClipMedia", "worker_workspace"]

# The repository root, found relative to this file: apps/worker/clipforge/home/clips.py
_REPO_ROOT = Path(__file__).resolve().parents[4]

APPROVAL_TTL_SECONDS = 60.0

CONTENT_TYPES = {".mp4": "video/mp4", ".webm": "video/webm"}


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


class ClipMedia:
    """``media_link`` for the listener: (kind, id, caller) -> (path, content type)."""

    def __init__(
        self,
        settings: Settings,
        *,
        approved: Callable[[str], bool],
        local_path: Callable[[str], str | None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clips_root = worker_workspace(settings) / "clips"
        self._approved = approved
        self._local_path = local_path
        self._clock = clock
        self._lock = threading.Lock()
        self._approval: dict[str, tuple[bool, float]] = {}

    @classmethod
    def from_firestore(cls, settings: Settings, client: Any) -> ClipMedia:
        from clipforge_contracts import UserStatus

        from clipforge.store.firestore import ClipStore
        from clipforge.store.users import UserStore

        users = UserStore(client, settings)
        clips = ClipStore(client, settings)

        def approved(uid: str) -> bool:
            profile = users.get(uid)
            return profile is not None and profile.status == UserStatus.APPROVED

        def local_path(clip_id: str) -> str | None:
            clip = clips.get(clip_id)
            return clip.local_path if clip is not None else None

        return cls(settings, approved=approved, local_path=local_path)

    def __call__(self, kind: str, item_id: str, caller: Caller) -> tuple[str, str]:
        if kind != "clip":
            raise LookupError("Only clips play over the home wifi.")
        if not self._is_approved(caller.uid):
            raise PermissionError("This account has not been approved.")
        recorded = self._local_path(item_id)
        if not recorded:
            raise LookupError("No such clip.")
        path = self._confined(recorded)
        if path is None:
            raise LookupError("That clip's file is not on this PC any more.")
        return str(path), CONTENT_TYPES[path.suffix.lower()]

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

    def _confined(self, recorded: str) -> Path | None:
        # Windows paths may arrive with either slash.
        candidate = Path(recorded.replace("\\", "/")).expanduser()
        try:
            resolved = candidate.resolve()
            root = self._clips_root
        except OSError:
            return None
        if root not in resolved.parents:
            return None
        if resolved.suffix.lower() not in CONTENT_TYPES:
            return None
        if not resolved.is_file():
            return None
        return resolved
