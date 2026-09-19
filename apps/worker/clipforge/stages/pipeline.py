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

from clipforge_contracts import (
    ClipOptions,
    CompileOptions,
    Job,
    JobStatus,
    JobType,
    Lane,
    MusicOptions,
    RemakeOptions,
    ResearchOptions,
    StageName,
    StageStatus,
)
from clipforge_contracts import Stage as ContractStage

from clipforge.config import Settings
from clipforge.media.speech import SpeechSynth
from clipforge.media.workspace import Workspace
from clipforge.publish.youtube import YouTubeClient
from clipforge.research.signals import TrendProvider, VideoFinder
from clipforge.stages.analyze import AnalyzeStage
from clipforge.stages.base import Stage, StageRegistry
from clipforge.stages.download import DownloadStage
from clipforge.stages.echo import echo_registry
from clipforge.stages.publish import PublishStage
from clipforge.stages.render import RenderStage
from clipforge.stages.transcribe import TranscribeStage
from clipforge.store.blobs import BlobStore
from clipforge.store.channels import ChannelStore
from clipforge.store.firestore import (
    CandidateStore,
    ClipStore,
    PreferenceStore,
    PublicationStore,
    SourceStore,
    TrendStore,
)
from clipforge.store.transcripts import TranscriptArchive, TranscriptStore

__all__ = [
    "CLIP_PIPELINE",
    "COMPILE_PIPELINE",
    "MUSIC_PIPELINE",
    "PUBLISH_PIPELINE",
    "REMAKE_PIPELINE",
    "RESEARCH_PIPELINE",
    "UPLOAD_PIPELINE",
    "build_clip_registry",
    "build_clip_stages",
    "build_compile_registry",
    "build_publish_registry",
    "build_registry_factory",
    "build_remake_registry",
    "build_research_registry",
    "build_upload_registry",
    "clip_stages",
    "new_clip_job",
    "new_compile_job",
    "new_music_job",
    "new_publish_job",
    "new_remake_job",
    "new_research_job",
    "new_upload_job",
]


# The CLIP pipeline, in execution order. This is the single definition of the
# pipeline's *shape*, and it is deliberately separate from the code that builds
# runnable stages: `submit` needs to know what a job will consist of without
# constructing a Whisper loader, an Ollama client and an ffmpeg wrapper to find
# out. Keeping the two together previously meant `submit` — which passes no
# machinery — silently produced one-stage jobs that downloaded a video and
# stopped.
CLIP_PIPELINE: tuple[tuple[StageName, Lane], ...] = (
    (StageName.DOWNLOAD, Lane.CPU),
    (StageName.TRANSCRIBE, Lane.GPU),
    (StageName.ANALYZE, Lane.GPU),
    (StageName.RENDER, Lane.CPU),
)

# A publish job is one stage. It is its own job type rather than a fifth CLIP
# stage because it starts on the far side of a human approval — see
# clipforge/stages/publish.py.
PUBLISH_PIPELINE: tuple[tuple[StageName, Lane], ...] = ((StageName.PUBLISH, Lane.CPU),)

# So is a music job, and for the same reason: someone chooses a track while
# watching a finished clip, which is hours after CLIP ended. CPU lane — the
# analysis is numpy arithmetic and the mix is ffmpeg, neither of which wants the
# GPU that Whisper and the model are contending over.
MUSIC_PIPELINE: tuple[tuple[StageName, Lane], ...] = ((StageName.MUSIC, Lane.CPU),)

UPLOAD_PIPELINE: tuple[tuple[StageName, Lane], ...] = ((StageName.UPLOAD, Lane.CPU),)

# And so is a remake, for the third time and the same reason: a correction is
# something someone decides while watching a clip that was finished hours ago.
# The CPU lane even though it may take a GPU lease part-way through — to read the
# reviewer's note, or to re-align captions against a new narration. Those are
# seconds of a job that is otherwise minutes of ffmpeg, and occupying the GPU
# lane for the whole of it would block a transcription for no reason.
REMAKE_PIPELINE: tuple[tuple[StageName, Lane], ...] = ((StageName.REMAKE, Lane.CPU),)

# Research is two stages because they cost different things: the first is
# somebody else's servers and the second is the local model. Keeping them apart
# means a run with curation turned off never touches the GPU lane at all.
RESEARCH_PIPELINE: tuple[tuple[StageName, Lane], ...] = (
    (StageName.RESEARCH, Lane.CPU),
    (StageName.CURATE, Lane.GPU),
)

