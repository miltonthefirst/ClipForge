"""The workspace and the BlobStore port.

Phase 3, exit criterion 3 — with the cap below the current size, GC reclaims
space and the pipeline still runs. On the free tier clips never leave the machine
either, so this is the only thing standing between a long run and a full disk.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from clipforge.config import Settings
from clipforge.media.workspace import EvictionCandidate, Workspace
from clipforge.store.blobs import LocalBlobStore, UnsafeBlobKeyError, build_blob_store

T0 = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)
MB = 1024 * 1024


def write(path: Path, megabytes: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * (megabytes * MB))
    return path


def candidate(
    path: Path, *, source_id: str, age_minutes: int, **kwargs: object
) -> EvictionCandidate:
    return EvictionCandidate(
        source_id=source_id,
        path=path,
        size_bytes=path.stat().st_size,
        last_accessed_at=T0 - timedelta(minutes=age_minutes),
        **kwargs,  # type: ignore[arg-type]
    )


# ─────────────────────────────────────────────────────────────────────────────
# Layout and measurement
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_the_workspace_creates_its_layout(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "ws", max_gb=1)
    for directory in (
        workspace.sources_dir,
        workspace.clips_dir,
        workspace.transcripts_dir,
        workspace.tmp_dir,
    ):
        assert directory.is_dir()


@pytest.mark.unit
def test_usage_is_measured_by_walking_not_by_bookkeeping(tmp_path: Path) -> None:
    """ffmpeg and yt-dlp write here without reporting back, so a cached figure
    would drift — and always in the dangerous direction."""
    workspace = Workspace(tmp_path, max_gb=1)
    write(workspace.sources_dir / "a.mp4", 3)
    write(workspace.clips_dir / "b.mp4", 2)

    assert workspace.used_bytes() == 5 * MB


@pytest.mark.unit
def test_a_workspace_under_its_cap_needs_no_collection(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path, max_gb=1)
    write(workspace.sources_dir / "a.mp4", 3)
    assert workspace.over_budget_by() == 0


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 3 — collection
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_collection_evicts_least_recently_used_first(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path, max_gb=1)
    oldest = write(workspace.sources_dir / "oldest.mp4", 2)
    newest = write(workspace.sources_dir / "newest.mp4", 2)

    outcome = workspace.collect(
        [
            candidate(newest, source_id="newest", age_minutes=1),
            candidate(oldest, source_id="oldest", age_minutes=600),
        ],
        need_bytes=1 * MB,
    )

    assert outcome.evicted == ["oldest"]
    assert not oldest.exists()
    assert newest.exists(), "collection took more than it needed"


@pytest.mark.unit
def test_collection_stops_as_soon_as_the_target_is_met(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path, max_gb=1)
    files = [write(workspace.sources_dir / f"{i}.mp4", 2) for i in range(4)]

    outcome = workspace.collect(
        [candidate(f, source_id=str(i), age_minutes=600 - i) for i, f in enumerate(files)],
        need_bytes=3 * MB,
    )

    assert len(outcome.evicted) == 2, "evicted more than the target required"
    assert outcome.target_met


@pytest.mark.unit
def test_a_pinned_source_is_never_evicted(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path, max_gb=1)
    pinned = write(workspace.sources_dir / "pinned.mp4", 4)

    outcome = workspace.collect(
        [candidate(pinned, source_id="pinned", age_minutes=9999, pinned=True)],
        need_bytes=1 * MB,
    )

    assert pinned.exists()
    assert outcome.skipped_pinned == ["pinned"]
    assert not outcome.target_met


@pytest.mark.unit
def test_a_source_in_use_is_never_evicted(tmp_path: Path) -> None:
    """Deleting the file a stage is mid-read on is worse than being short of space."""
    workspace = Workspace(tmp_path, max_gb=1)
    busy = write(workspace.sources_dir / "busy.mp4", 4)

    outcome = workspace.collect(
        [candidate(busy, source_id="busy", age_minutes=9999, in_use=True)],
        need_bytes=1 * MB,
    )

    assert busy.exists()
    assert outcome.skipped_in_use == ["busy"]


@pytest.mark.unit
def test_a_shortfall_is_reported_rather_than_raised(tmp_path: Path) -> None:
    """A GC pass that cannot free enough is a fact the caller should act on, not
    an exception thrown from underneath an unrelated stage."""
    workspace = Workspace(tmp_path, max_gb=1)
    pinned = write(workspace.sources_dir / "pinned.mp4", 1)

    outcome = workspace.collect(
        [candidate(pinned, source_id="pinned", age_minutes=1, pinned=True)],
        need_bytes=500 * MB,
    )

    assert outcome.target_met is False
    assert outcome.freed_bytes == 0


@pytest.mark.unit
def test_collection_survives_a_file_that_already_vanished(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path, max_gb=1)
    ghost = workspace.sources_dir / "ghost.mp4"

    outcome = workspace.collect(
        [EvictionCandidate(source_id="ghost", path=ghost, size_bytes=999, last_accessed_at=T0)],
        need_bytes=1 * MB,
    )

    assert outcome.evicted == []


@pytest.mark.unit
def test_clearing_tmp_reclaims_scratch_from_a_previous_run(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path, max_gb=1)
    write(workspace.tmp_dir / "half-download.part", 2)

    freed = workspace.clear_tmp()

    assert freed == 2 * MB
    assert list(workspace.tmp_dir.iterdir()) == []


# ─────────────────────────────────────────────────────────────────────────────
# The BlobStore port
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_a_blob_is_written_where_its_key_says(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    produced = write(tmp_path / "produced.mp4", 1)

    ref = store.put("clips/clip-1.mp4", produced)

    assert ref.local_path == (tmp_path / "clips" / "clip-1.mp4").resolve()
    assert ref.local_path.is_file()
    assert ref.size_bytes == 1 * MB


@pytest.mark.unit
def test_a_blob_write_leaves_no_partial_file(tmp_path: Path) -> None:
    """A stage that is DONE is never re-run, so a truncated file would be
    believed. Writes stage beside the target and rename into place."""
    store = LocalBlobStore(tmp_path)
    store.put("clips/clip-1.mp4", write(tmp_path / "produced.mp4", 1))

    assert list((tmp_path / "clips").glob("*.partial")) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    ["../escape.mp4", "clips/../../escape.mp4", "/absolute.mp4", "\\absolute.mp4", ""],
)
def test_a_key_that_escapes_the_workspace_is_refused(tmp_path: Path, key: str) -> None:
    """`..` is the obvious case; the realistic one is duller — a stage builds a
    key from a video title and the title contains a slash."""
    store = LocalBlobStore(tmp_path / "ws")
    with pytest.raises(UnsafeBlobKeyError):
        store.path_for(key)


@pytest.mark.unit
def test_a_local_blob_gets_a_playback_url_when_the_server_is_running(tmp_path: Path) -> None:
    """This is playback branch 2: the PWA opened on this machine can play it."""
    store = LocalBlobStore(tmp_path, public_origin="http://127.0.0.1:8765")
    ref = store.put("clips/clip-1.mp4", write(tmp_path / "produced.mp4", 1))
    assert ref.playback_url == "http://127.0.0.1:8765/clips/clip-1.mp4"


@pytest.mark.unit
def test_a_local_blob_has_no_playback_url_when_the_server_is_off(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    ref = store.put("clips/clip-1.mp4", write(tmp_path / "produced.mp4", 1))
    assert ref.playback_url is None


@pytest.mark.unit
def test_the_local_store_is_what_the_free_tier_selects(tmp_path: Path) -> None:
    store = build_blob_store(Settings(workspace_dir=tmp_path, blob_store="local"))
    assert isinstance(store, LocalBlobStore)


@pytest.mark.unit
def test_the_firebase_store_says_exactly_what_is_missing(tmp_path: Path) -> None:
    """Deliberately absent rather than stubbed: a stub that silently did nothing
    would be worse than an error naming the one piece of work the upgrade needs."""
    settings = Settings(
        workspace_dir=tmp_path, blob_store="firebase", firebase_storage_bucket="b.appspot.com"
    )
    with pytest.raises(NotImplementedError, match="Blaze"):
        build_blob_store(settings)
