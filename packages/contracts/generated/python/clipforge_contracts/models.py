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
    ECHO is a no-op job of three artificial stages used to exercise the scheduler without touching media. CLIP is the real pipeline. PUBLISH is a separate, single-stage job created after a human approves a clip — publishing cannot be a stage of CLIP because it happens on the far side of a human decision that may take days. MUSIC is the same shape for the same reason: scoring a finished clip is a choice someone makes while watching it, and it produces a new clip rather than altering the one they watched.
    """

    ECHO = "ECHO"
    CLIP = "CLIP"
    PUBLISH = "PUBLISH"
    MUSIC = "MUSIC"


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
    MUSIC = "MUSIC"


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
    code: str | None = None
    """
    A stable, machine-readable cause the PWA can map to a written explanation — see IngestErrorCode for the ingest stage's vocabulary. Null when a failure has no classified cause, in which case the UI falls back to `message`. Typed as a string rather than an enum so each stage can own its own vocabulary without every stage's codes leaking into every error.
    """
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


class MusicMode(StrEnum):
    """
    What the added track does to the clip's own audio. BED keeps the original and sits the music under it, ducking automatically so speech stays intelligible — the usual choice for a talking clip. REPLACE removes the original audio entirely and the track becomes the whole soundtrack, which is what a montage or a silent-action clip wants.
    """

    BED = "BED"
    REPLACE = "REPLACE"


class MusicCaptions(StrEnum):
    """
    Captions are burned into the clip's pixels by the RENDER stage, so they cannot be peeled off a finished file — removing them means re-rendering the segment from the original source without the subtitle filter. KEEP is therefore cheap and always available; REMOVE needs the source media to still be on the worker, and fails clearly when it has been garbage-collected. Offered as a choice rather than inferred from the mode because 'music over captions' is a real style, and guessing wrong either way is worse than asking.
    """

    KEEP = "KEEP"
    REMOVE = "REMOVE"


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
    seq: int = Field(..., ge=0)
    """
    Order within a single transition. One transition can emit several events at the identical instant — a reap emits LEASE_EXPIRED and REQUEUED together — and ordering by timestamp alone would then fall back to document id, which is random. The log is ordered by (at, seq).
    """
    stage: StageName | None = None
    worker_id: str | None = Field(None, alias="workerId")
    detail: str | None = None
    attempts: int | None = Field(None, ge=0)


class IngestErrorCode(StrEnum):
    """
    Why an ingest failed, in terms a user can act on. Phase 3 requires each of these to map to a distinct user-facing message rather than a stack trace: 'this video is age-restricted' is actionable, 'DownloadError' is not. Only RATE_LIMITED and NETWORK are worth retrying.
    """

    UNSUPPORTED_URL = "UNSUPPORTED_URL"
    NOT_FOUND = "NOT_FOUND"
    PRIVATE = "PRIVATE"
    GEO_BLOCKED = "GEO_BLOCKED"
    AGE_RESTRICTED = "AGE_RESTRICTED"
    LIVE_STREAM = "LIVE_STREAM"
    TOO_LONG = "TOO_LONG"
    NO_SUITABLE_FORMAT = "NO_SUITABLE_FORMAT"
    RATE_LIMITED = "RATE_LIMITED"
    NETWORK = "NETWORK"
    DISK_FULL = "DISK_FULL"
    UNKNOWN = "UNKNOWN"


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
    size_bytes: int | None = Field(None, alias="sizeBytes", ge=0)
    pinned: bool | None = False
    """
    Exempt from workspace garbage collection. Sources are large and the disk is finite, so GC is not optional — pinning is the escape hatch for one you are still working with.
    """
    last_accessed_at: AwareDatetime | None = Field(None, alias="lastAccessedAt")
    """
    Drives least-recently-used eviction. Touched whenever a stage reads the file, not when the document is read.
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
    Why the operator believes they may publish this clip. Publishing is disabled by default and no clip can be published without one of these recorded, together with who attested it and when. A null `rights` block means no attestation exists — which is a different thing from a weak one, and the gate refuses it. See docs/PLAN.md Phase 8.
    """

    OWN_CONTENT = "OWN_CONTENT"
    LICENSED = "LICENSED"
    PERMISSION_GRANTED = "PERMISSION_GRANTED"
    FAIR_USE_ASSERTED = "FAIR_USE_ASSERTED"
    PUBLIC_DOMAIN = "PUBLIC_DOMAIN"


class ChannelConnection(StrEnum):
    """
    Whether the worker can currently publish to this channel. NOT_CONFIGURED means no OAuth client has been supplied; NEEDS_AUTH means one has but nobody has authorised it, or the refresh token expired — which it does every 7 days while the consent screen is in Testing mode.
    """

    NOT_CONFIGURED = "NOT_CONFIGURED"
    NEEDS_AUTH = "NEEDS_AUTH"
    CONNECTED = "CONNECTED"
    ERROR = "ERROR"


class UserRole(StrEnum):
    """
    ADMIN can approve other users and change roles. MEMBER can use the app for their own data and nothing else. There is no third level because there is no third thing to protect.
    """

    ADMIN = "ADMIN"
    MEMBER = "MEMBER"


class UserStatus(StrEnum):
    """
    Registration is open; access is not. A new account lands in PENDING and can read nothing until an admin approves it, so an unapproved sign-up is an inert row rather than a foothold. REJECTED and DISABLED are kept apart deliberately: one was never let in, the other was and had it taken away, and an audit that cannot tell them apart is not much of an audit.
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DISABLED = "DISABLED"


