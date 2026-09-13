"""The routes the desktop app calls to see and clear what is on this disk.

## Why these are here and not in Firestore

Because the two are deliberately different things. Deleting a clip's record is a
statement about a review queue and it happens from anywhere, including a phone.
Removing the 400 MB behind it is a statement about *this machine*, and nothing
in Firestore knows what is on this machine's disk — the worker is the only thing
that does, and the local API is the only surface that reaches it.

That split is also why this can list files whose records are already gone. A
record deleted last week leaves a file nobody is tracking, and a storage screen
built from Firestore could never show it. This one walks the disk, so what it
shows is what is actually there.

## What a caller may name

A path, and only a path already inside the workspace. Everything here resolves
what it is given and refuses anything that lands outside — `..` in a path
segment would otherwise reach any file the worker can write, and the operation
on the other side is a move or a recursive delete. The token and the origin
check in :mod:`clipforge.localapi` guard the port; this guards the argument.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clipforge.media.trash import Trash, TrashError
from clipforge.media.workspace import Workspace
from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = ["StorageRoutes"]

# One listing should not have to paginate, and a workspace with more files than
# this has a different problem than a tidy-up screen can solve.
MAX_LISTED = 2000


class StorageRoutes:
    """Look at the workspace, move files to the bin, and empty it."""

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace
        self._trash = Trash(workspace.trash_dir)

    def table(self) -> dict[tuple[str, str], Any]:
        return {
            ("GET", "/storage"): self.listing,
            ("POST", "/storage/trash"): self.to_trash,
            ("POST", "/storage/trash/restore"): self.restore,
            ("POST", "/storage/trash/purge"): self.purge,
        }

    # ── Looking ──────────────────────────────────────────────────────────────

    def listing(self, _payload: dict[str, Any]) -> dict[str, Any]:
        """Every clip and source file on this disk, plus what is in the bin.

        Grouped by what the file IS rather than by where it sits, because the
        operator is deciding what to keep and "a source is 400 MB and a clip is
        6 MB" is the fact that decides it.
        """
        clips = _files_under(self._workspace.clips_dir, kind="clip")
        sources = _files_under(self._workspace.sources_dir, kind="source")
        binned = [item.as_json() for item in self._trash.items()]
        return {
            "root": str(self._workspace.root),
            "clips": clips,
            "sources": sources,
            "trash": binned,
            "usedBytes": self._workspace.used_bytes(),
            "maxBytes": self._workspace.max_bytes,
            "freeDiskBytes": self._workspace.free_disk_bytes(),
            # Reported separately and always, because it is excluded from
            # `usedBytes` on purpose: the bin can fill a disk while the
            # workspace says it is comfortable, and the only defence against
            # that is showing the number next to the button that empties it.
            "trashBytes": self._trash.used_bytes(),
        }

    # ── Moving ───────────────────────────────────────────────────────────────

    def to_trash(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Move named files into the bin. Nothing is deleted here."""
        moved: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        for raw in _paths(payload):
            try:
                path = self._inside(raw)
                kind = self._kind_of(path)
                item = self._trash.put(path, kind=kind, record_id=path.stem)
            except (TrashError, ValueError) as exc:
                failed.append({"path": raw, "error": str(exc)})
                continue
            moved.append(item.as_json())
        log.info("storage.to_trash", moved=len(moved), failed=len(failed))
        return {"moved": moved, "failed": failed, "trashBytes": self._trash.used_bytes()}

    def restore(self, payload: dict[str, Any]) -> dict[str, Any]:
        restored: list[str] = []
        failed: list[dict[str, str]] = []
        for trash_id in _ids(payload):
            try:
                restored.append(str(self._trash.restore(trash_id)))
            except TrashError as exc:
                failed.append({"id": trash_id, "error": str(exc)})
        return {"restored": restored, "failed": failed, "trashBytes": self._trash.used_bytes()}

    def purge(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Delete for good. The one irreversible call in this module.

        `all` empties the bin. It is a separate flag rather than "an empty id
        list means everything", because an empty list is what a buggy client
        sends by accident and "delete everything" must never be the accident.
        """
        if payload.get("all") is True:
            freed = self._trash.empty()
            log.info("storage.emptied", freed_mb=freed // (1024 * 1024))
            return {"purged": [], "freedBytes": freed, "trashBytes": self._trash.used_bytes()}

        freed = 0
        purged: list[str] = []
        for trash_id in _ids(payload):
            try:
                freed += self._trash.purge(trash_id)
            except TrashError as exc:
                log.info("storage.purge_failed", id=trash_id, error=str(exc))
                continue
            purged.append(trash_id)
        return {"purged": purged, "freedBytes": freed, "trashBytes": self._trash.used_bytes()}

    # ── Guards ───────────────────────────────────────────────────────────────

    def _inside(self, raw: str) -> Path:
        """Resolve a caller's path, refusing anything outside the workspace.

        The port is guarded by a token and an origin check. This guards the
        *argument*, which is a separate job: a caller holding a valid token is
        still not entitled to name `C:/Windows/System32` and have the worker
        move it somewhere.

        The bin is excluded too. Re-binning something already binned would give
        it a second manifest pointing at a path inside the first, and restoring
        that pair in the wrong order loses the file.
        """
        path = Path(raw).expanduser().resolve()
        root = self._workspace.root
        if root != path and root not in path.parents:
            raise ValueError(f"{raw} is not inside the workspace")
        if self._workspace.trash_dir in path.parents:
            raise ValueError(f"{path.name} is already in the bin")
        if not path.is_file():
            raise ValueError(f"{path.name} is not a file on this machine")
        return path

    def _kind_of(self, path: Path) -> str:
        if self._workspace.clips_dir in path.parents:
            return "clip"
        if self._workspace.sources_dir in path.parents:
            return "source"
        return "other"


# ── Reading a request ────────────────────────────────────────────────────────


def _paths(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("paths")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item).strip()][:MAX_LISTED]


def _ids(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("ids")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item).strip()][:MAX_LISTED]


def _files_under(directory: Path, *, kind: str) -> list[dict[str, Any]]:
    """Every file below one directory, with the numbers a decision needs.

    `recordId` is the file's stem, which is how every stage here names what it
    writes: `clips/<uid>/<clipId>.mp4`, `sources/<externalId>.mp4`. It is a
    convention rather than a guarantee, so it is offered for display and nothing
    is looked up by it.
    """
    if not directory.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        found.append(
            {
                "path": str(path),
                "name": path.name,
                "kind": kind,
                "recordId": path.stem,
                "sizeBytes": stat.st_size,
                "modifiedAt": stat.st_mtime,
            }
        )
        if len(found) >= MAX_LISTED:
            break
    return found
