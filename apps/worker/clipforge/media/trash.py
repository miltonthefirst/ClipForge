"""A bin for media the operator has finished with, that nothing empties.

## Why a bin rather than a delete

Because the two questions are different and get different answers. *"Is this
clip worth keeping in my review queue?"* is answered dozens of times a day and
is cheap to get wrong. *"Do I still want the 400 MB of video behind it?"* is
answered rarely and is expensive to get wrong — the source is a download that
may no longer be available, and the clip is a render that cost GPU time.

So deleting the Firestore record and removing the file are separate acts on
purpose, and only the second one goes through here. Removing a record leaves
the file exactly where it was; removing the file puts it in this bin, where it
stays until somebody says otherwise.

## Why it is inside the workspace and outside the budget

Inside, because a bin on another volume turns every move into a copy, and a
400 MB copy is a different thing from a rename.

Outside the budget, and that needs saying because the opposite is the obvious
choice. `Workspace.used_bytes` excludes this directory, so the garbage collector
never counts trashed bytes when deciding whether it is over its cap. Counting
them would be more *truthful* about the disk and catastrophic in practice: the
collector evicts **sources**, so a full bin would make it delete live downloads
to make room for deleted ones. Live data must never be evicted to house dead
data.

The cost of that choice is that the bin can fill a disk while the workspace
reports itself comfortable, so the size is reported wherever the bin is shown
and the operator is the one who decides when it goes.

## The layout, and why each item gets a directory

    workspace/trash/<trash id>/item.json      what it was and where it came from
    workspace/trash/<trash id>/<file name>    the file itself, name intact

A directory per item because two clips from different users can have the same
file name, and because a sidecar beside the file cannot drift away from it. The
manifest is what makes a restore possible: without the original path, a bin is
a place files go to be forgotten with extra steps.

No index, no database. The directory listing IS the index, which means a bin
survives the worker being killed mid-move, and a file dragged out of it by hand
is simply gone from the listing rather than a dangling row.
"""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = ["Trash", "TrashError", "TrashedItem"]

_MANIFEST = "item.json"


class TrashError(RuntimeError):
    """The move could not be made. Always says which file and why."""


@dataclass(frozen=True)
class TrashedItem:
    """One thing in the bin, and everything needed to put it back."""

    id: str
    kind: str  # "clip" | "source" | "other"
    name: str
    original_path: str
    size_bytes: int
    trashed_at: datetime
    # The Firestore id this file belonged to, when it had one. Kept so a listing
    # can say "the clip you rejected on Tuesday" rather than a file name, and
    # deliberately not required: a file whose record was deleted first is
    # exactly the case this bin exists to handle.
    record_id: str | None = None

    def as_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "originalPath": self.original_path,
            "sizeBytes": self.size_bytes,
            "trashedAt": self.trashed_at.isoformat(),
            "recordId": self.record_id,
        }