class UserProfile(BaseModel):
    """
    A person with an account, at users/{uid}. The document id IS the Firebase Auth uid, which is what lets security rules resolve a caller's status with one get() and no join.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    uid: str = Field(..., min_length=1)
    email: str = Field(..., min_length=1)
    display_name: str | None = Field(None, alias="displayName")
    photo_url: str | None = Field(None, alias="photoUrl")
    role: UserRole
    status: UserStatus
    created_at: AwareDatetime = Field(..., alias="createdAt")
    decided_at: AwareDatetime | None = Field(None, alias="decidedAt")
    """
    When an admin last approved, rejected or disabled this account.
    """
    decided_by: str | None = Field(None, alias="decidedBy")
    """
    The uid of the admin who made that decision. Recorded so 'who let this person in?' is answerable later.
    """


class RightsAttestation(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    basis: RightsBasis
    attested_by: str | None = Field(None, alias="attestedBy")
    attested_at: AwareDatetime | None = Field(None, alias="attestedAt")
    note: str | None = None


class ClipLocation(StrEnum):
    """
    Where the authoritative copy of a rendered clip lives. LOCAL is the free-tier default: Cloud Storage for Firebase requires the Blaze plan, so on Spark there is no bucket at all and clips stay on the worker. REMOTE means a copy exists that any browser can fetch. See docs/adr/0009-spark-tier-local-artefacts.md.
    """

    LOCAL = "LOCAL"
    REMOTE = "REMOTE"


class ClipPreview(BaseModel):
    """
    What a phone can actually see when the clip file is not reachable. Stored at clips/{clipId}/preview/poster as base64 — a subcollection document, so the review-queue query does not drag image bytes on every read. Sized to stay well inside Firestore's 1 MiB document limit; at ~40-60 KB the 1 GiB free tier holds roughly 20,000 of these.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    clip_id: str = Field(..., alias="clipId", min_length=1)
    poster_base64: str = Field(..., alias="posterBase64", min_length=1)
    """
    A single representative frame, JPEG, base64-encoded without a data: prefix.
    """
    filmstrip_base64: str | None = Field(None, alias="filmstripBase64")
    """
    Four frames tiled into one JPEG, so a reviewer gets a sense of motion rather than a single still.
    """
    width_px: int = Field(..., alias="widthPx", ge=1)
    height_px: int = Field(..., alias="heightPx", ge=1)
    byte_size: int | None = Field(None, alias="byteSize", ge=0)
    created_at: AwareDatetime = Field(..., alias="createdAt")


