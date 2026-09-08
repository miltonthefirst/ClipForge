"""Assembling the CLIP pipeline from the stages that currently exist.

The pipeline is built rather than hardcoded, because it grows one stage per phase
(DOWNLOAD in Phase 3, TRANSCRIBE in 4, ANALYZE in 5, RENDER in 6, PUBLISH in 8).
Building it from a registry means a job created today carries exactly the stages
that can actually run today, instead of a fixed list with `NotImplementedError`
sitting in the middle of it.

That is not a temporary scaffold. A job's `stages` array is written at creation
and is authoritative for that job forever — so a job created before a phase
landed keeps its own shape and still resumes correctly, rather than acquiring a
stage it never planned for halfway through a retry.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from clipforge_contracts import Job, JobStatus, JobType, StageStatus
from clipforge_contracts import Stage as ContractStage

from clipforge.config import Settings
from clipforge.media.workspace import Workspace
from clipforge.stages.analyze import AnalyzeStage
from clipforge.stages.base import Stage, StageRegistry
from clipforge.stages.download import DownloadStage
from clipforge.stages.echo import echo_registry
from clipforge.stages.transcribe import TranscribeStage
from clipforge.store.firestore import CandidateStore, SourceStore
from clipforge.store.transcripts import TranscriptArchive, TranscriptStore

__all__ = [
    "build_clip_registry",
    "build_clip_stages",
    "build_registry_factory",
    "clip_stages",
    "new_clip_job",
]


def build_clip_stages(
    *,
    settings: Settings,
    sources: SourceStore,
    workspace: Workspace,
    transcripts: TranscriptStore | None = None,
    archive: TranscriptArchive | None = None,
    candidates: CandidateStore | None = None,
) -> list[Stage]:
    """Every CLIP stage that is implemented, in pipeline order.

    Later phases append here. The order is the execution order.
    """
    del settings
    stages: list[Stage] = [DownloadStage(sources=sources, workspace=workspace)]

    # TRANSCRIBE needs Firestore, so it is only assembled where a client exists.
    # `submit` builds the stage list to shape a job document and has no need to
    # construct the machinery that runs it.
    if transcripts is not None and archive is not None:
        stages.append(
            TranscribeStage(
                sources=sources,
                transcripts=transcripts,
                archive=archive,
                workspace=workspace,
            )
        )
    if candidates is not None and archive is not None:
        stages.append(AnalyzeStage(sources=sources, candidates=candidates, archive=archive))
    return stages


def build_clip_registry(
    *,
    settings: Settings,
    sources: SourceStore,
    workspace: Workspace,
    transcripts: TranscriptStore,
    archive: TranscriptArchive,
    candidates: CandidateStore,
) -> StageRegistry:
    registry = StageRegistry()
    for stage in build_clip_stages(
        settings=settings,
        sources=sources,
        workspace=workspace,
        transcripts=transcripts,
        archive=archive,
        candidates=candidates,
    ):
        registry.register(stage)
    return registry


def clip_stages(stages: Sequence[Stage]) -> list[ContractStage]:
    """The `stages` array for a fresh CLIP job."""
    return [
        ContractStage(name=stage.name, lane=stage.lane, status=StageStatus.PENDING)
        for stage in stages
    ]


def new_clip_job(
    *,
    uid: str,
    submission: str,
    stages: Sequence[Stage],
    job_id: str | None = None,
    max_attempts: int = 3,
) -> Job:
    """Build a CLIP job for a submission.

    `submission` is kept verbatim and separately from `sourceId`, so the job can
    be re-run from the original input even if its source document was later
    garbage-collected.
    """
    now = datetime.now(UTC)
    return Job(
        id=job_id or uuid.uuid4().hex,
        uid=uid,
        type=JobType.CLIP,
        status=JobStatus.QUEUED,
        submission=submission,
        stages=clip_stages(stages),
        attempts=0,
        max_attempts=max_attempts,
        created_at=now,
        updated_at=now,
    )


def build_registry_factory(
    *,
    settings: Settings,
    sources: SourceStore,
    workspace: Workspace,
    transcripts: TranscriptStore,
    archive: TranscriptArchive,
    candidates: CandidateStore,
) -> Callable[[JobType], StageRegistry]:
    """The worker's stage lookup, for every job type it can run.

    ECHO keeps its own registry: it touches nothing and must stay runnable on a
    machine with no media dependencies at all, which is what makes it useful for
    testing scheduler behaviour in milliseconds.
    """
    clip = build_clip_registry(
        settings=settings,
        sources=sources,
        workspace=workspace,
        transcripts=transcripts,
        archive=archive,
        candidates=candidates,
    )

    def factory(job_type: JobType) -> StageRegistry:
        if job_type is JobType.ECHO:
            return echo_registry()
        if job_type is JobType.CLIP:
            return clip
        raise NotImplementedError(f"job type {job_type.value} has no stage implementations")

    return factory
