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

import contextlib
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover - types only; the client is imported lazily
    # `google.cloud` is a namespace package, which mypy resolves as a module
    # without seeing its submodules as attributes. The import is correct and
    # works at runtime; only the checker needs telling.
    from google.cloud import storage  # type: ignore[attr-defined]

from clipforge.config import Settings
from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = [
    "BlobRef",
    "BlobStore",
    "FirebaseBlobStore",
    "LocalBlobStore",
    "UnsafeBlobKeyError",
    "build_blob_store",
]


class UnsafeBlobKeyError(ValueError):
    """A key resolved outside the workspace root."""


@dataclass(frozen=True)
class BlobRef:
    """Where an artefact ended up, in every sense that matters downstream.

    `local_path` is always set — the worker still needs the file to publish
    (decision D7), and it is what survives when the bucket copy expires. The
    rest describe a copy somewhere a phone can reach, and are all null when
    there is not one.

    `playback_url` means what it says: a URL *any* browser can fetch. It is
    deliberately NOT the worker's own file server. That server answers on
    127.0.0.1, which on a phone is the phone — a URL that resolves to nothing
    and fails silently. The PWA composes that address itself when it detects the
    server is reachable (`playback.ts`, branch 2); putting it here made every
    clip look remote and pre-empted the fallback that would have shown a poster.
    """

    key: str
    local_path: Path
    playback_url: str | None = None
    storage_path: str | None = None
    expires_at: datetime | None = None
    size_bytes: int = 0


class BlobStore(Protocol):
    """Somewhere to put a finished artefact."""

    def put(self, key: str, source: Path, *, content_type: str | None = None) -> BlobRef:
        """Move or copy a produced file into the store under ``key``."""
        ...

    def forget(self, key: str) -> None:
        """Drop any copy that is not the local one. A no-op where there is none."""
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

    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

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

        return BlobRef(key=key, local_path=target, size_bytes=target.stat().st_size)

    def exists(self, key: str) -> bool:
        return self.path_for(key).is_file()

    def delete(self, key: str) -> None:
        self.path_for(key).unlink(missing_ok=True)

    def forget(self, key: str) -> None:
        """No remote copy exists, so there is nothing to forget."""
        del key


class FirebaseBlobStore:
    """A local copy *and* a bucket copy, because both are load-bearing.

    Composes the local store rather than replacing it. The local file is what
    the worker publishes from (decision D7) and what remains once the bucket
    copy is gone; the bucket copy exists for one purpose, which is letting a
    phone play a clip it is being asked to review.

    ## The bucket copy is deliberately short-lived

    Source video is never uploaded — a 60-minute 1080p source is 1-3 GB, and
    uploading it would invert the local-first thesis and cost real money
    (docs/PLAN.md D3). Clips are 5-20 MB, and even those are not kept: a
    lifecycle rule on the bucket deletes them after
    ``CLIPFORGE_CLIP_RETENTION_DAYS``, and a clip that has been reviewed is
    dropped sooner than that because its job is done.

    Nothing here performs that expiry. Deleting on a schedule from the worker
    would mean retention silently stopping whenever this machine is off, which
    is exactly when an unattended bucket grows. The bucket does it itself; this
    only records *when* it will happen, so the UI never has to ask.

    ## Why no URL is stored

    ``playback_url`` stays null even though a copy exists. The PWA resolves the
    object through the Storage SDK at play time, which runs `storage.rules` on
    the request — so access is decided when the video is fetched rather than
    frozen into a document. A download URL written into Firestore would be a
    bearer token in a database row, valid for anyone who ever saw it, and would
    make those rules decorative.
    """

    def __init__(
        self,
        local: LocalBlobStore,
        *,
        bucket: str,
        retention_days: int,
        timeout_s: float = 600.0,
        client: storage.Client | None = None,
    ) -> None:
        self._local = local
        self._bucket_name = bucket
        self._retention_days = retention_days
        self._timeout_s = timeout_s
        self._client = client
        self._bucket_handle: storage.Bucket | None = None

    @property
    def root(self) -> Path:
        return self._local.root

    def _bucket(self) -> storage.Bucket:
        """Resolved lazily so constructing the store needs no network."""
        if self._bucket_handle is None:
            if self._client is None:
                # Imported here rather than at module scope: the local adapter
                # must keep working on a machine with no Cloud Storage client.
                from google.cloud import storage as gcs  # type: ignore[attr-defined]

                self._client = gcs.Client()
            self._bucket_handle = self._client.bucket(self._bucket_name)
        return self._bucket_handle

    def path_for(self, key: str) -> Path:
        return self._local.path_for(key)

    def put(self, key: str, source: Path, *, content_type: str | None = None) -> BlobRef:
        # Local first, and unconditionally.
        ref = self._local.put(key, source, content_type=content_type)

        # An upload that fails must not cost the render — and saying so in a
        # comment is not the same as implementing it. The first version of this
        # let the exception propagate, and a 32 MB clip that timed out on a slow
        # uplink failed the RENDER stage non-retryably and threw away a job that
        # had already spent ten minutes in ANALYZE. The bucket copy is the part
        # of this that is allowed to be missing; the clip is still on the
        # machine, still publishable, and still reviewable there.
        try:
            blob = self._bucket().blob(key)
            # Resumable, because these are tens of megabytes over whatever
            # uplink the machine happens to have. `timeout` is per request, and
            # the default of 60s is a bet on a fast connection.
            blob.upload_from_filename(
                str(ref.local_path),
                content_type=content_type,
                timeout=self._timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 - any upload failure degrades the same way
            log.warning(
                "blob.upload_failed",
                key=key,
                size_mb=round(ref.size_bytes / 1_048_576, 2),
                error=str(exc),
                consequence="clip stays local-only; review it on this machine",
            )
            return ref

        log.info("blob.uploaded", key=key, size_mb=round(ref.size_bytes / 1_048_576, 2))
        return BlobRef(
            key=key,
            local_path=ref.local_path,
            playback_url=None,
            storage_path=key,
            expires_at=datetime.now(UTC) + timedelta(days=self._retention_days),
            size_bytes=ref.size_bytes,
        )

    def exists(self, key: str) -> bool:
        return self._local.exists(key)

    def delete(self, key: str) -> None:
        self.forget(key)
        self._local.delete(key)

    def forget(self, key: str) -> None:
        """Remove the bucket copy, keeping the local one.

        Tolerates an object that is already gone, because it usually is: the
        lifecycle rule collects these on its own schedule, so by the time
        anything asks, the answer is often "already handled".
        """
        blob = self._bucket().blob(key)
        with contextlib.suppress(Exception):
            blob.delete()


def build_blob_store(settings: Settings) -> BlobStore:
    """Select the adapter named by ``CLIPFORGE_BLOB_STORE``."""
    local = LocalBlobStore(settings.workspace_dir)
    if settings.blob_store == "local":
        return local

    return FirebaseBlobStore(
        local,
        bucket=settings.firebase_storage_bucket,
        retention_days=settings.clip_retention_days,
        timeout_s=settings.upload_timeout_seconds,
    )