class TranscriptRef(BaseModel):
    """
    What Firestore keeps about a transcript, at sources/{sourceId}/transcripts/{modelVersion}. The word-level segments themselves live on the worker: a 60-minute word-level transcript approaches Firestore's 1 MiB document limit, and the PWA never needs one whole — Candidate.transcriptExcerpt carries the part a reviewer reads.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    source_id: str = Field(..., alias="sourceId", min_length=1)
    model_version: str = Field(..., alias="modelVersion", min_length=1)
    local_path: str = Field(..., alias="localPath", min_length=1)
    """
    Worker-local path to the full Transcript document, as JSON.
    """
    vad_path: str | None = Field(None, alias="vadPath")
    """
    Worker-local path to the voice-activity map produced alongside the transcript: the speech spans, whose gaps are the silences. Phase 5 snaps clip boundaries to those gaps, so it is worth persisting rather than recomputing. Its contents are deliberately NOT modelled here — it never crosses to the PWA, and this document is the wire protocol.
    """
    language: str | None = None
    duration_sec: float | None = Field(None, alias="durationSec", ge=0.0)
    segment_count: int | None = Field(None, alias="segmentCount", ge=0)
    word_count: int | None = Field(None, alias="wordCount", ge=0)
    created_at: AwareDatetime = Field(..., alias="createdAt")


class PublicationState(StrEnum):
    """
    Lifecycle of one attempt to publish one clip to one platform. PENDING is written BEFORE the upload begins, which is what makes a retry reconcile against the platform instead of double-posting.
    """

    PENDING = "PENDING"
    UPLOADING = "UPLOADING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PublishPlatform(StrEnum):
    """
    Where a clip was published. TikTok and Instagram are out of scope until v0.3+ — both need app review with materially harder approval paths.
    """

    YOUTUBE = "YOUTUBE"


class PublishPrivacy(StrEnum):
    """
    Defaults to unlisted. Publishing something to the world by accident is not recoverable in the way an unlisted upload is.
    """

    PRIVATE = "private"
    UNLISTED = "unlisted"
    PUBLIC = "public"


class Publication(BaseModel):
    """
    One publish attempt, at clips/{clipId}/publications/{pubId}. Doubles as the audit log: it records who attested the rights basis and on what grounds, so 'who authorised this and why' is answerable for any published clip without reading worker logs.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    id: str = Field(..., min_length=1)
    clip_id: str = Field(..., alias="clipId", min_length=1)
    uid: str = Field(..., min_length=1)
    platform: PublishPlatform
    state: PublicationState
    external_id: str | None = Field(None, alias="externalId")
    """
    The platform's id for the uploaded video. Null until the upload completes.
    """
    external_url: str | None = Field(None, alias="externalUrl")
    channel_id: str | None = Field(None, alias="channelId")
    """
    Which channel this went to. Recorded on the attempt because a clip published today and re-published elsewhere next month must not look like it went to the same place.
    """
    privacy: PublishPrivacy | None = None
    title: str | None = None
    description: str | None = None
    tags: list[str] | None = []
    category_id: str | None = Field(None, alias="categoryId")
    """
    Recorded because it is part of what went out. The resolved value, not the request's — this record is the answer to 'what did we actually send', and a field that only sometimes reflects the upload answers nothing.
    """
    rights: RightsAttestation | None = None
    """
    Copied from the clip at publish time rather than referenced. The attestation that justified THIS upload must survive a later edit to the clip, or the audit trail records the wrong reason.
    """
    attempts: int | None = Field(0, ge=0)
    quota_units: int | None = Field(None, alias="quotaUnits", ge=0)
    """
    What this attempt cost. A YouTube upload is 1,600 of a 10,000 daily allowance — about six a day — so the budget is tracked rather than discovered on the seventh failure.
    """
    error: StageError | None = None
    publish_at: AwareDatetime | None = Field(None, alias="publishAt")
    """
    Scheduled publish time. The worker holds the clip until then.
    """
    created_at: AwareDatetime = Field(..., alias="createdAt")
    published_at: AwareDatetime | None = Field(None, alias="publishedAt")


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


