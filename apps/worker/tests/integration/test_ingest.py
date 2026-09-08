"""Ingestion, end to end against the emulator.

Phase 3, exit criteria 1, 2, 3 and 5. Everything here runs through
`LocalFileAdapter` against a committed fixture, so the whole tier needs **no
network at all** — which is exit criterion 5, and the reason the local adapter
exists in the first place.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from clipforge.config import Settings
from clipforge.media.sources import LocalFileAdapter
from clipforge.media.workspace import Workspace
from clipforge.models.broker import ModelBroker
from clipforge.scheduler.worker import Worker
from clipforge.stages.base import StageRegistry
from clipforge.stages.download import DownloadStage
from clipforge.stages.pipeline import new_clip_job
from clipforge.store.firestore import JobStore, SourceStore, WorkerStore
from clipforge_contracts import IngestErrorCode, JobStatus, SourceProvider, StageStatus
from google.cloud import firestore

FIXTURES = Path(__file__).resolve().parents[2] / "assets" / "fixtures"
SAMPLE_AV = FIXTURES / "sample-av.mp4"
VIDEO_ONLY = FIXTURES / "video-only.mp4"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not SAMPLE_AV.is_file(), reason="media fixtures are missing"),
]


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path / "ws", max_gb=1)


@pytest.fixture
def sources(client: firestore.Client, settings: Settings) -> SourceStore:
    return SourceStore(client, settings)


def local_only_factory(_submission: str, **_kwargs: object) -> LocalFileAdapter:
    """Force the local adapter, so no test can reach YouTube even by accident."""
    return LocalFileAdapter()


@pytest.fixture
def ingest_worker(
    jobs: JobStore,
    workers: WorkerStore,
    sources: SourceStore,
    workspace: Workspace,
    settings: Settings,
) -> Worker:
    registry = StageRegistry()
    registry.register(
        DownloadStage(sources=sources, workspace=workspace, adapter_factory=local_only_factory)
    )
    probe: object = lambda: None  # noqa: E731 - no GPU in this tier
    return Worker(
        settings=settings,
        jobs=jobs,
        workers=workers,
        broker=ModelBroker(reserve_mb=700, probe=probe),  # type: ignore[arg-type]
        registry_factory=lambda _t: registry,
    )


def submit(
    jobs: JobStore,
    workspace: Workspace,
    sources: SourceStore,
    settings: Settings,
    submission: str,
    job_id: str,
) -> None:
    stages = [DownloadStage(sources=sources, workspace=workspace)]
    jobs.create(new_clip_job(uid="user-1", submission=submission, stages=stages, job_id=job_id))


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 1 — a submission becomes a source on disk and in Firestore
# ─────────────────────────────────────────────────────────────────────────────


def test_a_submission_produces_a_source_document(
    jobs: JobStore,
    sources: SourceStore,
    workspace: Workspace,
    settings: Settings,
    ingest_worker: Worker,
) -> None:
    """Phase 3, exit criterion 1."""
    submit(jobs, workspace, sources, settings, str(SAMPLE_AV), "ingest-1")

    finished = ingest_worker.run_once()

    assert finished is not None, "the job was not claimed"
    assert finished.status is JobStatus.COMPLETED, finished.error
    assert finished.stages[0].status is StageStatus.DONE

    source_id = (finished.stages[0].checkpoint or {})["sourceId"]
    source = sources.get(source_id)
    assert source is not None
    assert source.provider is SourceProvider.LOCAL
    assert source.title == SAMPLE_AV.stem
    assert source.duration_sec is not None and 4.5 < source.duration_sec < 5.5
    assert source.content_hash
    assert source.local_path and Path(source.local_path).is_file()
    assert source.size_bytes == SAMPLE_AV.stat().st_size


def test_the_source_records_when_it_was_last_touched(
    jobs: JobStore,
    sources: SourceStore,
    workspace: Workspace,
    settings: Settings,
    ingest_worker: Worker,
) -> None:
    """Least-recently-used eviction is worthless without this."""
    submit(jobs, workspace, sources, settings, str(SAMPLE_AV), "ingest-1")
    finished = ingest_worker.run_once()
    assert finished is not None

    source = sources.get((finished.stages[0].checkpoint or {})["sourceId"])
    assert source is not None
    assert source.last_accessed_at is not None


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 2 — submitting twice creates one source
# ─────────────────────────────────────────────────────────────────────────────


def test_submitting_the_same_file_twice_creates_one_source(
    jobs: JobStore,
    sources: SourceStore,
    workspace: Workspace,
    settings: Settings,
    ingest_worker: Worker,
) -> None:
    """Phase 3, exit criterion 2. Two jobs, deliberately — the user asked twice —
    but one source, because re-fetching two gigabytes would be the waste."""
    submit(jobs, workspace, sources, settings, str(SAMPLE_AV), "ingest-1")
    submit(jobs, workspace, sources, settings, str(SAMPLE_AV), "ingest-2")

    first = ingest_worker.run_once()
    second = ingest_worker.run_once()

    assert first is not None and second is not None
    assert first.status is JobStatus.COMPLETED
    assert second.status is JobStatus.COMPLETED

    assert len(sources.eviction_candidates(uid="user-1")) == 1

    first_id = (first.stages[0].checkpoint or {})["sourceId"]
    second_checkpoint = second.stages[0].checkpoint or {}
    assert second_checkpoint["sourceId"] == first_id
    assert second_checkpoint["deduped"] is True


def test_dedupe_reports_itself_in_the_stage_detail(
    jobs: JobStore,
    sources: SourceStore,
    workspace: Workspace,
    settings: Settings,
    ingest_worker: Worker,
) -> None:
    submit(jobs, workspace, sources, settings, str(SAMPLE_AV), "ingest-1")
    ingest_worker.run_once()
    submit(jobs, workspace, sources, settings, str(SAMPLE_AV), "ingest-2")
    ingest_worker.run_once()

    details = [e.get("detail") for e in jobs.events("ingest-2") if e.get("detail")]
    assert any("already ingested" in str(d) for d in details)


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 4 — failures arrive as codes, not stack traces
# ─────────────────────────────────────────────────────────────────────────────


def test_a_video_with_no_audio_fails_with_its_code(
    jobs: JobStore,
    sources: SourceStore,
    workspace: Workspace,
    settings: Settings,
    ingest_worker: Worker,
) -> None:
    submit(jobs, workspace, sources, settings, str(VIDEO_ONLY), "no-audio")

    finished = ingest_worker.run_once()

    assert finished is not None
    assert finished.status is JobStatus.FAILED
    assert finished.error is not None
    assert finished.error.code == IngestErrorCode.NO_SUITABLE_FORMAT.value
    assert finished.error.retryable is False, "this will never succeed on a retry"
    assert "audio" in finished.error.message


def test_a_missing_file_fails_without_burning_attempts(
    jobs: JobStore,
    sources: SourceStore,
    workspace: Workspace,
    settings: Settings,
    ingest_worker: Worker,
) -> None:
    """A path that does not exist will not start existing. Retrying it three
    times wastes the user's time to learn nothing."""
    submit(jobs, workspace, sources, settings, "./nope-not-here.mp4", "missing")

    finished = ingest_worker.run_once()

    assert finished is not None
    assert finished.status is JobStatus.FAILED
    assert finished.error is not None
    assert finished.error.code == IngestErrorCode.NOT_FOUND.value
    assert finished.attempts == 1