class Trash:
    """Move files out of the way without destroying them."""

    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    # ── Putting things in ────────────────────────────────────────────────────

    def put(self, path: Path, *, kind: str = "other", record_id: str | None = None) -> TrashedItem:
        """Move one file into the bin, keeping its name and where it came from.

        The manifest is written **after** the move rather than before. A crash
        between the two leaves a directory holding a file and no manifest, which
        `items` skips and a human can still recognise by name; the other order
        would leave a manifest describing a file that is still in place, and a
        restore would then overwrite the live one with nothing.
        """
        source = path.expanduser().resolve()
        if not source.is_file():
            raise TrashError(f"{source.name} is not a file on this machine")

        trash_id = uuid.uuid4().hex
        holder = self._root / trash_id
        holder.mkdir(parents=True, exist_ok=False)
        destination = holder / source.name

        try:
            size = source.stat().st_size
            # `shutil.move` rather than `Path.rename`: the workspace and the bin
            # are normally the same volume and this is a rename, but a workspace
            # mounted across volumes would make rename fail with EXDEV and this
            # falls back to a copy.
            shutil.move(str(source), str(destination))
        except OSError as exc:
            holder.rmdir()
            raise TrashError(f"could not move {source.name} to the bin: {exc}") from exc

        item = TrashedItem(
            id=trash_id,
            kind=kind,
            name=source.name,
            original_path=str(source),
            size_bytes=size,
            trashed_at=datetime.now(UTC),
            record_id=record_id,
        )
        (holder / _MANIFEST).write_text(json.dumps(item.as_json(), indent=2), encoding="utf-8")
        log.info("trash.put", name=item.name, kind=kind, mb=size // (1024 * 1024))
        return item

    # ── Looking at what is in there ──────────────────────────────────────────

    def items(self) -> list[TrashedItem]:
        """Everything in the bin, newest first.

        Skips a directory whose manifest is missing or unreadable rather than
        raising. One unreadable item must not make the whole bin unopenable,
        which is precisely when somebody needs to open it.
        """
        found: list[TrashedItem] = []
        for holder in self._root.iterdir():
            if not holder.is_dir():
                continue
            item = self._read(holder)
            if item is not None:
                found.append(item)
        return sorted(found, key=lambda item: item.trashed_at, reverse=True)

    def used_bytes(self) -> int:
        total = 0
        for path in self._root.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    # ── Taking things out ────────────────────────────────────────────────────

    def restore(self, trash_id: str) -> Path:
        """Put one item back where it came from.

        Refuses rather than overwrites when something is already at the original
        path. That is not a theoretical case: re-running the job that made a clip
        writes the same path, so the file in the bin and the file on disk are two
        different renders and only the operator knows which they want.
        """
        holder = self._holder(trash_id)
        item = self._read(holder)
        if item is None:
            raise TrashError(f"nothing in the bin with id {trash_id}")

        destination = Path(item.original_path)
        if destination.exists():
            raise TrashError(
                f"{destination.name} is already back at {destination.parent} — "
                "the job that made it has run again. Move or rename that one first."
            )

        stored = holder / item.name
        if not stored.is_file():
            raise TrashError(f"the file for {item.name} is no longer in the bin")

        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(stored), str(destination))
        except OSError as exc:
            raise TrashError(f"could not restore {item.name}: {exc}") from exc

        shutil.rmtree(holder, ignore_errors=True)
        log.info("trash.restored", name=item.name, to=str(destination))
        return destination

    def purge(self, trash_id: str) -> int:
        """Delete one item for good. Returns the bytes reclaimed."""
        holder = self._holder(trash_id)
        item = self._read(holder)
        size = item.size_bytes if item is not None else 0
        shutil.rmtree(holder, ignore_errors=True)
        log.info("trash.purged", id=trash_id, mb=size // (1024 * 1024))
        return size

    def empty(self) -> int:
        """Delete everything. Returns the bytes reclaimed."""
        freed = 0
        for item in self.items():
            freed += self.purge(item.id)
        return freed

    # ── Internals ────────────────────────────────────────────────────────────

    def _holder(self, trash_id: str) -> Path:
        """The directory for one id, checked to be inside the bin.

        `trash_id` arrives over HTTP. Without this, `../../..` in a path segment
        reaches anywhere the worker can write, and `purge` is a recursive delete.
        """
        holder = (self._root / trash_id).resolve()
        if holder.parent != self._root or not holder.is_dir():
            raise TrashError(f"nothing in the bin with id {trash_id}")
        return holder

    def _read(self, holder: Path) -> TrashedItem | None:
        try:
            raw = json.loads((holder / _MANIFEST).read_text(encoding="utf-8"))
            return TrashedItem(
                id=str(raw["id"]),
                kind=str(raw.get("kind") or "other"),
                name=str(raw["name"]),
                original_path=str(raw["originalPath"]),
                size_bytes=int(raw.get("sizeBytes") or 0),
                trashed_at=datetime.fromisoformat(str(raw["trashedAt"])),
                record_id=(str(raw["recordId"]) if raw.get("recordId") else None),
            )
        except (OSError, ValueError, KeyError) as exc:
            log.info("trash.unreadable", holder=holder.name, error=str(exc))
            return None