class AppliedMusic(BaseModel):
    """
    What was actually done to a scored clip, recorded on the clip itself. Provenance rather than configuration: it answers 'what is this version, and where did the track come from' months later, when the job that made it is long gone.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    mode: MusicMode
    captions: MusicCaptions
    source: str
    """
    The URL or path the track came from, verbatim.
    """
    track_title: str | None = Field(None, alias="trackTitle")
    """
    What the source called itself, when it said. Worth keeping for the description and for answering a claim.
    """
    tempo_bpm: float | None = Field(None, alias="tempoBpm")
    """
    The tempo the analysis settled on. Null when the track had no beat clear enough to measure, in which case alignment was skipped rather than guessed.
    """
    music_start_sec: float | None = Field(None, alias="musicStartSec")
    """
    Where in the track the excerpt begins. Rarely 0: a track's first bars are usually its least interesting, so the stage picks a section by energy and starts it on a downbeat.
    """
    rights: RightsAttestation | None = None


class MusicOptions(BaseModel):
    """
    What to add to a clip, and how. Carried on the MUSIC job that produces the scored version.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    source: str = Field(..., min_length=1)
    """
    A YouTube URL, or a path to an audio file on the worker. Only the audio is ever fetched from a URL — the video of a music source is of no use here and would cost bandwidth for nothing.
    """
    mode: MusicMode
    captions: MusicCaptions
    gain_db: float | None = Field(None, alias="gainDb", ge=-40.0, le=12.0)
    """
    Trim on the music, relative to the level the stage picks. Null means 'use the stage's judgement', which targets a bed roughly 18 LUFS below speech and a replacement at the clip's own loudness target.
    """
    align_to_beat: bool | None = Field(True, alias="alignToBeat")
    """
    Whether to start the music excerpt exactly on a beat, so its pulse lands with the clip's first frame. The music moves to meet the clip, never the other way round: re-cutting the video to fall on a beat would mean the published clip differed from the one that was reviewed, and would force a re-encode to achieve something the listener hears identically either way. Ignored when the track has no tempo clear enough to measure.
    """
    rights: RightsAttestation
    """
    Why this track may be used. The same gate the video passes, applied to the music, because a Content ID claim does not care which half of the file it came from. It does not make a claim less likely — it records who decided the track was usable, which is the question that matters afterwards.
    """


class PublishDefaults(BaseModel):
    """
    What a publish uses when the clip does not say otherwise. Editable from the app because none of it is secret — unlike the credentials, which never leave the worker.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    privacy: PublishPrivacy
    category_id: str | None = Field("22", alias="categoryId")
    """
    YouTube category id. 22 is People & Blogs, which is the safe default for talking-head clips.
    """
    tags: list[str] | None = []
    title_suffix: str | None = Field(None, alias="titleSuffix")
    """
    Appended to every title, for a channel handle or series marker. Truncation still applies: YouTube rejects titles over 100 characters.
    """
    description_template: str | None = Field(None, alias="descriptionTemplate")
    """
    Appended to every description. Where a standing credit, licence note or link block belongs.
    """


class Channel(BaseModel):
    """
    A publishing destination, at channels/{channelId}. Modelled as a collection from the outset even though the UI manages one: adding a second channel is then a document and a second `youtube-auth`, not a schema migration and a rewrite of every publication record. Deliberately holds NO credentials — the client secret and refresh token live on the worker (docs/adr/0010-worker-held-publishing-credentials.md).
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    id: str = Field(..., min_length=1)
    uid: str = Field(..., min_length=1)
    platform: PublishPlatform
    label: str = Field(..., min_length=1)
    """
    What the operator calls it. Free text, because 'the cooking one' is more useful at 6am than a channel id.
    """
    is_default: bool | None = Field(False, alias="isDefault")
    external_channel_id: str | None = Field(None, alias="externalChannelId")
    external_channel_title: str | None = Field(None, alias="externalChannelTitle")
    """
    Read back from YouTube after authorising, so the app can show which account was actually connected rather than which one was intended.
    """
    connection: ChannelConnection
    connection_message: str | None = Field(None, alias="connectionMessage")
    """
    Why the connection is not CONNECTED, in words the operator can act on.
    """
    authorised_at: AwareDatetime | None = Field(None, alias="authorisedAt")
    checked_at: AwareDatetime | None = Field(None, alias="checkedAt")
    defaults: PublishDefaults
    quota_day: str | None = Field(None, alias="quotaDay")
    quota_used_units: int | None = Field(None, alias="quotaUsedUnits", ge=0)
    uploads_remaining_today: int | None = Field(
        None, alias="uploadsRemainingToday", ge=0
    )
    created_at: AwareDatetime = Field(..., alias="createdAt")
    updated_at: AwareDatetime | None = Field(None, alias="updatedAt")


