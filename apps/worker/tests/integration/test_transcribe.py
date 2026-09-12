"""The TRANSCRIBE stage against the emulator, with no GPU.

Phase 4, exit criterion 3 — re-running a job is a cache hit and the stage reports
SKIPPED. Whisper is substituted here by a transcriber that returns a fixed
transcript: what is under test is the *caching, storage and resumption* logic,
and running a real model would make that slow, non-deterministic, and impossible
in CI without proving anything extra.

Real transcription accuracy is covered in tests/gpu/test_transcribe_accuracy.py,
against a public-domain fixture with a known reference.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from clipforge.config import Settings
from clipforge.media.sources import LocalFileAdapter
from clipforge.media.workspace import Workspace
from clipforge.models.broker import ModelBroker
from clipforge.models.whisper import TranscriptionResult, model_version
from clipforge.scheduler.worker import Worker
from clipforge.stages.base import Stage, StageContext, StageRegistry
from clipforge.stages.download import DownloadStage
from clipforge.stages.pipeline import new_clip_job
from clipforge.stages.transcribe import SourceNotIngestedError, TranscribeStage
from clipforge.store.firestore import JobStore, SourceStore, WorkerStore
from clipforge.store.transcripts import TranscriptArchive, TranscriptStore
from clipforge_contracts import (
    JobStatus,
    StageStatus,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
)
from google.cloud import firestore

FIXTURES = Path(__file__).resolve().parents[2] / "assets" / "fixtures"
SAMPLE_AV = FIXTURES / "sample-av.mp4"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not SAMPLE_AV.is_file() or shutil.which("ffmpeg") is None,
        reason="needs the media fixtures and ffmpeg on PATH",
    ),
]


class CountingTranscriber:
    """Stands in for Whisper. Counts how often it was actually asked to work."""

    def __init__(self, version: str) -> None:
        self.version = version
        self.calls = 0

    def transcribe(
        self, audio_path: Path, *, source_id: str, language: str | None = None
    ) -> TranscriptionResult:
        del language
        self.calls += 1
        assert audio_path.is_file(), "the stage must extract audio before transcribing"
        segments = [
            TranscriptSegment(
                index=0,
                text="once upon a midnight dreary",
                start_sec=0.0,
                end_sec=2.5,
                words=[
                    TranscriptWord(text="once", start_sec=0.0, end_sec=0.4),
                    TranscriptWord(text="upon", start_sec=0.4, end_sec=0.8),
                    TranscriptWord(text="a", start_sec=0.8, end_sec=0.9),
                    TranscriptWord(text="midnight", start_sec=0.9, end_sec=1.6),
                    TranscriptWord(text="dreary", start_sec=1.6, end_sec=2.5),
                ],
            )
        ]
        return TranscriptionResult(
            transcript=Transcript(
                source_id=source_id,
                model_version=self.version,
                language="en",
                duration_sec=5.0,
                segments=segments,
                created_at=datetime.now(UTC),
            ),
            speech_spans=((0.0, 2.5),),
            realtime_factor=12.5,
            peak_vram_mb=1750,
        )


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path / "ws", max_gb=1)


@pytest.fixture
def sources(client: firestore.Client, settings: Settings) -> SourceStore:
    return SourceStore(client, settings)


@pytest.fixture
def archive(workspace: Workspace) -> TranscriptArchive:
    return TranscriptArchive(workspace.transcripts_dir)


@pytest.fixture
def transcripts(client: firestore.Client, settings: Settings) -> TranscriptStore:
    return TranscriptStore(client, settings)


@pytest.fixture
def transcriber(settings: Settings) -> CountingTranscriber:
    return CountingTranscriber(model_version(settings.whisper_model, settings.whisper_compute_type))


@pytest.fixture
def pipeline_worker(
    jobs: JobStore,
    workers: WorkerStore,
    sources: SourceStore,
    transcripts: TranscriptStore,
    archive: TranscriptArchive,
    workspace: Workspace,
    settings: Settings,
    transcriber: CountingTranscriber,
) -> Worker:
    registry = StageRegistry()
    registry.register(
        DownloadStage(
            sources=sources,
            workspace=workspace,
            adapter_factory=lambda *_a, **_k: LocalFileAdapter(),
        )
    )
    registry.register(
        TranscribeStage(
            sources=sources,
            transcripts=transcripts,
            archive=archive,
            workspace=workspace,
            transcriber_factory=lambda _ctx: transcriber,
        )
    )
    probe: object = lambda: None  # noqa: E731 - no GPU in this tier
    return Worker(
        settings=settings,
        jobs=jobs,
        workers=workers,
        broker=ModelBroker(reserve_mb=700, probe=probe),  # type: ignore[arg-type]
        registry_factory=lambda _t: registry,
    )


def submit(jobs: JobStore, sources: SourceStore, workspace: Workspace, job_id: str) -> None:
    stages: list[Stage] = [
        DownloadStage(sources=sources, workspace=workspace),
        TranscribeStage(
            sources=sources,
            transcripts=TranscriptStore.__new__(TranscriptStore),
            archive=TranscriptArchive(workspace.transcripts_dir),
            workspace=workspace,
        ),
    ]
    jobs.create(new_clip_job(uid="user-1", submission=str(SAMPLE_AV), stages=stages, job_id=job_id))


# ─────────────────────────────────────────────────────────────────────────────
# The happy path
# ─────────────────────────────────────────────────────────────────────────────


def test_a_job_ingests_then_transcribes(
    jobs: JobStore,
    sources: SourceStore,
    workspace: Workspace,
    archive: TranscriptArchive,
    transcripts: TranscriptStore,
    pipeline_worker: Worker,
    transcriber: CountingTranscriber,
) -> None:
    submit(jobs, sources, workspace, "job-1")

    finished = pipeline_worker.run_once()

    assert finished is not None
    assert finished.status is JobStatus.COMPLETED, finished.error
    assert [s.status for s in finished.stages] == [StageStatus.DONE, StageStatus.DONE]
    assert transcriber.calls == 1

    checkpoint = finished.stages[1].checkpoint or {}
    assert Path(checkpoint["transcriptPath"]).is_file()
    assert Path(checkpoint["vadPath"]).is_file()
    assert checkpoint["wordCount"] == 5
    assert checkpoint["realtimeFactor"] == 12.5


def test_the_transcript_is_stored_on_disk_not_in_firestore(
    jobs: JobStore,
    sources: SourceStore,
    workspace: Workspace,
    archive: TranscriptArchive,
    transcripts: TranscriptStore,
    pipeline_worker: Worker,
    settings: Settings,
) -> None:
    """A 60-minute word-level transcript approaches Firestore's 1 MiB document
    limit, and the PWA never needs one whole."""
    submit(jobs, sources, workspace, "job-1")
    finished = pipeline_worker.run_once()
    assert finished is not None

    source_id = (finished.stages[0].checkpoint or {})["sourceId"]
    version = model_version(settings.whisper_model, settings.whisper_compute_type)

    ref = transcripts.get(source_id, version)
    assert ref is not None
    assert ref.word_count == 5
    assert ref.segment_count == 1
    assert ref.language == "en"
    assert Path(ref.local_path).is_file()
    assert ref.vad_path and Path(ref.vad_path).is_file()


def test_the_extracted_audio_is_cleaned_up(
    jobs: JobStore, sources: SourceStore, workspace: Workspace, pipeline_worker: Worker
) -> None:
    """The WAV is reconstructible from the source and can be hundreds of MB; the
    transcript is the artefact worth keeping."""
    submit(jobs, sources, workspace, "job-1")
    pipeline_worker.run_once()

    assert list(workspace.tmp_dir.glob("*.wav")) == []


def test_word_level_timestamps_survive_the_round_trip(
    jobs: JobStore,
    sources: SourceStore,
    workspace: Workspace,
    archive: TranscriptArchive,
    pipeline_worker: Worker,
    settings: Settings,
) -> None:
    """Phase 5 snaps boundaries to these and Phase 6 builds captions from them,
    so losing them in serialisation would break both."""
    submit(jobs, sources, workspace, "job-1")
    finished = pipeline_worker.run_once()
    assert finished is not None

    content_hash = (finished.stages[0].checkpoint or {})["contentHash"]
    version = model_version(settings.whisper_model, settings.whisper_compute_type)

    loaded = archive.load(content_hash, version)
    assert loaded is not None
    words = loaded.segments[0].words or []
    assert [w.text for w in words] == ["once", "upon", "a", "midnight", "dreary"]
    assert words[3].start_sec == 0.9

    assert archive.load_speech_spans(content_hash, version) == ((0.0, 2.5),)


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 3 — the cache
# ─────────────────────────────────────────────────────────────────────────────


def test_a_second_job_over_the_same_media_reports_skipped(
    jobs: JobStore,
    sources: SourceStore,
    workspace: Workspace,
    pipeline_worker: Worker,
    transcriber: CountingTranscriber,
) -> None:
    """Phase 4, exit criterion 3.

    SKIPPED rather than DONE, so "this was already transcribed" stays
    distinguishable from "we transcribed it" in the job document — which is what
    makes the cache claim checkable rather than merely asserted.
    """
    submit(jobs, sources, workspace, "job-1")
    submit(jobs, sources, workspace, "job-2")

    first = pipeline_worker.run_once()
    second = pipeline_worker.run_once()

    assert first is not None and second is not None
    assert first.stages[1].status is StageStatus.DONE
    assert second.stages[1].status is StageStatus.SKIPPED
    assert second.status is JobStatus.COMPLETED
    assert transcriber.calls == 1, "the model was re-run despite a cache hit"
    assert (second.stages[1].checkpoint or {})["cacheHit"] is True


def test_the_cache_is_keyed_on_content_not_source_id(
    jobs: JobStore,
    sources: SourceStore,
    workspace: Workspace,
    archive: TranscriptArchive,
    pipeline_worker: Worker,
    transcriber: CountingTranscriber,
    tmp_path: Path,
) -> None:
    """The same video under a different path is a different source with identical
    bytes. Re-transcribing it would cost GPU minutes for a byte-identical
    result."""
    submit(jobs, sources, workspace, "job-1")
    pipeline_worker.run_once()

    # A byte-identical copy at a different path.
    twin = tmp_path / "twin.mp4"
    shutil.copy2(SAMPLE_AV, twin)
    stages: list[Stage] = [
        DownloadStage(sources=sources, workspace=workspace),
        TranscribeStage(
            sources=sources,
            transcripts=TranscriptStore.__new__(TranscriptStore),
            archive=archive,
            workspace=workspace,
        ),
    ]
    jobs.create(new_clip_job(uid="user-1", submission=str(twin), stages=stages, job_id="job-2"))

    second = pipeline_worker.run_once()

    assert second is not None
    assert second.status is JobStatus.COMPLETED
    assert transcriber.calls == 1, "identical bytes were transcribed twice"


def test_a_corrupt_cache_entry_causes_a_retranscription_not_a_failure(
    jobs: JobStore,
    sources: SourceStore,
    workspace: Workspace,
    archive: TranscriptArchive,
    pipeline_worker: Worker,
    transcriber: CountingTranscriber,
    settings: Settings,
) -> None:
    """Failing a job over a file we wrote ourselves would be the wrong response."""
    submit(jobs, sources, workspace, "job-1")
    first = pipeline_worker.run_once()
    assert first is not None

    content_hash = (first.stages[0].checkpoint or {})["contentHash"]
    version = model_version(settings.whisper_model, settings.whisper_compute_type)
    transcript_path, _ = archive.paths_for(content_hash, version)
    transcript_path.write_text("{ not json at all", encoding="utf-8")

    submit(jobs, sources, workspace, "job-2")
    second = pipeline_worker.run_once()

    assert second is not None
    assert second.status is JobStatus.COMPLETED
    assert transcriber.calls == 2


# ─────────────────────────────────────────────────────────────────────────────
# Failure handling
# ─────────────────────────────────────────────────────────────────────────────


def test_a_garbage_collected_source_reports_something_actionable(
    sources: SourceStore,
    workspace: Workspace,
    transcripts: TranscriptStore,
    archive: TranscriptArchive,
    settings: Settings,
    transcriber: CountingTranscriber,
) -> None:
    """The workspace GC can reclaim a source between stages. The message has to
    say what to do about it, not surface a bare FileNotFoundError."""
    from clipforge_contracts import Source, SourceProvider

    doomed = workspace.sources_dir / "doomed.mp4"
    shutil.copy2(SAMPLE_AV, doomed)
    now = datetime.now(UTC)
    sources.save(
        Source(
            id="gone",
            uid="user-1",
            provider=SourceProvider.YOUTUBE,
            external_id="gone",
            content_hash="deadbeef",
            local_path=str(doomed),
            size_bytes=doomed.stat().st_size,
            created_at=now,
            last_accessed_at=now,
        )
    )
    doomed.unlink()

    stage = TranscribeStage(
        sources=sources,
        transcripts=transcripts,
        archive=archive,
        workspace=workspace,
        transcriber_factory=lambda _ctx: transcriber,
    )
    probe: object = lambda: None  # noqa: E731
    job = new_clip_job(
        uid="user-1",
        submission="irrelevant",
        stages=[stage],
        job_id="gc-job",
    ).model_copy(update={"source_id": "gone"})

    context = StageContext(
        job=job,
        stage_name=stage.name,
        checkpoint=None,
        settings=settings,
        broker=ModelBroker(reserve_mb=700, probe=probe),  # type: ignore[arg-type]
    )

    with pytest.raises(SourceNotIngestedError, match="re-run the job"):
        stage.run(context)
    assert transcriber.calls == 0
