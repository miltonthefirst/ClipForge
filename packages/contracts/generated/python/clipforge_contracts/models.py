# ClipForge contracts - GENERATED FILE, DO NOT EDIT.
#
# Source of truth: packages/contracts/schemas/clipforge.json
# Regenerate with:
#   uv run --project apps/worker python packages/contracts/scripts/generate_python.py
#
# Editing this file by hand is pointless: CI regenerates it and fails on any
# difference. Change the schema instead.

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class JobStatus(StrEnum):
    """
    Lifecycle of a job. Transitions are defined in docs/PLAN.md 3.3 and enforced by clipforge.scheduler.lease.
    """

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class JobType(StrEnum):
    """
    ECHO is a no-op job of three artificial stages used to exercise the scheduler without touching media. CLIP is the real pipeline.
    """

    ECHO = "ECHO"
    CLIP = "CLIP"


class StageStatus(StrEnum):
    """
    A stage that is DONE is never re-executed on retry. That is what makes a crash during ANALYZE cost seconds rather than the twenty minutes of DOWNLOAD and TRANSCRIBE that preceded it.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class StageName(StrEnum):
    """
    Ordered pipeline steps. ECHO_* belong to the ECHO job type only.
    """

    ECHO_ONE = "ECHO_ONE"
    ECHO_TWO = "ECHO_TWO"
    ECHO_THREE = "ECHO_THREE"
    DOWNLOAD = "DOWNLOAD"
    TRANSCRIBE = "TRANSCRIBE"
    ANALYZE = "ANALYZE"
    RENDER = "RENDER"
    PUBLISH = "PUBLISH"


class Lane(StrEnum):
    """
    Which scheduler lane a stage runs in. The GPU lane is depth 1 because Whisper and the LLM cannot be co-resident in 6 GB of VRAM (docs/PLAN.md 2.1).
    """

    GPU = "GPU"
    CPU = "CPU"


class StageError(BaseModel):
    """
    Structured failure detail. Captured rather than raised so a failed stage can be inspected from the PWA without reading worker logs.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    type: str
    """
    Exception class name.
    """
    message: str
    traceback: str | None = None
    retryable: bool | None = True
    """
    False marks a failure that will never succeed on retry (bad input, a rights refusal), so the scheduler fails the job immediately instead of burning its remaining attempts.
    """


class Stage(BaseModel):
    """
    One idempotent, checkpointed step of a job.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    name: StageName
    lane: Lane
    status: StageStatus
    started_at: AwareDatetime | None = Field(None, alias="startedAt")
    ended_at: AwareDatetime | None = Field(None, alias="endedAt")
    duration_ms: int | None = Field(None, alias="durationMs", ge=0)
    peak_vram_mb: int | None = Field(None, alias="peakVramMb", ge=0)
    """
    Peak VRAM observed through NVML while this stage held the ModelBroker lease. Null for CPU stages.
    """
    attempts: int | None = Field(0, ge=0)
    checkpoint: dict[str, Any] | None = None
    """
    Opaque, stage-owned resume state. The scheduler persists and returns it verbatim and never interprets it.
    """
    error: StageError | None = None


class Job(BaseModel):
    """
    One pipeline run over one source. Stored at jobs/{jobId}.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    id: str = Field(..., min_length=1)
    uid: str = Field(..., min_length=1)
    """
    Owning user. Every security rule keys off this field.
    """
    type: JobType
    status: JobStatus
    source_id: str | None = Field(None, alias="sourceId")
    stages: list[Stage] = Field(..., min_length=1)
    """
    Ordered. Executed front to back; DONE stages are skipped on retry.
    """
    worker_id: str | None = Field(None, alias="workerId")
    lease_expires_at: AwareDatetime | None = Field(None, alias="leaseExpiresAt")
    """
    Set on claim, extended by heartbeat. A RUNNING job whose lease is in the past is reclaimable by the reaper.
    """
    attempts: int = Field(..., ge=0)
    max_attempts: int = Field(..., alias="maxAttempts", ge=1)
    error: StageError | None = None
    """
    The failure that terminated the job, copied from the failing stage.
    """
    created_at: AwareDatetime = Field(..., alias="createdAt")
    updated_at: AwareDatetime = Field(..., alias="updatedAt")
    started_at: AwareDatetime | None = Field(None, alias="startedAt")
    ended_at: AwareDatetime | None = Field(None, alias="endedAt")


class JobEventKind(StrEnum):
    CREATED = "CREATED"
    CLAIMED = "CLAIMED"
    STAGE_STARTED = "STAGE_STARTED"
    STAGE_COMPLETED = "STAGE_COMPLETED"
    STAGE_FAILED = "STAGE_FAILED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    REQUEUED = "REQUEUED"


