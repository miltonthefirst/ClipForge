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
from clipforge.store.blobs import BlobRef
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
    """Records what it was asked to put, and answers with what it would return."""

    def __init__(self, ref: BlobRef | None = None) -> None:
        self._ref = ref
        self.puts: list[tuple[str, Path]] = []

    def put(self, key: str, source: Path, *, content_type: str | None = None) -> BlobRef:
        self.puts.append((key, source))
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
    """
    source = tmp_path / "clip-1.mp4"
    source.write_bytes(b"video")
    store = FakeClipStore(clip(tmp_path))
    # No playback_url and no storage_path: exactly what LocalBlobStore returns.
    stage = UploadStage(clips=store, blobs=FakeBlobStore())

    with pytest.raises(UploadStageError) as caught:
        stage.run(context(job_for("clip-1")))

    assert caught.value.code == "NO_BUCKET"
    assert "CLIPFORGE_BLOB_STORE" in str(caught.value)
    assert store.saved == []


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
