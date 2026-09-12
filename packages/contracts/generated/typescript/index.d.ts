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
 * ECHO is a no-op job of three artificial stages used to exercise the scheduler without touching media. CLIP is the real pipeline. PUBLISH is a separate, single-stage job created after a human approves a clip — publishing cannot be a stage of CLIP because it happens on the far side of a human decision that may take days. MUSIC is the same shape for the same reason: scoring a finished clip is a choice someone makes while watching it, and it produces a new clip rather than altering the one they watched. UPLOAD is how a reviewer on a phone asks for a clip that only exists on the worker's disk: the phone cannot reach the worker, so the request travels as a job like everything else.
 */
export type JobType = 'ECHO' | 'CLIP' | 'PUBLISH' | 'MUSIC' | 'UPLOAD';
/**
 * Lifecycle of a job. Transitions are defined in docs/PLAN.md 3.3 and enforced by clipforge.scheduler.lease.
 */
export type JobStatus = 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'CANCELLED';
/**
 * What the added track does to the clip's own audio. BED keeps the original and sits the music under it, ducking automatically so speech stays intelligible — the usual choice for a talking clip. REPLACE removes the original audio entirely and the track becomes the whole soundtrack, which is what a montage or a silent-action clip wants.
 */
export type MusicMode = 'BED' | 'REPLACE';
/**
 * Captions are burned into the clip's pixels by the RENDER stage, so they cannot be peeled off a finished file — removing them means re-rendering the segment from the original source without the subtitle filter. KEEP is therefore cheap and always available; REMOVE needs the source media to still be on the worker, and fails clearly when it has been garbage-collected. Offered as a choice rather than inferred from the mode because 'music over captions' is a real style, and guessing wrong either way is worse than asking.
 */
export type MusicCaptions = 'KEEP' | 'REMOVE';
/**
 * Why the operator believes they may publish this clip. Publishing is disabled by default and no clip can be published without one of these recorded, together with who attested it and when. A null `rights` block means no attestation exists — which is a different thing from a weak one, and the gate refuses it. See docs/PLAN.md Phase 8.
 */
export type RightsBasis =
  'OWN_CONTENT' | 'LICENSED' | 'PERMISSION_GRANTED' | 'FAIR_USE_ASSERTED' | 'PUBLIC_DOMAIN';
/**
 * Defaults to unlisted. Publishing something to the world by accident is not recoverable in the way an unlisted upload is.
 */
export type PublishPrivacy = 'private' | 'unlisted' | 'public';
/**
 * Ordered pipeline steps. ECHO_* belong to the ECHO job type only. UPLOAD is the single stage of an UPLOAD job: it copies a clip that already exists on the worker into the bucket so a phone can play it.
 */
export type StageName =
  | 'ECHO_ONE'
  | 'ECHO_TWO'
  | 'ECHO_THREE'
  | 'DOWNLOAD'
  | 'TRANSCRIBE'
  | 'ANALYZE'
  | 'RENDER'
  | 'PUBLISH'
  | 'MUSIC'
  | 'UPLOAD';
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
 * Where a clip was published. TikTok and Instagram are out of scope until v0.3+ — both need app review with materially harder approval paths.
 */
export type PublishPlatform = 'YOUTUBE';
/**
 * Lifecycle of one attempt to publish one clip to one platform. PENDING is written BEFORE the upload begins, which is what makes a retry reconcile against the platform instead of double-posting.
 */
export type PublicationState = 'PENDING' | 'UPLOADING' | 'PUBLISHED' | 'FAILED' | 'CANCELLED';
/**
 * Whether the worker can currently publish to this channel. NOT_CONFIGURED means no OAuth client has been supplied; NEEDS_AUTH means one has but nobody has authorised it, or the refresh token expired — which it does every 7 days while the consent screen is in Testing mode.
 */
export type ChannelConnection = 'NOT_CONFIGURED' | 'NEEDS_AUTH' | 'CONNECTED' | 'ERROR';
/**
 * ADMIN can approve other users and change roles. MEMBER can use the app for their own data and nothing else. There is no third level because there is no third thing to protect.
 */
export type UserRole = 'ADMIN' | 'MEMBER';
/**
 * Registration is open; access is not. A new account lands in PENDING and can read nothing until an admin approves it, so an unapproved sign-up is an inert row rather than a foothold. REJECTED and DISABLED are kept apart deliberately: one was never let in, the other was and had it taken away, and an audit that cannot tell them apart is not much of an audit.
 */
