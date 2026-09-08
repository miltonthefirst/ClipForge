/**
 * ClipForge contracts — GENERATED FILE, DO NOT EDIT.
 *
 * Source of truth: packages/contracts/schemas/clipforge.json
 * Regenerate with: npm --prefix packages/contracts run generate
 *
 * Editing this file by hand is pointless: CI regenerates it and fails on any
 * difference. Change the schema instead.
 */

/**
 * ECHO is a no-op job of three artificial stages used to exercise the scheduler without touching media. CLIP is the real pipeline.
 */
export type JobType = 'ECHO' | 'CLIP';
/**
 * Lifecycle of a job. Transitions are defined in docs/PLAN.md 3.3 and enforced by clipforge.scheduler.lease.
 */
export type JobStatus = 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'CANCELLED';
/**
 * Ordered pipeline steps. ECHO_* belong to the ECHO job type only.
 */
export type StageName =
  | 'ECHO_ONE'
  | 'ECHO_TWO'
  | 'ECHO_THREE'
  | 'DOWNLOAD'
  | 'TRANSCRIBE'
  | 'ANALYZE'
  | 'RENDER'
  | 'PUBLISH';
/**
 * Which scheduler lane a stage runs in. The GPU lane is depth 1 because Whisper and the LLM cannot be co-resident in 6 GB of VRAM (docs/PLAN.md 2.1).
 */
export type Lane = 'GPU' | 'CPU';
/**
 * A stage that is DONE is never re-executed on retry. That is what makes a crash during ANALYZE cost seconds rather than the twenty minutes of DOWNLOAD and TRANSCRIBE that preceded it.
 */
export type StageStatus = 'PENDING' | 'RUNNING' | 'DONE' | 'SKIPPED' | 'FAILED';
export type JobEventKind =
  | 'CREATED'
  | 'CLAIMED'
  | 'STAGE_STARTED'
  | 'STAGE_COMPLETED'
  | 'STAGE_FAILED'
  | 'COMPLETED'
  | 'FAILED'
  | 'CANCELLED'
  | 'LEASE_EXPIRED'
  | 'REQUEUED';
/**
 * local exists so the whole pipeline can be exercised from a committed fixture with no network, which is what keeps the integration tier runnable in CI.
 */
export type SourceProvider = 'youtube' | 'local';
/**
 * Why an ingest failed, in terms a user can act on. Phase 3 requires each of these to map to a distinct user-facing message rather than a stack trace: 'this video is age-restricted' is actionable, 'DownloadError' is not. Only RATE_LIMITED and NETWORK are worth retrying.
 */
export type IngestErrorCode =
  | 'UNSUPPORTED_URL'
  | 'NOT_FOUND'
  | 'PRIVATE'
  | 'GEO_BLOCKED'
  | 'AGE_RESTRICTED'
  | 'LIVE_STREAM'
  | 'TOO_LONG'
  | 'NO_SUITABLE_FORMAT'
  | 'RATE_LIMITED'
  | 'NETWORK'
  | 'DISK_FULL'
  | 'UNKNOWN';
/**
 * Where the authoritative copy of a rendered clip lives. LOCAL is the free-tier default: Cloud Storage for Firebase requires the Blaze plan, so on Spark there is no bucket at all and clips stay on the worker. REMOTE means a copy exists that any browser can fetch. See docs/adr/0009-spark-tier-local-artefacts.md.
 */
export type ClipLocation = 'LOCAL' | 'REMOTE';
export type ReviewState = 'PENDING' | 'APPROVED' | 'REJECTED';
/**
 * Why the user believes they may publish this. Publishing is gated on an explicit attestation; see docs/PLAN.md 7.
 */
export type RightsBasis =
  'OWN_CONTENT' | 'LICENSED' | 'PERMISSION_GRANTED' | 'FAIR_USE_CLAIMED' | 'UNVERIFIED';
export type WorkerStatus = 'ONLINE' | 'BUSY' | 'OFFLINE';