# A compilation is the CLIP pipeline folded over several sources — ingest
# them all, choose from each, join the results — and it is its own job type
# for the reason D10 gives: a job's stage list is authoritative for its whole
# life, and a harvest must not carry stages it can never run.
COMPILE_PIPELINE: tuple[tuple[StageName, Lane], ...] = (
    (StageName.GATHER, Lane.CPU),
    (StageName.SELECT, Lane.GPU),
    (StageName.ASSEMBLE, Lane.CPU),
)


def build_clip_stages(
    *,
    settings: Settings,
    sources: SourceStore,
    workspace: Workspace,
    transcripts: TranscriptStore,
    archive: TranscriptArchive,
    candidates: CandidateStore,
    clips: ClipStore,
    blobs: BlobStore,
) -> list[Stage]:
    """Every CLIP stage, in pipeline order, ready to run.

    Every dependency is required. An optional one would mean a stage could go
    missing without anyone noticing, which is exactly the failure this module
    used to have.
    """
    del settings
    stages: list[Stage] = [
        DownloadStage(sources=sources, workspace=workspace),
        TranscribeStage(
            sources=sources,
            transcripts=transcripts,
            archive=archive,
            workspace=workspace,
        ),
        AnalyzeStage(sources=sources, candidates=candidates, archive=archive),
        RenderStage(
            sources=sources,
            candidates=candidates,
            clips=clips,
            archive=archive,
            workspace=workspace,
            blobs=blobs,
        ),
    ]

    # The shape and the implementations must not be able to drift apart: a stage
    # in the job document with no implementation fails the job at that step,
    # after everything before it has already run.
    assert tuple((s.name, s.lane) for s in stages) == CLIP_PIPELINE, (  # noqa: S101
        "CLIP_PIPELINE and build_clip_stages disagree about the pipeline"
    )
    return stages


def build_clip_registry(
    *,
    settings: Settings,
    sources: SourceStore,
    workspace: Workspace,
    transcripts: TranscriptStore,
    archive: TranscriptArchive,
    candidates: CandidateStore,
    clips: ClipStore,
    blobs: BlobStore,
) -> StageRegistry:
    registry = StageRegistry()
    for stage in build_clip_stages(
        settings=settings,
        sources=sources,
        workspace=workspace,
        transcripts=transcripts,
        archive=archive,
        candidates=candidates,
        clips=clips,
        blobs=blobs,
    ):
        registry.register(stage)
    return registry


def clip_stages(
    stages: Sequence[Stage] | Sequence[tuple[StageName, Lane]] | None = None,
) -> list[ContractStage]:
    """The `stages` array for a fresh job.

    Accepts either runnable stages or bare ``(name, lane)`` pairs, and defaults
    to the full CLIP pipeline. Tests build partial pipelines on purpose — one
    stage in isolation is the only way to assert what that stage does — so the
    narrower form has to stay available; production always takes the default.
    """
    shape = CLIP_PIPELINE if stages is None else stages
    pairs = [item if isinstance(item, tuple) else (item.name, item.lane) for item in shape]
    return [ContractStage(name=name, lane=lane, status=StageStatus.PENDING) for name, lane in pairs]


def new_clip_job(
    *,
    uid: str,
    submission: str,
    stages: Sequence[Stage] | Sequence[tuple[StageName, Lane]] | None = None,
    job_id: str | None = None,
    max_attempts: int = 3,
    options: ClipOptions | None = None,
) -> Job:
    """Build a CLIP job for a submission.

    `submission` is kept verbatim and separately from `sourceId`, so the job can
    be re-run from the original input even if its source document was later
    garbage-collected. `options` is the brief — what to look for, how many, how
    long — and None is the pipeline's own judgement.
    """
    now = datetime.now(UTC)
    return Job(
        id=job_id or uuid.uuid4().hex,
        uid=uid,
        type=JobType.CLIP,
        status=JobStatus.QUEUED,
        submission=submission,
        clip_options=options,
        stages=clip_stages(stages),
        attempts=0,
        max_attempts=max_attempts,
        created_at=now,
        updated_at=now,
    )


def new_publish_job(
    *,
    uid: str,
    clip_id: str,
    job_id: str | None = None,
    publish_at: datetime | None = None,
    max_attempts: int = 3,
) -> Job:
    """Build a PUBLISH job for one approved clip.

    ``publish_at`` becomes the job's ``notBefore``, so a scheduled publish is
    simply a job the scheduler will not claim yet. No separate timer, no second
    scheduler that could disagree with the first — see
    :func:`clipforge.scheduler.lease.is_due`.
    """
    now = datetime.now(UTC)
    return Job(
        id=job_id or uuid.uuid4().hex,
        uid=uid,
        type=JobType.PUBLISH,
        status=JobStatus.QUEUED,
        clip_id=clip_id,
        not_before=publish_at,
        stages=clip_stages(PUBLISH_PIPELINE),
        attempts=0,
        max_attempts=max_attempts,
        created_at=now,
        updated_at=now,
    )