export type UserStatus = 'PENDING' | 'APPROVED' | 'REJECTED' | 'DISABLED';
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
  publication?: Publication;
  channel?: Channel;
  userProfile?: UserProfile;
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
   * The clip a PUBLISH or MUSIC job acts on. Null for every other job type. Security rules read this to check the clip's rights attestation before allowing the job to be created at all.
   */
  clipId?: string | null;
  /**
   * What a MUSIC job should add, and how. Null for every other job type.
   */
  musicOptions?: MusicOptions | null;
  /**
   * Set by the client on a PUBLISH job. Null for every other job type, and null here means 'use the channel defaults'.
   */
  publishOptions?: PublishOptions | null;
  /**
   * The job is not claimable until this instant. Null means claimable immediately. This is how a publish-at time is honoured: scheduling lives in the one predicate every claim path already consults, rather than in a second scheduler that could disagree with the first.
   */
  notBefore?: string | null;
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
 * What to add to a clip, and how. Carried on the MUSIC job that produces the scored version.
 */
export interface MusicOptions {
  /**
   * A YouTube URL, or a path to an audio file on the worker. Only the audio is ever fetched from a URL — the video of a music source is of no use here and would cost bandwidth for nothing.
   */
  source: string;
  mode: MusicMode;
  captions: MusicCaptions;
  /**
   * Trim on the music, relative to the level the stage picks. Null means 'use the stage's judgement', which targets a bed roughly 18 LUFS below speech and a replacement at the clip's own loudness target.
   */
  gainDb?: number | null;
  /**
   * Whether to start the music excerpt exactly on a beat, so its pulse lands with the clip's first frame. The music moves to meet the clip, never the other way round: re-cutting the video to fall on a beat would mean the published clip differed from the one that was reviewed, and would force a re-encode to achieve something the listener hears identically either way. Ignored when the track has no tempo clear enough to measure.
   */
  alignToBeat?: boolean;
  rights: RightsAttestation;
}
/**
 * Why this track may be used. The same gate the video passes, applied to the music, because a Content ID claim does not care which half of the file it came from. It does not make a claim less likely — it records who decided the track was usable, which is the question that matters afterwards.
 */
export interface RightsAttestation {
  basis: RightsBasis;
  attestedBy?: string | null;
  attestedAt?: string | null;
  note?: string | null;
}
/**
 * What the operator chose for one specific upload, set when the publish is requested. Absent fields fall back to the channel's defaults, so a clip published without opening any of this still behaves sensibly.
 */
export interface PublishOptions {
  channelId?: string | null;
  /**
   * Replaces the clip's title for this upload only. Capped at YouTube's own limit here rather than truncated silently on the way out, so what the operator typed is what the audit record shows.
   */
  title?: string | null;
  /**
   * Replaces the clip's description for this upload only.
   */
  description?: string | null;
  privacy?: PublishPrivacy | null;
  categoryId?: string | null;
  /**
   * Null and empty are different answers here, and both are ones an operator can mean. Null is 'I did not touch the tags', which falls through to the channel's; an empty list is 'no tags on this one', which does not. Every other field in this block is nullable for the same reason, and an array whose absent value was [] could not express the second.
   */
  tags?: string[] | null;
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
  /**
   * When the bucket copy stops being playable. Written at upload as upload time plus the retention window, and honoured by the UI without asking the bucket: the object is removed by a Cloud Storage lifecycle rule, which reports to nobody, so a clip whose expiry has passed is treated as local-only rather than discovered to be missing when someone presses play. Null when there is no bucket copy.
   */
  playbackExpiresAt?: string | null;
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
  /**
   * The clip this one was made from, when it is a scored version of another. The original is never altered — a MUSIC job produces a new clip — so this is what relates the two, and what lets the review queue say 'music version of' rather than showing two unexplained near-duplicates.
   */
  derivedFromClipId?: string | null;
  /**
   * What was added to this clip, when something was. Null on an ordinary render.
   */
  music?: AppliedMusic | null;
  /**
   * What the reviewer thought, in their own words. Distinct from `description`, which is copy that may be published: this is never uploaded anywhere and exists to answer 'why did I reject this?' three weeks later. Phase 9 calibrates the rubric against realised performance; a human's stated reason is the other half of that evidence and is worth capturing while it is fresh.
   */
  reviewNote?: string | null;
  rights?: RightsAttestation1 | null;
  createdAt: string;
}
/**
 * What was actually done to a scored clip, recorded on the clip itself. Provenance rather than configuration: it answers 'what is this version, and where did the track come from' months later, when the job that made it is long gone.
 */