/**
 * The complete ClipForge wire protocol. The PWA and the worker are two independent implementations of the types defined here; both are generated from this file, so neither can drift from it. See docs/adr/0005-single-source-contracts.md. Every timestamp is an ISO 8601 date-time string, NOT a Firestore Timestamp: the store adapters convert at the boundary, which keeps this document language-neutral and lets the unit tier compare documents as plain JSON with no emulator running.
 */
export interface ClipForgeContracts {
  job?: Job;
  jobEvent?: JobEvent;
  stage?: Stage;
  source?: Source;
  ingestErrorCode?: IngestErrorCode;
  transcript?: Transcript;
  transcriptRef?: TranscriptRef;
  candidate?: Candidate;
  clip?: Clip;
  clipPreview?: ClipPreview;
  workerHeartbeat?: WorkerHeartbeat;
  llmClipResponse?: LlmClipResponse;
}
/**
 * One pipeline run over one source. Stored at jobs/{jobId}.
 */
export interface Job {
  id: string;
  /**
   * Owning user. Every security rule keys off this field.
   */
  uid: string;
  type: JobType;
  status: JobStatus;
  /**
   * Set by the DOWNLOAD stage once ingestion resolves the submission to a source. Null until then.
   */
  sourceId?: string | null;
  /**
   * What the user actually submitted: a YouTube URL or a local file path. Kept verbatim and separate from sourceId, because a job must be re-runnable from the original input even if its source document was garbage-collected.
   */
  submission?: string | null;
  /**
   * Ordered. Executed front to back; DONE stages are skipped on retry.
   *
   * @minItems 1
   */
  stages: [Stage, ...Stage[]];
  workerId?: string | null;
  /**
   * Set on claim, extended by heartbeat. A RUNNING job whose lease is in the past is reclaimable by the reaper.
   */
  leaseExpiresAt?: string | null;
  attempts: number;
  maxAttempts: number;
  /**
   * The failure that terminated the job, copied from the failing stage.
   */
  error?: StageError | null;
  createdAt: string;
  updatedAt: string;
  startedAt?: string | null;
  endedAt?: string | null;
}
/**
 * One idempotent, checkpointed step of a job.
 */
export interface Stage {
  name: StageName;
  lane: Lane;
  status: StageStatus;
  startedAt?: string | null;
  endedAt?: string | null;
  durationMs?: number | null;
  /**
   * Peak VRAM observed through NVML while this stage held the ModelBroker lease. Null for CPU stages.
   */
  peakVramMb?: number | null;
  attempts?: number;
  /**
   * Opaque, stage-owned resume state. The scheduler persists and returns it verbatim and never interprets it.
   */
  checkpoint?: {
    [k: string]: unknown;
  } | null;
  error?: StageError | null;
}
/**
 * Structured failure detail. Captured rather than raised so a failed stage can be inspected from the PWA without reading worker logs.
 */
export interface StageError {
  /**
   * Exception class name.
   */
  type: string;
  message: string;
  /**
   * A stable, machine-readable cause the PWA can map to a written explanation — see IngestErrorCode for the ingest stage's vocabulary. Null when a failure has no classified cause, in which case the UI falls back to `message`. Typed as a string rather than an enum so each stage can own its own vocabulary without every stage's codes leaking into every error.
   */
  code?: string | null;
  traceback?: string | null;
  /**
   * False marks a failure that will never succeed on retry (bad input, a rights refusal), so the scheduler fails the job immediately instead of burning its remaining attempts.
   */
  retryable?: boolean;
}
/**
 * Append-only transition log at jobs/{jobId}/events/{eventId}. Never mutated, which is what makes it trustworthy when debugging a job that failed hours ago.
 */
export interface JobEvent {
  id: string;
  jobId: string;
  kind: JobEventKind;
  at: string;
  /**
   * Order within a single transition. One transition can emit several events at the identical instant — a reap emits LEASE_EXPIRED and REQUEUED together — and ordering by timestamp alone would then fall back to document id, which is random. The log is ordered by (at, seq).
   */
  seq: number;
  stage?: StageName | null;
  workerId?: string | null;
  detail?: string | null;
  attempts?: number | null;
}
/**
 * One ingested long-form video at sources/{sourceId}. The media itself never leaves the worker (docs/PLAN.md decision D3).
 */