class JobEvent(BaseModel):
    """
    Append-only transition log at jobs/{jobId}/events/{eventId}. Never mutated, which is what makes it trustworthy when debugging a job that failed hours ago.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    id: str = Field(..., min_length=1)
    job_id: str = Field(..., alias="jobId", min_length=1)
    kind: JobEventKind
    at: AwareDatetime
    stage: StageName | None = None
    worker_id: str | None = Field(None, alias="workerId")
    detail: str | None = None
    attempts: int | None = Field(None, ge=0)


class SourceProvider(StrEnum):
    """
    local exists so the whole pipeline can be exercised from a committed fixture with no network, which is what keeps the integration tier runnable in CI.
    """

    YOUTUBE = "youtube"
    LOCAL = "local"


class Source(BaseModel):
    """
    One ingested long-form video at sources/{sourceId}. The media itself never leaves the worker (docs/PLAN.md decision D3).
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    id: str = Field(..., min_length=1)
    uid: str = Field(..., min_length=1)
    provider: SourceProvider
    external_id: str | None = Field(None, alias="externalId")
    """
    YouTube video id, or null for a local file.
    """
    url: str | None = None
    title: str | None = None
    channel: str | None = None
    duration_sec: float | None = Field(None, alias="durationSec", ge=0.0)
    content_hash: str | None = Field(None, alias="contentHash")
    """
    Hash of the downloaded media. Transcripts are cached against this, so re-ingesting the same video costs no GPU time.
    """
    local_path: str | None = Field(None, alias="localPath")
    """
    Worker-local absolute path. Advisory only for the PWA, which can never read it.
    """
    created_at: AwareDatetime = Field(..., alias="createdAt")


class TranscriptWord(BaseModel):
    """
    Word-level timing. Load-bearing for boundary snapping (D4) and caption generation, which is why faster-whisper is configured with word timestamps enabled.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    text: str
    start_sec: float = Field(..., alias="startSec", ge=0.0)
    end_sec: float = Field(..., alias="endSec", ge=0.0)
    probability: float | None = Field(None, ge=0.0, le=1.0)


class TranscriptSegment(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    index: int = Field(..., ge=0)
    text: str
    start_sec: float = Field(..., alias="startSec", ge=0.0)
    end_sec: float = Field(..., alias="endSec", ge=0.0)
    words: list[TranscriptWord] | None = Field([], validate_default=True)


class Transcript(BaseModel):
    """
    Stored at sources/{sourceId}/transcripts/{modelVersion}, keyed by model so re-transcribing with a better model does not destroy the old one.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    source_id: str = Field(..., alias="sourceId", min_length=1)
    model_version: str = Field(..., alias="modelVersion", min_length=1)
    """
    For example faster-whisper:large-v3-turbo:int8_float16
    """
    language: str | None = None
    duration_sec: float | None = Field(None, alias="durationSec", ge=0.0)
    segments: list[TranscriptSegment]
    created_at: AwareDatetime = Field(..., alias="createdAt")


class SubScores(BaseModel):
    """
    The rubric from docs/PLAN.md. The LLM returns these; Python computes the weighted total (decision D5), so the weighting can be changed and every historical candidate rescored without re-running inference.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    hook: int = Field(..., ge=0, le=25)
    curiosity: int = Field(..., ge=0, le=20)
    standalone: int = Field(..., ge=0, le=20)
    emotion: int = Field(..., ge=0, le=15)
    pacing: int = Field(..., ge=0, le=10)
    shareability: int = Field(..., ge=0, le=10)


class Candidate(BaseModel):
    """
    An LLM-proposed clip window at candidates/{candidateId}, after deterministic boundary snapping.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    id: str = Field(..., min_length=1)
    uid: str = Field(..., min_length=1)
    source_id: str = Field(..., alias="sourceId", min_length=1)
    job_id: str | None = Field(None, alias="jobId")
    start_sec: float = Field(..., alias="startSec", ge=0.0)
    """
    Snapped to a silence boundary and a sentence end, not the raw LLM value.
    """
    end_sec: float = Field(..., alias="endSec", ge=0.0)
    proposed_start_sec: float | None = Field(None, alias="proposedStartSec", ge=0.0)
    """
    The model's raw suggestion, retained so snapping can be evaluated against it.
    """
    proposed_end_sec: float | None = Field(None, alias="proposedEndSec", ge=0.0)
    sub_scores: SubScores = Field(..., alias="subScores")
    total: int = Field(..., ge=0, le=100)
    """
    Computed in Python from subScores. Never supplied by the model.
    """
    hook: str | None = None
    reason: str | None = None
    transcript_excerpt: str | None = Field(None, alias="transcriptExcerpt")
    model_version: str | None = Field(None, alias="modelVersion")
    prompt_version: str | None = Field(None, alias="promptVersion")
    created_at: AwareDatetime = Field(..., alias="createdAt")