def build_music_registry(
    *,
    settings: Settings,
    clips: ClipStore,
    candidates: CandidateStore,
    sources: SourceStore,
    workspace: Workspace,
    blobs: BlobStore,
) -> StageRegistry:
    """The one-stage registry for MUSIC jobs."""
    from clipforge.stages.music import MusicStage

    registry = StageRegistry()
    registry.register(
        MusicStage(
            settings=settings,
            clips=clips,
            candidates=candidates,
            sources=sources,
            workspace=workspace,
            blobs=blobs,
        )
    )
    return registry


def new_music_job(
    *,
    uid: str,
    clip_id: str,
    options: MusicOptions,
    job_id: str | None = None,
    max_attempts: int = 1,
) -> Job:
    """Build a MUSIC job for one finished clip.

    ``max_attempts`` is 1 rather than 3. Every way this stage fails is a
    property of its inputs — a track with no audio, a source the GC has taken,
    a link that is not a link — and retrying reproduces them exactly while
    costing another download.
    """
    now = datetime.now(UTC)
    return Job(
        id=job_id or uuid.uuid4().hex,
        uid=uid,
        type=JobType.MUSIC,
        status=JobStatus.QUEUED,
        clip_id=clip_id,
        music_options=options,
        stages=clip_stages(MUSIC_PIPELINE),
        attempts=0,
        max_attempts=max_attempts,
        created_at=now,
        updated_at=now,
    )


def build_remake_registry(
    *,
    settings: Settings,
    clips: ClipStore,
    candidates: CandidateStore,
    sources: SourceStore,
    archive: TranscriptArchive,
    workspace: Workspace,
    blobs: BlobStore,
    preferences: PreferenceStore | None = None,
) -> StageRegistry:
    """The one-stage registry for REMAKE jobs.

    The synthesiser is constructed here but loads nothing: a worker with no
    speech extra installed must still start, and must still be able to reframe a
    clip — reframing is the half of this stage that needs no models at all, and
    it is the half the football case needs. A *voice* request is what produces
    the install instruction, from the stage, where the message can name the
    command that fixes it.

    The transcriber is deliberately not built here. It needs the worker's own
    `ModelBroker`, which arrives on the stage context: a second broker would be
    a second lock over one GPU, and two "exclusive" leases held at once is
    precisely the 6 GB overcommit the broker exists to prevent.
    """
    from clipforge.stages.remake import RemakeStage

    registry = StageRegistry()
    registry.register(
        RemakeStage(
            settings=settings,
            clips=clips,
            candidates=candidates,
            sources=sources,
            archive=archive,
            workspace=workspace,
            blobs=blobs,
            preferences=preferences,
            speech=_speech_synth(settings),
        )
    )
    return registry


def _speech_synth(settings: Settings) -> SpeechSynth:
    """The Kokoro adapter, or None when it cannot be constructed.

    Construction does not load the model — that happens on first use — so this
    is cheap and the failure it guards against is an import error, not a missing
    weights file. A missing weights file is reported by the stage, where the
    message can name the command that fetches it.
    """
    from clipforge.media.speech import KokoroSynth

    return KokoroSynth(
        model_path=settings.speech_model_path.expanduser(),
        voices_path=settings.speech_voices_path.expanduser(),
    )


def new_remake_job(
    *,
    uid: str,
    clip_id: str,
    options: RemakeOptions,
    job_id: str | None = None,
    max_attempts: int = 1,
) -> Job:
    """Build a REMAKE job for one finished clip.

    ``max_attempts`` is 1, as with MUSIC. Every way this stage fails is a
    property of its inputs — a source the collector has taken, a language with
    no voice, nudges that cross over — and a retry reproduces them exactly while
    spending the render time again.
    """
    now = datetime.now(UTC)
    return Job(
        id=job_id or uuid.uuid4().hex,
        uid=uid,
        type=JobType.REMAKE,
        status=JobStatus.QUEUED,
        clip_id=clip_id,
        remake_options=options,
        stages=clip_stages(REMAKE_PIPELINE),
        attempts=0,
        max_attempts=max_attempts,
        created_at=now,
        updated_at=now,
    )


