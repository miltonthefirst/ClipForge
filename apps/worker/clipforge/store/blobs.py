"""The `BlobStore` port: where rendered artefacts go.

ClipForge runs on the Firebase Spark free tier, where Cloud Storage is
unavailable outright, so artefacts stay on the worker. That is a *deployment*
fact, not an architectural one, and this port is what keeps it that way: every
write goes through one interface, and enabling Blaze later means adding one
adapter rather than editing every call site.

See docs/adr/0009-spark-tier-local-artefacts.md.

Two rules keep the local adapter honest:

**Every path is confined to the workspace root.** A stage passes a relative key;
the adapter resolves it and refuses anything that escapes. Stage code eventually
handles LLM output and video titles, so "the key is always well-formed" is not an
assumption worth making.

**Writes are atomic.** Artefacts are written to a temporary file and moved into
place, so a crash mid-write cannot leave a half-rendered MP4 that looks finished.
That matters more here than usual: a stage that is `DONE` is never re-run, so a
truncated file would be believed.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from clipforge.config import Settings

__all__ = ["BlobRef", "BlobStore", "LocalBlobStore", "UnsafeBlobKeyError", "build_blob_store"]


class UnsafeBlobKeyError(ValueError):
    """A key resolved outside the workspace root."""


@dataclass(frozen=True)
class BlobRef:
    """Where an artefact ended up, in both possible senses.

    Both fields exist on every adapter's result because `Clip` carries both:
    `localPath` is always set — the worker still needs the file to publish
    (decision D7) — and `playbackUrl` is populated only when a copy exists that a
    browser can fetch from anywhere.
    """

    key: str
    local_path: Path
    playback_url: str | None = None
    size_bytes: int = 0


class BlobStore(Protocol):
    """Somewhere to put a finished artefact."""

    def put(self, key: str, source: Path, *, content_type: str | None = None) -> BlobRef:
        """Move or copy a produced file into the store under ``key``."""
        ...

    def path_for(self, key: str) -> Path:
        """The local path a key maps to, without requiring the file to exist."""
        ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...


class LocalBlobStore:
    """Artefacts stay on the worker, under the workspace root.

    The free-tier default, and the only adapter that works without Blaze.
    """

    def __init__(self, root: Path, *, public_origin: str | None = None) -> None:
        self._root = root.expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        # The worker's read-only local file server, when it is running. Turns a
        # key into something a browser on this machine can actually play.
        self._public_origin = public_origin.rstrip("/") if public_origin else None

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, key: str) -> Path:
        """Resolve a key, refusing anything that escapes the workspace.

        `..` in a key is the obvious attack, but the realistic case is duller: a
        stage builds a key from a video title, and a title contains a slash.
        """
        if not key or key.startswith(("/", "\\")) or Path(key).is_absolute():
            raise UnsafeBlobKeyError(f"blob key must be relative and non-empty: {key!r}")

        candidate = (self._root / key).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            raise UnsafeBlobKeyError(f"blob key escapes the workspace root: {key!r}")
        return candidate

    def put(self, key: str, source: Path, *, content_type: str | None = None) -> BlobRef:
        del content_type  # Meaningful only to a remote store.
        target = self.path_for(key)
        target.parent.mkdir(parents=True, exist_ok=True)

        # Write beside the target and rename, so a crash cannot leave a
        # half-copied file that a DONE stage would never revisit.
        staging = target.with_suffix(target.suffix + ".partial")
        shutil.copy2(source, staging)
        staging.replace(target)

        return BlobRef(
            key=key,
            local_path=target,
            playback_url=f"{self._public_origin}/{key}" if self._public_origin else None,
            size_bytes=target.stat().st_size,
        )

    def exists(self, key: str) -> bool:
        return self.path_for(key).is_file()

    def delete(self, key: str) -> None:
        self.path_for(key).unlink(missing_ok=True)


def build_blob_store(settings: Settings) -> BlobStore:
    """Select the adapter named by ``CLIPFORGE_BLOB_STORE``.

    The `firebase` adapter is deliberately absent rather than stubbed: it is the
    one piece of code the Blaze upgrade needs, and a stub that silently did
    nothing would be worse than an error that says so.
    """
    if settings.blob_store == "local":
        origin = settings.local_server_origin if settings.local_server_enabled else None
        return LocalBlobStore(settings.workspace_dir, public_origin=origin)

    raise NotImplementedError(
        "CLIPFORGE_BLOB_STORE=firebase is not implemented. Cloud Storage for Firebase "
        "requires the Blaze plan, which this project does not have — see "
        "docs/adr/0009-spark-tier-local-artefacts.md. When Blaze is enabled, add a "
        "FirebaseBlobStore here; nothing else needs to change."
    )