export interface AppliedMusic {
  mode: MusicMode;
  captions: MusicCaptions;
  /**
   * The URL or path the track came from, verbatim.
   */
  source: string;
  /**
   * What the source called itself, when it said. Worth keeping for the description and for answering a claim.
   */
  trackTitle?: string | null;
  /**
   * The tempo the analysis settled on. Null when the track had no beat clear enough to measure, in which case alignment was skipped rather than guessed.
   */
  tempoBpm?: number | null;
  /**
   * Where in the track the excerpt begins. Rarely 0: a track's first bars are usually its least interesting, so the stage picks a section by energy and starts it on a downbeat.
   */
  musicStartSec?: number | null;
  rights?: RightsAttestation1 | null;
}
export interface RightsAttestation1 {
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
 * One publish attempt, at clips/{clipId}/publications/{pubId}. Doubles as the audit log: it records who attested the rights basis and on what grounds, so 'who authorised this and why' is answerable for any published clip without reading worker logs.
 */
export interface Publication {
  id: string;
  clipId: string;
  uid: string;
  platform: PublishPlatform;
  state: PublicationState;
  /**
   * The platform's id for the uploaded video. Null until the upload completes.
   */
  externalId?: string | null;
  externalUrl?: string | null;
  /**
   * Which channel this went to. Recorded on the attempt because a clip published today and re-published elsewhere next month must not look like it went to the same place.
   */
  channelId?: string | null;
  privacy?: PublishPrivacy | null;
  title?: string | null;
  description?: string | null;
  tags?: string[];
  /**
   * Recorded because it is part of what went out. The resolved value, not the request's — this record is the answer to 'what did we actually send', and a field that only sometimes reflects the upload answers nothing.
   */
  categoryId?: string | null;
  /**
   * Copied from the clip at publish time rather than referenced. The attestation that justified THIS upload must survive a later edit to the clip, or the audit trail records the wrong reason.
   */
  rights?: RightsAttestation1 | null;
  attempts?: number;
  /**
   * What this attempt cost. A YouTube upload is 1,600 of a 10,000 daily allowance — about six a day — so the budget is tracked rather than discovered on the seventh failure.
   */
  quotaUnits?: number | null;
  error?: StageError | null;
  /**
   * Scheduled publish time. The worker holds the clip until then.
   */
  publishAt?: string | null;
  createdAt: string;
  publishedAt?: string | null;
}
/**
 * A publishing destination, at channels/{channelId}. Modelled as a collection from the outset even though the UI manages one: adding a second channel is then a document and a second `youtube-auth`, not a schema migration and a rewrite of every publication record. Deliberately holds NO credentials — the client secret and refresh token live on the worker (docs/adr/0010-worker-held-publishing-credentials.md).
 */
export interface Channel {
  id: string;
  uid: string;
  platform: PublishPlatform;
  /**
   * What the operator calls it. Free text, because 'the cooking one' is more useful at 6am than a channel id.
   */
  label: string;
  isDefault?: boolean;
  externalChannelId?: string | null;
  /**
   * Read back from YouTube after authorising, so the app can show which account was actually connected rather than which one was intended.
   */
  externalChannelTitle?: string | null;
  connection: ChannelConnection;
  /**
   * Why the connection is not CONNECTED, in words the operator can act on.
   */
  connectionMessage?: string | null;
  authorisedAt?: string | null;
  checkedAt?: string | null;
  defaults: PublishDefaults;
  quotaDay?: string | null;
  quotaUsedUnits?: number | null;
  uploadsRemainingToday?: number | null;
  createdAt: string;
  updatedAt?: string | null;
}
/**
 * What a publish uses when the clip does not say otherwise. Editable from the app because none of it is secret — unlike the credentials, which never leave the worker.
 */
export interface PublishDefaults {
  privacy: PublishPrivacy;
  /**
   * YouTube category id. 22 is People & Blogs, which is the safe default for talking-head clips.
   */
  categoryId?: string;
  tags?: string[];
  /**
   * Appended to every title, for a channel handle or series marker. Truncation still applies: YouTube rejects titles over 100 characters.
   */
  titleSuffix?: string | null;
  /**
   * Appended to every description. Where a standing credit, licence note or link block belongs.
   */
  descriptionTemplate?: string | null;
}
/**
 * A person with an account, at users/{uid}. The document id IS the Firebase Auth uid, which is what lets security rules resolve a caller's status with one get() and no join.
 */
export interface UserProfile {
  uid: string;
  email: string;
  displayName?: string | null;
  photoUrl?: string | null;
  role: UserRole;
  status: UserStatus;
  createdAt: string;
  /**
   * When an admin last approved, rejected or disabled this account.
   */
  decidedAt?: string | null;
  /**
   * The uid of the admin who made that decision. Recorded so 'who let this person in?' is answerable later.
   */
  decidedBy?: string | null;
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