class PublishOptions(BaseModel):
    """
    What the operator chose for one specific upload, set when the publish is requested. Absent fields fall back to the channel's defaults, so a clip published without opening any of this still behaves sensibly.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    channel_id: str | None = Field(None, alias="channelId")
    title: str | None = Field(None, max_length=100)
    """
    Replaces the clip's title for this upload only. Capped at YouTube's own limit here rather than truncated silently on the way out, so what the operator typed is what the audit record shows.
    """
    description: str | None = Field(None, max_length=4900)
    """
    Replaces the clip's description for this upload only.
    """
    privacy: PublishPrivacy | None = None
    category_id: str | None = Field(None, alias="categoryId")
    tags: list[str] | None = None
    """
    Null and empty are different answers here, and both are ones an operator can mean. Null is 'I did not touch the tags', which falls through to the channel's; an empty list is 'no tags on this one', which does not. Every other field in this block is nullable for the same reason, and an array whose absent value was [] could not express the second.
    """


class Clip(BaseModel):
    """
    A rendered artefact at clips/{clipId}. The FILE itself never enters Firestore. On the free tier it stays on the worker and is reachable through the worker's local file server; when Blaze is available the same document also carries a playbackUrl. Both states are first-class here on purpose, so enabling Blaze populates a field rather than migrating a model.
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
    location: ClipLocation
    local_path: str = Field(..., alias="localPath", min_length=1)
    """
    Absolute path on the worker. Always set, even once a remote copy exists — the worker still needs it to publish (decision D7).
    """
    playback_url: str | None = Field(None, alias="playbackUrl")
    """
    A URL any browser can fetch. Null on the free tier. Populated by the Cloud Storage adapter, and equally by a tunnel — this field is 'a URL', not 'a Firebase thing'.
    """
    storage_path: str | None = Field(None, alias="storagePath")
    """
    Object path within the bucket, when one exists. Kept alongside playbackUrl because deletion and rules key off the path, not the URL.
    """
    playback_expires_at: AwareDatetime | None = Field(None, alias="playbackExpiresAt")
    """
    When the bucket copy stops being playable. Written at upload as upload time plus the retention window, and honoured by the UI without asking the bucket: the object is removed by a Cloud Storage lifecycle rule, which reports to nobody, so a clip whose expiry has passed is treated as local-only rather than discovered to be missing when someone presses play. Null when there is no bucket copy.
    """
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
    derived_from_clip_id: str | None = Field(None, alias="derivedFromClipId")
    """
    The clip this one was made from, when it is a scored version of another. The original is never altered — a MUSIC job produces a new clip — so this is what relates the two, and what lets the review queue say 'music version of' rather than showing two unexplained near-duplicates.
    """
    music: AppliedMusic | None = None
    """
    What was added to this clip, when something was. Null on an ordinary render.
    """
    review_note: str | None = Field(None, alias="reviewNote", max_length=2000)
    """
    What the reviewer thought, in their own words. Distinct from `description`, which is copy that may be published: this is never uploaded anywhere and exists to answer 'why did I reject this?' three weeks later. Phase 9 calibrates the rubric against realised performance; a human's stated reason is the other half of that evidence and is worth capturing while it is fresh.
    """
    rights: RightsAttestation | None = None
    created_at: AwareDatetime = Field(..., alias="createdAt")


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
    """
    Set by the DOWNLOAD stage once ingestion resolves the submission to a source. Null until then.
    """
    submission: str | None = None
    """
    What the user actually submitted: a YouTube URL or a local file path. Kept verbatim and separate from sourceId, because a job must be re-runnable from the original input even if its source document was garbage-collected.
    """
    clip_id: str | None = Field(None, alias="clipId")
    """
    The clip a PUBLISH or MUSIC job acts on. Null for every other job type. Security rules read this to check the clip's rights attestation before allowing the job to be created at all.
    """
    music_options: MusicOptions | None = Field(None, alias="musicOptions")
    """
    What a MUSIC job should add, and how. Null for every other job type.
    """
    publish_options: PublishOptions | None = Field(None, alias="publishOptions")
    """
    Set by the client on a PUBLISH job. Null for every other job type, and null here means 'use the channel defaults'.
    """
    not_before: AwareDatetime | None = Field(None, alias="notBefore")
    """
    The job is not claimable until this instant. Null means claimable immediately. This is how a publish-at time is honoured: scheduling lives in the one predicate every claim path already consults, rather than in a second scheduler that could disagree with the first.
    """
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
    ingest_error_code: IngestErrorCode | None = Field(None, alias="ingestErrorCode")
    transcript: Transcript | None = None
    transcript_ref: TranscriptRef | None = Field(None, alias="transcriptRef")
    candidate: Candidate | None = None
    clip: Clip | None = None
    clip_preview: ClipPreview | None = Field(None, alias="clipPreview")
    publication: Publication | None = None
    channel: Channel | None = None
    user_profile: UserProfile | None = Field(None, alias="userProfile")
    worker_heartbeat: WorkerHeartbeat | None = Field(None, alias="workerHeartbeat")
    llm_clip_response: LlmClipResponse | None = Field(None, alias="llmClipResponse")