def build_upload_registry(*, settings: Settings, clips: ClipStore) -> StageRegistry:
    """The one-stage registry for UPLOAD jobs.

    Builds its own bucket-backed store rather than taking the worker's. The
    worker's is chosen by ``CLIPFORGE_BLOB_STORE``, which is ``local`` on a
    machine that deliberately keeps routine renders off the network — and an
    UPLOAD job is precisely the exception to that: someone reviewing on a phone
    has asked for this one clip. Honouring the routine setting here would mean
    the request could only be granted on a worker that did not need it.

    With no bucket configured there is nothing to fall back to, so the ordinary
    store is used and the stage refuses with a message naming the two settings.
    """
    from clipforge.stages.upload import UploadStage
    from clipforge.store.blobs import FirebaseBlobStore, LocalBlobStore, build_blob_store

    local = LocalBlobStore(settings.workspace_dir)
    blobs: BlobStore = (
        FirebaseBlobStore(
            local,
            bucket=settings.firebase_storage_bucket,
            retention_days=settings.clip_retention_days,
            timeout_s=settings.upload_timeout_seconds,
        )
        if settings.firebase_storage_bucket
        else build_blob_store(settings)
    )

    registry = StageRegistry()
    registry.register(UploadStage(clips=clips, blobs=blobs))
    return registry


def new_upload_job(
    *,
    uid: str,
    clip_id: str,
    job_id: str | None = None,
    max_attempts: int = 3,
) -> Job:
    """Build an UPLOAD job for one clip a reviewer cannot currently play."""
    now = datetime.now(UTC)
    return Job(
        id=job_id or uuid.uuid4().hex,
        uid=uid,
        type=JobType.UPLOAD,
        status=JobStatus.QUEUED,
        clip_id=clip_id,
        stages=clip_stages(UPLOAD_PIPELINE),
        attempts=0,
        max_attempts=max_attempts,
        created_at=now,
        updated_at=now,
    )


def build_publish_registry(
    *,
    settings: Settings,
    clips: ClipStore,
    publications: PublicationStore,
    channels: ChannelStore | None = None,
) -> StageRegistry:
    """The one-stage registry for PUBLISH jobs.

    The YouTube client is built lazily, on the first job that needs it. A worker
    that never publishes must start cleanly on a machine that has never
    authorised anything — reading a token file at construction time would make
    "no credentials" a startup failure rather than a publish-time one.
    """

    def client_factory() -> YouTubeClient:
        from clipforge.publish.credentials import TokenStore
        from clipforge.publish.youtube import load_client_secrets

        client_id, client_secret = load_client_secrets(settings.youtube_client_secrets)
        return YouTubeClient(
            tokens=TokenStore(settings.youtube_token_store),
            client_id=client_id,
            client_secret=client_secret,
        )

    registry = StageRegistry()
    registry.register(
        PublishStage(
            clips=clips,
            publications=publications,
            client_factory=client_factory,
            channels=channels,
        )
    )
    return registry


def build_research_registry(
    *,
    settings: Settings,
    trends: TrendStore,
    providers: Sequence[TrendProvider] | None = None,
    finder: VideoFinder | None = None,
) -> StageRegistry:
    """The two-stage registry for RESEARCH jobs.

    The providers default to whatever the settings enable. Tests pass their
    own, which is the whole reason the port exists: nothing in this registry
    can tell a fixture from the internet.
    """
    from clipforge.research.providers import YouTubeSearchProvider, iter_default_providers
    from clipforge.stages.research import CurateStage, ResearchStage

    if providers is None:
        providers = iter_default_providers(
            user_agent=settings.research_user_agent,
            subreddits=settings.research_subreddit_list,
            timeout_s=settings.research_timeout_seconds,
            google_trends=settings.research_google_trends,
            reddit=settings.research_reddit,
            youtube=settings.research_youtube,
        )
        # The finder is the YouTube provider, whether or not it is also asked
        # as a provider: a trending search with no videos is a row nobody can
        # act on.
        if finder is None and settings.research_youtube:
            finder = next(
                (p for p in providers if isinstance(p, YouTubeSearchProvider)),
                YouTubeSearchProvider(),
            )

    registry = StageRegistry()
    registry.register(ResearchStage(trends=trends, providers=providers, finder=finder))
    registry.register(CurateStage(trends=trends))
    return registry