export interface Source {
  id: string;
  uid: string;
  provider: SourceProvider;
  /**
   * YouTube video id, or null for a local file.
   */
  externalId?: string | null;
  url?: string | null;
  title?: string | null;
  channel?: string | null;
  durationSec?: number | null;
  /**
   * Hash of the downloaded media. Transcripts are cached against this, so re-ingesting the same video costs no GPU time.
   */
  contentHash?: string | null;
  /**
   * Worker-local absolute path. Advisory only for the PWA, which can never read it.
   */
  localPath?: string | null;
  sizeBytes?: number | null;
  /**
   * Exempt from workspace garbage collection. Sources are large and the disk is finite, so GC is not optional — pinning is the escape hatch for one you are still working with.
   */
  pinned?: boolean;
  /**
   * Drives least-recently-used eviction. Touched whenever a stage reads the file, not when the document is read.
   */
  lastAccessedAt?: string | null;
  createdAt: string;
}
/**
 * Stored at sources/{sourceId}/transcripts/{modelVersion}, keyed by model so re-transcribing with a better model does not destroy the old one.
 */
export interface Transcript {
  sourceId: string;
  /**
   * For example faster-whisper:large-v3-turbo:int8_float16
   */
  modelVersion: string;
  language?: string | null;
  durationSec?: number | null;
  segments: TranscriptSegment[];
  createdAt: string;
}
export interface TranscriptSegment {
  index: number;
  text: string;
  startSec: number;
  endSec: number;
  words?: TranscriptWord[];
}
/**
 * Word-level timing. Load-bearing for boundary snapping (D4) and caption generation, which is why faster-whisper is configured with word timestamps enabled.
 */
export interface TranscriptWord {
  text: string;
  startSec: number;
  endSec: number;
  probability?: number | null;
}
/**
 * What Firestore keeps about a transcript, at sources/{sourceId}/transcripts/{modelVersion}. The word-level segments themselves live on the worker: a 60-minute word-level transcript approaches Firestore's 1 MiB document limit, and the PWA never needs one whole — Candidate.transcriptExcerpt carries the part a reviewer reads.
 */
export interface TranscriptRef {
  sourceId: string;
  modelVersion: string;
  /**
   * Worker-local path to the full Transcript document, as JSON.
   */
  localPath: string;
  /**
   * Worker-local path to the voice-activity map produced alongside the transcript: the speech spans, whose gaps are the silences. Phase 5 snaps clip boundaries to those gaps, so it is worth persisting rather than recomputing. Its contents are deliberately NOT modelled here — it never crosses to the PWA, and this document is the wire protocol.
   */
  vadPath?: string | null;
  language?: string | null;
  durationSec?: number | null;
  segmentCount?: number | null;
  wordCount?: number | null;
  createdAt: string;
}
/**
 * An LLM-proposed clip window at candidates/{candidateId}, after deterministic boundary snapping.
 */
export interface Candidate {
  id: string;
  uid: string;
  sourceId: string;
  jobId?: string | null;
  /**
   * Snapped to a silence boundary and a sentence end, not the raw LLM value.
   */
  startSec: number;
  endSec: number;
  /**
   * The model's raw suggestion, retained so snapping can be evaluated against it.
   */
  proposedStartSec?: number | null;
  proposedEndSec?: number | null;
  subScores: SubScores;
  /**
   * Computed in Python from subScores. Never supplied by the model.
   */
  total: number;
  hook?: string | null;
  reason?: string | null;
  transcriptExcerpt?: string | null;
  modelVersion?: string | null;
  promptVersion?: string | null;
  createdAt: string;
}
/**
 * The rubric from docs/PLAN.md. The LLM returns these; Python computes the weighted total (decision D5), so the weighting can be changed and every historical candidate rescored without re-running inference.
 */