class ReviewState(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class RightsBasis(StrEnum):
    """
    Why the user believes they may publish this. Publishing is gated on an explicit attestation; see docs/PLAN.md 7.
    """

    OWN_CONTENT = "OWN_CONTENT"
    LICENSED = "LICENSED"
    PERMISSION_GRANTED = "PERMISSION_GRANTED"
    FAIR_USE_CLAIMED = "FAIR_USE_CLAIMED"
    UNVERIFIED = "UNVERIFIED"


class RightsAttestation(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    basis: RightsBasis
    attested_by: str | None = Field(None, alias="attestedBy")
    attested_at: AwareDatetime | None = Field(None, alias="attestedAt")
    note: str | None = None


class Clip(BaseModel):
    """
    A rendered artefact at clips/{clipId}. Only clips and thumbnails are ever uploaded (decision D3).
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    id: str = Field(..., min_length=1)
    uid: str = Field(..., min_length=1)
    candidate_id: str = Field(..., alias="candidateId", min_length=1)
    source_id: str | None = Field(None, alias="sourceId")
    job_id: str | None = Field(None, alias="jobId")
    storage_path: str | None = Field(None, alias="storagePath")
    thumbnail_path: str | None = Field(None, alias="thumbnailPath")
    duration_sec: float | None = Field(None, alias="durationSec", ge=0.0)
    width_px: int | None = Field(None, alias="widthPx", ge=1)
    height_px: int | None = Field(None, alias="heightPx", ge=1)
    size_bytes: int | None = Field(None, alias="sizeBytes", ge=0)
    render_profile: str | None = Field(None, alias="renderProfile")
    title: str | None = None
    description: str | None = None
    review: ReviewState
    reviewed_at: AwareDatetime | None = Field(None, alias="reviewedAt")
    rights: RightsAttestation | None = None
    created_at: AwareDatetime = Field(..., alias="createdAt")


class WorkerStatus(StrEnum):
    ONLINE = "ONLINE"
    BUSY = "BUSY"
    OFFLINE = "OFFLINE"


class WorkerCapabilities(BaseModel):
    """
    Advertised so the PWA can explain why a job is not progressing, rather than leaving it silently queued.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    whisper: bool
    llm: bool
    render: bool
    publish: bool


class GpuInfo(BaseModel):
    """
    vramFreeMb is measured through NVML rather than tracked internally, because a foreign process (an interactive Ollama session, say) can hold VRAM the pipeline needs. Observed in Phase 0.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    name: str
    vram_total_mb: int = Field(..., alias="vramTotalMb", ge=0)
    vram_free_mb: int = Field(..., alias="vramFreeMb", ge=0)


class WorkerHeartbeat(BaseModel):
    """
    Liveness and capability advertisement at workers/{workerId}.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    worker_id: str = Field(..., alias="workerId", min_length=1)
    uid: str = Field(..., min_length=1)
    status: WorkerStatus
    capabilities: WorkerCapabilities
    gpu: GpuInfo | None = None
    version: str
    hostname: str | None = None
    active_job_ids: list[str] | None = Field([], alias="activeJobIds")
    last_seen_at: AwareDatetime = Field(..., alias="lastSeenAt")
    started_at: AwareDatetime | None = Field(None, alias="startedAt")


class LlmClipProposal(BaseModel):
    """
    One window as the model returns it. Deliberately carries no total and no precise boundaries: the model supplies judgement, Python supplies arithmetic (D5) and boundary precision (D4).
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    start_sec: float = Field(..., alias="startSec", ge=0.0)
    end_sec: float = Field(..., alias="endSec", ge=0.0)
    sub_scores: SubScores = Field(..., alias="subScores")
    hook: str
    reason: str


class LlmClipResponse(BaseModel):
    """
    The schema-constrained response from the local model for one transcript window. Passed to Ollama as a format constraint so malformed output is rejected by the runtime rather than parsed defensively here.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    clips: list[LlmClipProposal]


class ClipForgeContracts(BaseModel):
    """
    The complete ClipForge wire protocol. The PWA and the worker are two independent implementations of the types defined here; both are generated from this file, so neither can drift from it. See docs/adr/0005-single-source-contracts.md. Every timestamp is an ISO 8601 date-time string, NOT a Firestore Timestamp: the store adapters convert at the boundary, which keeps this document language-neutral and lets the unit tier compare documents as plain JSON with no emulator running.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    job: Job | None = None
    job_event: JobEvent | None = Field(None, alias="jobEvent")
    stage: Stage | None = None
    source: Source | None = None
    transcript: Transcript | None = None
    candidate: Candidate | None = None
    clip: Clip | None = None
    worker_heartbeat: WorkerHeartbeat | None = Field(None, alias="workerHeartbeat")
    llm_clip_response: LlmClipResponse | None = Field(None, alias="llmClipResponse")