def test_a_video_over_the_duration_cap_is_refused(
    jobs: JobStore,
    sources: SourceStore,
    workspace: Workspace,
    settings: Settings,
    workers: WorkerStore,
) -> None:
    """Checked before the download, not after — that is the whole point of doing
    metadata first."""
    strict = settings.model_copy(update={"max_source_duration_sec": 1.0})
    registry = StageRegistry()
    registry.register(
        DownloadStage(sources=sources, workspace=workspace, adapter_factory=local_only_factory)
    )
    probe: object = lambda: None  # noqa: E731
    worker = Worker(
        settings=strict,
        jobs=jobs,
        workers=workers,
        broker=ModelBroker(reserve_mb=700, probe=probe),  # type: ignore[arg-type]
        registry_factory=lambda _t: registry,
    )

    submit(jobs, workspace, sources, strict, str(SAMPLE_AV), "too-long")
    finished = worker.run_once()

    assert finished is not None
    assert finished.status is JobStatus.FAILED
    assert finished.error is not None
    assert finished.error.code == IngestErrorCode.TOO_LONG.value
    assert "limit" in finished.error.message


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 3 — GC reclaims, and the pipeline still runs
# ─────────────────────────────────────────────────────────────────────────────


def test_the_pipeline_still_runs_with_the_workspace_over_its_cap(
    jobs: JobStore, sources: SourceStore, settings: Settings, workers: WorkerStore, tmp_path: Path
) -> None:
    """Phase 3, exit criterion 3.

    A cap of 1 GB against a workspace already holding more forces a collection
    pass. Local sources are never evicted — the workspace does not own those
    files — so this proves the *stage* survives a GC pass, not that GC deletes
    the thing under test.
    """
    tight = Workspace(tmp_path / "tight", max_gb=1)
    # Pre-fill with reclaimable junk that no source document claims.
    (tight.sources_dir / "junk.bin").write_bytes(b"\x00" * (2 * 1024 * 1024))

    registry = StageRegistry()
    registry.register(
        DownloadStage(sources=sources, workspace=tight, adapter_factory=local_only_factory)
    )
    probe: object = lambda: None  # noqa: E731
    worker = Worker(
        settings=settings,
        jobs=jobs,
        workers=workers,
        broker=ModelBroker(reserve_mb=700, probe=probe),  # type: ignore[arg-type]
        registry_factory=lambda _t: registry,
    )

    submit(jobs, tight, sources, settings, str(SAMPLE_AV), "gc-job")
    finished = worker.run_once()

    assert finished is not None
    assert finished.status is JobStatus.COMPLETED, finished.error


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 5 — resumability, offline
# ─────────────────────────────────────────────────────────────────────────────


def test_a_completed_ingest_is_not_repeated_on_a_rerun(
    jobs: JobStore,
    sources: SourceStore,
    workspace: Workspace,
    settings: Settings,
    ingest_worker: Worker,
) -> None:
    """The stage contract: a DONE stage is never re-executed. Re-running the same
    job must not re-hash a gigabyte."""
    submit(jobs, workspace, sources, settings, str(SAMPLE_AV), "ingest-1")
    first = ingest_worker.run_once()
    assert first is not None
    checkpoint = first.stages[0].checkpoint

    # A second pass over the finished job finds nothing to claim.
    assert ingest_worker.run_once() is None

    stored = jobs.get("ingest-1")
    assert stored is not None
    assert stored.stages[0].checkpoint == checkpoint