export interface SubScores {
  hook: number;
  curiosity: number;
  standalone: number;
  emotion: number;
  pacing: number;
  shareability: number;
}
/**
 * A rendered artefact at clips/{clipId}. The FILE itself never enters Firestore. On the free tier it stays on the worker and is reachable through the worker's local file server; when Blaze is available the same document also carries a playbackUrl. Both states are first-class here on purpose, so enabling Blaze populates a field rather than migrating a model.
 */
export interface Clip {
  id: string;
  uid: string;
  candidateId: string;
  sourceId?: string | null;
  jobId?: string | null;
  location: ClipLocation;
  /**
   * Absolute path on the worker. Always set, even once a remote copy exists — the worker still needs it to publish (decision D7).
   */
  localPath: string;
  /**
   * A URL any browser can fetch. Null on the free tier. Populated by the Cloud Storage adapter, and equally by a tunnel — this field is 'a URL', not 'a Firebase thing'.
   */
  playbackUrl?: string | null;
  /**
   * Object path within the bucket, when one exists. Kept alongside playbackUrl because deletion and rules key off the path, not the URL.
   */
  storagePath?: string | null;
  thumbnailPath?: string | null;
  durationSec?: number | null;
  widthPx?: number | null;
  heightPx?: number | null;
  sizeBytes?: number | null;
  renderProfile?: string | null;
  title?: string | null;
  description?: string | null;
  review: ReviewState;
  reviewedAt?: string | null;
  rights?: RightsAttestation | null;
  createdAt: string;
}
export interface RightsAttestation {
  basis: RightsBasis;
  attestedBy?: string | null;
  attestedAt?: string | null;
  note?: string | null;
}
/**
 * What a phone can actually see when the clip file is not reachable. Stored at clips/{clipId}/preview/poster as base64 — a subcollection document, so the review-queue query does not drag image bytes on every read. Sized to stay well inside Firestore's 1 MiB document limit; at ~40-60 KB the 1 GiB free tier holds roughly 20,000 of these.
 */
export interface ClipPreview {
  clipId: string;
  /**
   * A single representative frame, JPEG, base64-encoded without a data: prefix.
   */
  posterBase64: string;
  /**
   * Four frames tiled into one JPEG, so a reviewer gets a sense of motion rather than a single still.
   */
  filmstripBase64?: string | null;
  widthPx: number;
  heightPx: number;
  byteSize?: number | null;
  createdAt: string;
}
/**
 * Liveness and capability advertisement at workers/{workerId}.
 */
export interface WorkerHeartbeat {
  workerId: string;
  uid: string;
  status: WorkerStatus;
  capabilities: WorkerCapabilities;
  gpu?: GpuInfo | null;
  version: string;
  hostname?: string | null;
  activeJobIds?: string[];
  lastSeenAt: string;
  startedAt?: string | null;
}
/**
 * Advertised so the PWA can explain why a job is not progressing, rather than leaving it silently queued.
 */
export interface WorkerCapabilities {
  whisper: boolean;
  llm: boolean;
  render: boolean;
  publish: boolean;
}
/**
 * vramFreeMb is measured through NVML rather than tracked internally, because a foreign process (an interactive Ollama session, say) can hold VRAM the pipeline needs. Observed in Phase 0.
 */
export interface GpuInfo {
  name: string;
  vramTotalMb: number;
  vramFreeMb: number;
}
/**
 * The schema-constrained response from the local model for one transcript window. Passed to Ollama as a format constraint so malformed output is rejected by the runtime rather than parsed defensively here.
 */
export interface LlmClipResponse {
  clips: LlmClipProposal[];
}
/**
 * One window as the model returns it. Deliberately carries no total and no precise boundaries: the model supplies judgement, Python supplies arithmetic (D5) and boundary precision (D4).
 */
export interface LlmClipProposal {
  startSec: number;
  endSec: number;
  subScores: SubScores;
  hook: string;
  reason: string;
}
