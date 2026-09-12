"""The blob store's two promises, which pull in opposite directions.

A render's bucket copy is *best effort*: the artefact is the file on disk, the
upload is a convenience, and an upload that fails must not cost ten minutes of
ANALYZE. An UPLOAD job's bucket copy is *the deliverable*: someone on a phone
asked for this clip because they cannot reach this machine, and a "success" that
leaves them with nothing is worse than a failure that says why.

Both used to run through the same swallow-everything `except`, so the second
promise was not kept — and the way it broke was silent, because the caller could
only tell an upload failure from an unconfigured bucket by looking at an empty
field that means both.

A fake Cloud Storage client rather than the emulator: what is under test is
which exceptions escape, and a client that can be told to fail is the only way
to ask.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from clipforge.store.blobs import (
    BlobUploadError,
    FirebaseBlobStore,
    LocalBlobStore,
)

# Every test in this file is the unit tier. Without this marker CI's
# `pytest -m unit` silently deselects the whole file — the tests pass locally,
# run nowhere, and protect nothing.
pytestmark = pytest.mark.unit


class FakeBlob:
    def __init__(self, key: str, fails: Exception | None) -> None:
        self.key = key
        self._fails = fails
        self.uploaded: list[tuple[str, str | None, float | None]] = []

    def upload_from_filename(
        self, filename: str, content_type: str | None = None, timeout: float | None = None
    ) -> None:
        if self._fails is not None:
            raise self._fails
        self.uploaded.append((filename, content_type, timeout))


class FakeBucket:
    def __init__(self, fails: Exception | None) -> None:
        self._fails = fails
        self.blobs: dict[str, FakeBlob] = {}

    def blob(self, key: str) -> FakeBlob:
        return self.blobs.setdefault(key, FakeBlob(key, self._fails))


class FakeClient:
    """Stands in for `google.cloud.storage.Client`."""

    def __init__(self, fails: Exception | None = None) -> None:
        self._bucket = FakeBucket(fails)

    def bucket(self, name: str) -> FakeBucket:
        del name
        return self._bucket


def store(tmp_path: Path, *, fails: Exception | None = None) -> FirebaseBlobStore:
    return FirebaseBlobStore(
        LocalBlobStore(tmp_path / "workspace"),
        bucket="a-bucket",
        retention_days=5,
        client=FakeClient(fails),
    )


def source_file(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"video")
    return path


def test_a_failed_upload_does_not_cost_the_render(tmp_path: Path) -> None:
    """The default, and the reason the swallow exists at all.

    A 32 MB clip that times out on a slow uplink once failed RENDER
    non-retryably and threw away a job that had already spent ten minutes in
    ANALYZE. The clip is still on the machine, still publishable, still
    reviewable there — so the bucket copy is the part allowed to be missing.
    """
    blobs = store(tmp_path, fails=TimeoutError("the write operation timed out"))

    ref = blobs.put("clips/u/clip.mp4", source_file(tmp_path))

    assert ref.local_path.is_file(), "the local copy is the artefact and must survive"
    assert ref.storage_path is None, "there is no bucket copy, and it must not claim one"
    assert ref.expires_at is None


def test_a_failed_upload_is_raised_when_the_caller_required_one(tmp_path: Path) -> None:
    """The promise the UPLOAD job depends on.

    Note what is asserted about the message: the underlying failure has to
    survive into it. Reporting "no bucket configured" for a timeout is what sent
    an operator to check two environment variables that were already right.
    """
    blobs = store(tmp_path, fails=TimeoutError("the write operation timed out"))

    with pytest.raises(BlobUploadError) as caught:
        blobs.put("clips/u/clip.mp4", source_file(tmp_path), required=True)

    assert caught.value.cause_code == "UPLOAD_FAILED"
    assert "timed out" in str(caught.value)
    assert "a-bucket" in str(caught.value), "which bucket was refused is half the diagnosis"


def test_a_required_upload_still_leaves_the_local_copy(tmp_path: Path) -> None:
    """Raising must not undo the half that worked.

    The local copy is written first and unconditionally, and the clip stays
    publishable from this machine whatever the bucket did.
    """
    blobs = store(tmp_path, fails=RuntimeError("403 Forbidden"))

    with pytest.raises(BlobUploadError):
        blobs.put("clips/u/clip.mp4", source_file(tmp_path), required=True)

    assert (tmp_path / "workspace" / "clips" / "u" / "clip.mp4").is_file()


def test_a_local_store_refuses_a_required_upload_rather_than_pretending(tmp_path: Path) -> None:
    """There is no bucket here and there never will be.

    Distinguished from a failed upload by `cause_code`, because the two need
    opposite responses: this one cannot be retried into working, and it names
    the settings that would fix it.
    """
    blobs = LocalBlobStore(tmp_path / "workspace")

    with pytest.raises(BlobUploadError) as caught:
        blobs.put("clips/u/clip.mp4", source_file(tmp_path), required=True)

    assert caught.value.cause_code == "NO_BUCKET"
    assert "CLIPFORGE_FIREBASE_STORAGE_BUCKET" in str(caught.value)


def test_a_local_store_still_accepts_an_ordinary_put(tmp_path: Path) -> None:
    """`required` is opt-in; the free-tier path must be untouched by it."""
    blobs = LocalBlobStore(tmp_path / "workspace")

    ref = blobs.put("clips/u/clip.mp4", source_file(tmp_path))

    assert ref.local_path.is_file()
    assert ref.storage_path is None


def test_a_successful_upload_reports_where_it_went_and_when_it_expires(tmp_path: Path) -> None:
    blobs = store(tmp_path)

    ref = blobs.put("clips/u/clip.mp4", source_file(tmp_path), content_type="video/mp4")

    assert ref.storage_path == "clips/u/clip.mp4"
    assert ref.expires_at is not None
    # Null on purpose: the PWA resolves the object through the Storage SDK so
    # storage.rules run at fetch time. A URL in a document would be a bearer
    # token in a database row.
    assert ref.playback_url is None
