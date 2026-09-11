"""Put one clip in the bucket, because a phone asked for it.

Clips live on the worker's disk (ADR-0009). A phone reviewing the queue can
reach Firestore and the bucket, and cannot reach the worker at all — its file
server answers on 127.0.0.1, which on a phone is the phone. So a reviewer who
finds a clip with nothing to play has no way to fix it from where they are
standing, and the clip they most want to watch is the one they cannot.

This is the way back: the phone writes an UPLOAD job, the worker claims it like
any other, copies the file into the bucket and stamps the clip with a URL. The
phone is already watching that document, so the player fills in on its own.

A job rather than a control-API call on purpose. The control API is loopback —
the desktop can use it and a phone can never — and the whole point here is the
device that is not at the desk. The queue is the one channel that reaches the
worker from anywhere, and it already handles leases, retries and reporting.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from clipforge_contracts import ClipLocation, Lane, StageName

from clipforge.observability import get_logger
from clipforge.stages.base import StageContext, StageOutcome
from clipforge.store.blobs import BlobStore
from clipforge.store.firestore import ClipStore

log = get_logger(__name__)

__all__ = ["UploadStage", "UploadStageError"]


class UploadStageError(RuntimeError):
    """The clip could not be uploaded.

    ``retryable`` and ``code`` are read by the StageRunner, which decides
    whether the job burns another attempt. A missing file is not retryable —
    waiting will not make it reappear — while a refused bucket usually is.
    """

    def __init__(
        self, message: str, *, retryable: bool = True, code: str = "UPLOAD_FAILED"
    ) -> None:
        self.retryable = retryable
        self.code = code
        super().__init__(message)


class UploadStage:
    """Copy one already-rendered clip into the bucket, and say where it went."""

    name = StageName.UPLOAD
    # Network-bound, like PUBLISH. A 20 MB upload holding the GPU lane would
    # stall transcription for minutes over something that never touches CUDA.
    lane = Lane.CPU

    def __init__(self, *, clips: ClipStore, blobs: BlobStore) -> None:
        self._clips = clips
        self._blobs = blobs

    def run(self, context: StageContext) -> StageOutcome:
        clip_id = context.job.clip_id
        if not clip_id:
            raise UploadStageError(
                "an UPLOAD job must name a clip", retryable=False, code="NO_CLIP_ID"
            )

        clip = self._clips.get(clip_id)
        if clip is None:
            raise UploadStageError(
                f"clip {clip_id} does not exist", retryable=False, code="CLIP_NOT_FOUND"
            )

        # Already reachable, and not about to expire. Skipped rather than done:
        # "this was already there" and "we uploaded it" are different answers,
        # and the job document should be able to tell them apart.
        #
        # Reachability is `storage_path`, not `playback_url`. FirebaseBlobStore
        # deliberately leaves the URL null so the PWA resolves the object
        # through the Storage SDK and `storage.rules` run at fetch time; a URL
        # in a document would be a bearer token in a database row.
        if clip.storage_path and not self._expired(clip.playback_expires_at):
            log.info("upload.already_remote", clip_id=clip_id)
            return StageOutcome(
                skipped=True,
                detail="already in the bucket",
                metadata={"storagePath": clip.storage_path},
            )

        if not clip.local_path:
            raise UploadStageError(
                f"clip {clip_id} has no file on this worker to upload",
                retryable=False,
                code="NO_LOCAL_COPY",
            )

        source = Path(clip.local_path)
        if not source.is_file():
            # The commonest real case: a clip rendered on a different machine,
            # or a workspace that has since been cleared. Retrying cannot help,
            # and the message has to say which machine was asked.
            raise UploadStageError(
                f"{source} is not on this worker — it was rendered somewhere else, "
                "or the workspace has been cleared since",
                retryable=False,
                code="LOCAL_FILE_MISSING",
            )

        ref = self._blobs.put(f"clips/{clip.uid}/{clip_id}.mp4", source, content_type="video/mp4")

        if not (ref.playback_url or ref.storage_path):
            # A local blob store accepted the file and put it back on disk. That
            # is a configuration answer, not an upload, and saying so beats a
            # job that reports success while the phone still has nothing.
            raise UploadStageError(
                "this worker has no bucket configured, so the clip cannot be made "
                "reachable from a phone. Set CLIPFORGE_BLOB_STORE=firebase and "
                "CLIPFORGE_FIREBASE_STORAGE_BUCKET, then restart the worker.",
                retryable=False,
                code="NO_BUCKET",
            )

        self._clips.save(
            clip.model_copy(
                update={
                    "location": ClipLocation.REMOTE,
                    "playback_url": ref.playback_url,
                    "storage_path": ref.storage_path,
                    "playback_expires_at": ref.expires_at,
                    "size_bytes": ref.size_bytes or clip.size_bytes,
                }
            )
        )
        log.info("upload.done", clip_id=clip_id, size_bytes=ref.size_bytes)

        return StageOutcome(
            detail="uploaded for review",
            metadata={"storagePath": ref.storage_path, "sizeBytes": ref.size_bytes},
        )

    @staticmethod
    def _expired(expires_at: datetime | None) -> bool:
        """No expiry means it does not expire; a past one means re-upload."""
        if expires_at is None:
            return False
        return expires_at <= datetime.now(UTC)
