"""The UPLOAD stage: the way back for a reviewer who cannot reach the worker.

The interesting cases are all refusals. Uploading a file is not the hard part —
saying something useful when it cannot be uploaded is, because the person who
asked is holding a phone in another room and has no way to look at the worker.

In-memory stores rather than the emulator: what is under test is which writes
happen and which do not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from clipforge.config import Settings
from clipforge.models.broker import ModelBroker
from clipforge.stages.base import StageContext
from clipforge.stages.upload import UploadStage, UploadStageError
from clipforge.store.blobs import BlobRef, BlobUploadError, LocalBlobStore
from clipforge_contracts import (
    Clip,
    ClipLocation,
    Job,
    JobStatus,
    JobType,
    Lane,
    ReviewState,
    Stage,
    StageName,
    StageStatus,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class FakeClipStore:
    def __init__(self, clip: Clip | None) -> None:
        self._clip = clip
        self.saved: list[Clip] = []

    def get(self, clip_id: str) -> Clip | None:
        if self._clip is not None and self._clip.id == clip_id:
            return self._clip
        return None

    def save(self, clip: Clip) -> None:
        self.saved.append(clip)


class FakeBlobStore:
    """Records what it was asked to put, and answers with what it would return.

    Honours ``required`` the way the real stores do, which is the whole reason
    the signature is worth keeping in step. The previous fake ignored it and
    returned a local-only ``BlobRef`` for every call — so the stage's "no bucket"
    branch was reachable in tests by a route that no real store takes, and the
    branch that actually fires in production (an upload that raises) had no test
    at all.
    """

    def __init__(self, ref: BlobRef | None = None, *, fails: Exception | None = None) -> None:
        self._ref = ref
        self._fails = fails
        self.puts: list[tuple[str, Path]] = []
        self.required: list[bool] = []

    def put(
        self,
        key: str,
        source: Path,
        *,
        content_type: str | None = None,
        required: bool = False,
    ) -> BlobRef:
        self.puts.append((key, source))
        self.required.append(required)
        if self._fails is not None:
            raise self._fails
        return self._ref or BlobRef(key=key, local_path=source)


def clip(tmp_path: Path, **overrides: object) -> Clip:
    defaults = {
        "id": "clip-1",
        "uid": "user-1",
        "candidate_id": "cand-1",
        "source_id": "src-1",
        "job_id": "job-0",
        "location": ClipLocation.LOCAL,
        "local_path": str(tmp_path / "clip-1.mp4"),
        "duration_sec": 30.0,
        "width_px": 1080,
        "height_px": 1920,
        "size_bytes": 1024,
        "render_profile": "default",
        "title": "a clip",
        "review": ReviewState.PENDING,
        "created_at": NOW,
    }
    return Clip(**{**defaults, **overrides})


def context(job: Job) -> StageContext:
    settings = Settings(_env_file=None)
    return StageContext(
        job=job,
        stage_name=StageName.UPLOAD,
        checkpoint=None,
        settings=settings,
        # UPLOAD never touches the GPU; the broker is here to satisfy the contract.
        broker=ModelBroker(reserve_mb=settings.vram_reserve_mb),
    )


def job_for(clip_id: str | None) -> Job:
    return Job(
        id="job-1",
        uid="user-1",
        type=JobType.UPLOAD,
        status=JobStatus.RUNNING,
        clip_id=clip_id,
        stages=[Stage(name=StageName.UPLOAD, lane=Lane.CPU, status=StageStatus.RUNNING)],
        attempts=1,
        max_attempts=3,
        created_at=NOW,
        updated_at=NOW,
    )


def test_a_job_without_a_clip_is_refused_without_retrying(tmp_path: Path) -> None:
    stage = UploadStage(clips=FakeClipStore(None), blobs=FakeBlobStore())
    with pytest.raises(UploadStageError) as caught:
        stage.run(context(job_for(None)))
    assert caught.value.retryable is False
    assert caught.value.code == "NO_CLIP_ID"


def test_a_missing_local_file_is_not_retried(tmp_path: Path) -> None:
    """Retrying cannot make a file reappear, and burning three attempts on it
    only delays the message that says which machine was asked."""
    store = FakeClipStore(clip(tmp_path))  # the path is never created
    stage = UploadStage(clips=store, blobs=FakeBlobStore())

    with pytest.raises(UploadStageError) as caught:
        stage.run(context(job_for("clip-1")))

    assert caught.value.retryable is False
    assert caught.value.code == "LOCAL_FILE_MISSING"
    assert store.saved == [], "a clip was written for an upload that never happened"


def test_a_worker_with_no_bucket_says_so_rather_than_reporting_success(tmp_path: Path) -> None:
    """A local blob store will happily accept the file and put it back on disk.

    That is a configuration answer, not an upload — and a job that reported
    success would leave the phone with nothing and no explanation.

    Uses the real :class:`LocalBlobStore` rather than a fake. The refusal is the
    store's to make, and a fake that returns an empty ``BlobRef`` proves only
    that the fake does.
    """
    source = tmp_path / "clip-1.mp4"
    source.write_bytes(b"video")
    store = FakeClipStore(clip(tmp_path))
    stage = UploadStage(clips=store, blobs=LocalBlobStore(tmp_path / "workspace"))

    with pytest.raises(UploadStageError) as caught:
        stage.run(context(job_for("clip-1")))

    assert caught.value.code == "NO_BUCKET"
    assert caught.value.retryable is False, "no bucket will not appear on a retry"
    assert "CLIPFORGE_BLOB_STORE" in str(caught.value)
    assert store.saved == []


def test_a_refused_upload_says_what_went_wrong_and_is_retried(tmp_path: Path) -> None:
    """The failure this bug was actually made of.

    The store used to swallow every upload exception and hand back a local-only
    ``BlobRef``, which the stage could only read as "no bucket configured". So a
    403 from Cloud Storage, a timeout on a slow uplink and a genuinely
    unconfigured worker produced one message — and it named two environment
    variables that were, in the case that kept happening, already correct.
    """
    source = tmp_path / "clip-1.mp4"
    source.write_bytes(b"video")
    store = FakeClipStore(clip(tmp_path))
    blobs = FakeBlobStore(fails=BlobUploadError("uploading clips/user-1/clip-1.mp4 failed: 403"))
    stage = UploadStage(clips=store, blobs=blobs)

    with pytest.raises(UploadStageError) as caught:
        stage.run(context(job_for("clip-1")))

    assert caught.value.code == "UPLOAD_FAILED"
    assert caught.value.retryable is True, "a refused or interrupted upload may well not repeat"
    assert "403" in str(caught.value), "the real reason has to survive to the job document"
    assert "CLIPFORGE_BLOB_STORE" not in str(caught.value), "that is the other failure"
    assert store.saved == []


def test_the_upload_job_demands_a_bucket_copy(tmp_path: Path) -> None:
    """`required=True` is what separates this from a render's best-effort upload.

    Without it the store is free to degrade to a local copy and report success,
    which is exactly how an UPLOAD job came to complete while the phone still
    had nothing to play.
    """
    source = tmp_path / "clip-1.mp4"
    source.write_bytes(b"video")
    blobs = FakeBlobStore(
        BlobRef(
            key="clips/user-1/clip-1.mp4",
            local_path=source,
            storage_path="clips/user-1/clip-1.mp4",
            expires_at=NOW + timedelta(days=5),
            size_bytes=5,
        )
    )
    stage = UploadStage(clips=FakeClipStore(clip(tmp_path)), blobs=blobs)

    stage.run(context(job_for("clip-1")))

    assert blobs.required == [True]


def test_a_clip_already_in_the_bucket_is_skipped_not_reuploaded(tmp_path: Path) -> None:
    """Two reviewers pressing the same button should cost one upload."""
    source = tmp_path / "clip-1.mp4"
    source.write_bytes(b"video")
    blobs = FakeBlobStore()
    store = FakeClipStore(
        clip(
            tmp_path,
            storage_path="clips/user-1/clip-1.mp4",
            # Relative to now, not to NOW: the stage compares against the real
            # clock, and a fixed date in the past is an expired copy.
            playback_expires_at=datetime.now(UTC) + timedelta(days=5),
            location=ClipLocation.REMOTE,
        )
    )
    stage = UploadStage(clips=store, blobs=blobs)

    outcome = stage.run(context(job_for("clip-1")))

    assert outcome.skipped is True
    assert blobs.puts == [], "a clip already in the bucket was uploaded again"
    assert store.saved == []


def test_an_expired_bucket_copy_is_uploaded_again(tmp_path: Path) -> None:
    """The lifecycle rule deletes the object silently, so an expiry in the past
    means the phone has nothing to play even though the clip says REMOTE."""
    source = tmp_path / "clip-1.mp4"
    source.write_bytes(b"video")
    blobs = FakeBlobStore(
        BlobRef(
            key="clips/user-1/clip-1.mp4",
            local_path=source,
            storage_path="clips/user-1/clip-1.mp4",
            expires_at=NOW + timedelta(days=5),
            size_bytes=5,
        )
    )
    store = FakeClipStore(
        clip(
            tmp_path,
            storage_path="clips/user-1/clip-1.mp4",
            playback_expires_at=datetime.now(UTC) - timedelta(days=1),
            location=ClipLocation.REMOTE,
        )
    )
    stage = UploadStage(clips=store, blobs=blobs)

    outcome = stage.run(context(job_for("clip-1")))

    assert outcome.skipped is False
    assert len(blobs.puts) == 1


def test_a_successful_upload_marks_the_clip_reachable(tmp_path: Path) -> None:
    source = tmp_path / "clip-1.mp4"
    source.write_bytes(b"video")
    expires = NOW + timedelta(days=5)
    blobs = FakeBlobStore(
        BlobRef(
            key="clips/user-1/clip-1.mp4",
            local_path=source,
            storage_path="clips/user-1/clip-1.mp4",
            expires_at=expires,
            size_bytes=5,
        )
    )
    store = FakeClipStore(clip(tmp_path))
    stage = UploadStage(clips=store, blobs=blobs)

    outcome = stage.run(context(job_for("clip-1")))

    assert outcome.skipped is False
    assert blobs.puts == [("clips/user-1/clip-1.mp4", source)]
    (written,) = store.saved
    assert written.location is ClipLocation.REMOTE
    assert written.storage_path == "clips/user-1/clip-1.mp4"
    assert written.playback_expires_at == expires
    # The local copy is what publishing uses and what survives the bucket's
    # lifecycle rule, so uploading must never move or forget it.
    assert written.local_path == str(tmp_path / "clip-1.mp4")