def new_research_job(
    *,
    uid: str,
    options: ResearchOptions,
    job_id: str | None = None,
    max_attempts: int = 2,
    schedule_id: str | None = None,
) -> Job:
    """Build a RESEARCH job. Two attempts: the failures worth retrying are network ones.

    ``schedule_id`` names the schedule that fired it, when one did. The job is
    otherwise identical to one a person created, which is the point.
    """
    now = datetime.now(UTC)
    return Job(
        id=job_id or uuid.uuid4().hex,
        uid=uid,
        type=JobType.RESEARCH,
        status=JobStatus.QUEUED,
        research_options=options,
        schedule_id=schedule_id,
        stages=clip_stages(RESEARCH_PIPELINE),
        attempts=0,
        max_attempts=max_attempts,
        created_at=now,
        updated_at=now,
    )


def build_compile_registry(
    *,
    settings: Settings,
    sources: SourceStore,
    workspace: Workspace,
    transcripts: TranscriptStore,
    archive: TranscriptArchive,
    candidates: CandidateStore,
    clips: ClipStore,
    blobs: BlobStore,
) -> StageRegistry:
    """The three-stage registry for COMPILE jobs.

    Built from the same DownloadStage, TranscribeStage and AnalyzeStage the
    CLIP pipeline runs, so a compilation ingests, transcribes and judges
    exactly as a harvest does — with the theme added to the judging.
    """
    from clipforge.stages.compile import AssembleStage, GatherStage, SelectStage

    del settings
    download = DownloadStage(sources=sources, workspace=workspace)
    transcribe = TranscribeStage(
        sources=sources, transcripts=transcripts, archive=archive, workspace=workspace
    )
    analyze = AnalyzeStage(sources=sources, candidates=candidates, archive=archive)

    registry = StageRegistry()
    registry.register(GatherStage(download=download))
    registry.register(
        SelectStage(
            transcribe=transcribe,
            analyze=analyze,
            archive=archive,
            candidates=candidates,
            sources=sources,
        )
    )
    registry.register(
        AssembleStage(
            sources=sources, clips=clips, archive=archive, workspace=workspace, blobs=blobs
        )
    )
    assert tuple((s.name, s.lane) for s in registry._stages.values()) == COMPILE_PIPELINE, (  # noqa: S101
        "COMPILE_PIPELINE and build_compile_registry disagree about the pipeline"
    )
    return registry


def new_compile_job(
    *,
    uid: str,
    options: CompileOptions,
    job_id: str | None = None,
    max_attempts: int = 2,
) -> Job:
    """Build a COMPILE job.

    Two attempts rather than three. GATHER keeps what it fetched across a
    retry and TRANSCRIBE is cached by content hash, so a second attempt is
    cheap — but a third would only re-run the model over the same inputs.
    """
    now = datetime.now(UTC)
    return Job(
        id=job_id or uuid.uuid4().hex,
        uid=uid,
        type=JobType.COMPILE,
        status=JobStatus.QUEUED,
        compile_options=options,
        stages=clip_stages(COMPILE_PIPELINE),
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
    clips: ClipStore,
    publications: PublicationStore,
    blobs: BlobStore,
    trends: TrendStore,
    channels: ChannelStore | None = None,
    preferences: PreferenceStore | None = None,
    providers: Sequence[TrendProvider] | None = None,
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
        clips=clips,
        blobs=blobs,
    )

    publish = build_publish_registry(
        settings=settings, clips=clips, publications=publications, channels=channels
    )

    upload = build_upload_registry(settings=settings, clips=clips)

    music = build_music_registry(
        settings=settings,
        clips=clips,
        candidates=candidates,
        sources=sources,
        workspace=workspace,
        blobs=blobs,
    )

    remake = build_remake_registry(
        settings=settings,
        clips=clips,
        candidates=candidates,
        sources=sources,
        archive=archive,
        workspace=workspace,
        blobs=blobs,
        preferences=preferences,
    )

    research = build_research_registry(settings=settings, trends=trends, providers=providers)

    compile_ = build_compile_registry(
        settings=settings,
        sources=sources,
        workspace=workspace,
        transcripts=transcripts,
        archive=archive,
        candidates=candidates,
        clips=clips,
        blobs=blobs,
    )

    def factory(job_type: JobType) -> StageRegistry:
        if job_type is JobType.ECHO:
            return echo_registry()
        if job_type is JobType.CLIP:
            return clip
        if job_type is JobType.PUBLISH:
            return publish
        if job_type is JobType.MUSIC:
            return music
        if job_type is JobType.UPLOAD:
            return upload
        if job_type is JobType.REMAKE:
            return remake
        if job_type is JobType.RESEARCH:
            return research
        if job_type is JobType.COMPILE:
            return compile_
        raise NotImplementedError(f"job type {job_type.value} has no stage implementations")

    return factory
