# ClipForge — Execution Plan

> **A local-first AI content agent.** Ingest long-form video, transcribe and analyse it on your own
> hardware, cut high-potential vertical clips, review them from your phone, publish, and learn from
> what actually performed.

**Status:** Phase 0 complete · **Target of record:** v0.1.0 · **Owner:** @miltonthefirst
**Supersedes:** [`initial-plan.md`](../initial-plan.md) (kept for provenance)

---

## 0. How to read this document

This plan is **milestone-driven, not calendar-driven**. There is no deadline; the ordering constraint
is that each phase's exit criteria must be *demonstrably* met before the next begins, because every
later phase assumes the earlier one is load-bearing.

Each phase specifies:

| Field | Meaning |
| --- | --- |
| **Goal** | The single sentence that justifies the phase existing |
| **In scope** | What gets built |
| **Explicitly out of scope** | The scope-creep magnets, named so they can be refused |
| **Deliverables** | Artefacts that land in the repo |
| **Exit criteria** | Objectively checkable. If you can't demo it, the phase isn't done |
| **Risks** | What is most likely to go wrong, and the mitigation |

**Definition of Done (applies to every phase, non-negotiable):**

1. Code merged to `master` via PR, CI green.
2. Unit tests for pure logic; integration tests for anything touching Firebase (emulator-backed).
3. An ADR in [`docs/adr/`](adr/) for any decision that closes off an alternative.
4. `README.md` and `.env.example` updated if the phase changed setup.
5. The phase's demo can be reproduced from a clean clone by following the docs.

Rule 5 is the portfolio rule. A reviewer who clones this repo should be able to get to the same place
you did. If they can't, the phase is not done.

---

## 1. Product thesis

The pitch that makes this repo worth a reviewer's time is **not** "AI cuts videos." It is:

> Cloud AI video pipelines are metered per-minute and per-token. ClipForge moves every expensive
> operation — transcription, semantic analysis, encoding — onto commodity local hardware, and uses
> the cloud only for the three things it is genuinely good at: identity, small structured state, and
> reaching your phone. The result runs on a 6 GB laptop-class GPU for the cost of electricity.

Every architectural decision below is downstream of that sentence.

### 1.1 The one-line v0.1 milestone

> Paste a YouTube URL on your phone → walk away → get a push notification → review 3 AI-selected
> vertical clips → approve one.

Nothing else ships in v0.1.

---

## 2. Hardware reality check

Measured on the target machine, 2026-09-07:

| Resource | Value | Consequence for the design |
| --- | --- | --- |
| GPU | RTX 3050, **6144 MiB** VRAM | **The binding constraint.** Whisper and the LLM cannot be co-resident |
| CPU | i9-14900K, 24C / 32T | Effectively free parallelism for ffmpeg, VAD, face tracking, I/O |
| RAM | 32 GB | Comfortable; holds a full transcript plus working set easily |
| Disk | C: 327 GB free, P: 157 GB free | Sources are large; needs an explicit workspace quota and GC |
| NVENC | Ampere 7th-gen (h264, hevc) | Hardware encode available — the render stage need not touch the CPU |

### 2.1 VRAM budget

Usable budget: **6144 MiB − ~700 MiB** (Windows desktop, browser) ≈ **5.4 GB**.

| Stage | Model | Precision / context | Est. VRAM | Lane |
| --- | --- | --- | --- | --- |
| `TRANSCRIBE` | faster-whisper `large-v3-turbo` | `int8_float16` | ~1.6 GB | **GPU (exclusive)** |
| `ANALYZE` | `qwen3.5:4b` via Ollama | 16K ctx | ~3.4 GB on disk | **GPU (exclusive)** |
| `ANALYZE` (stretch) | `llama3.1:8b` via Ollama | 8K ctx | ~4.9 GB on disk | **GPU (exclusive)** |
| `ANALYZE` (too large) | `qwen3.5:9b` / `qwen3.6:27b` | — | 6.6 / 19 GB | ❌ exceeds budget; will spill to CPU |
| `DOWNLOAD` | — | — | 0 | CPU |
| `RENDER` | — | NVENC session | ~200 MB | CPU + NVENC |

**Architectural rule, enforced in code:** a single `ModelBroker` owns GPU residency. At most one model
class is resident at any moment. Whisper models are released by dropping the last reference and
the release is **verified through NVML**, not assumed — there is no PyTorch allocator to ask
(see [ADR-0002](adr/0002-ctranslate2-without-pytorch.md)). Ollama calls pass `keep_alive: 0`
outside a held broker lease. Any stage
wanting the GPU acquires the broker lock. The scheduler therefore runs **GPU stages serially
(depth 1)** and **CPU stages in parallel**, so a download or a render proceeds while another job
transcribes.

> This constraint is a feature of the portfolio narrative, not an embarrassment. "Runs the whole
> pipeline in 5.4 GB by scheduling model residency explicitly" is a far more interesting engineering
> story than "we rented an A100."

### 2.2 Toolchain gaps to close in Phase 0

| Gap | Detail | Action |
| --- | --- | --- |
| **Python 3.14.5 is the only Python** | CTranslate2 publishes no wheels for 3.14; `faster-whisper` will not install | ✅ Done — worker pinned to **Python 3.12.14** via uv ([ADR-0003](adr/0003-uv-managed-python-toolchain.md)) |
| **`ffmpeg` not installed** | Hard dependency of the render stage | ✅ Done — **9.0.1 full build**; `h264_nvenc`, `libx264`, `ass`, `loudnorm`, `silencedetect` all verified present |
| **No `uv`** | Needed for reproducible Python envs and interpreter pinning | ✅ Done — **uv 0.12.10**, `uv.lock` committed |
| **No Docker** | Only needed for the public release phase | Defer to Phase 11 |

Present and usable already: Node 24.16, npm 11.13, Angular CLI, Firebase CLI 15.25.1, Ollama 0.33.3,
git 2.54, Blaze billing enabled.

---

## 3. Architecture

```text
┌───────────────────────────────┐
│  PWA — Angular + Tailwind     │   Identity, submission, review queue,
│  installable, offline shell   │   progress, analytics, settings
└───────────────┬───────────────┘
                │ Firebase SDK (authenticated as the user)
                ▼
┌───────────────────────────────┐
│  FIREBASE — control plane     │   Auth · Firestore (jobs, state, metadata)
│  Blaze                        │   Storage (rendered clips only) · FCM
│  Functions: lease reaper,     │   Rules enforce per-user isolation
│  analytics poller             │
└───────────────┬───────────────┘
                │ Admin SDK (service account) — onSnapshot, not polling
                ▼
┌───────────────────────────────┐
│  LOCAL WORKER — Python 3.12   │
│                               │
│  Scheduler ── GPU lane (1) ───┼─▶ faster-whisper ─┐
│            └─ CPU lane (N) ───┼─▶ Ollama LLM  ────┼─ ModelBroker (exclusive)
│                               │   yt-dlp ─────────┤
│  Stage runner + checkpoints   │   ffmpeg/NVENC ───┘
│  Lease claim + heartbeat      │
└───────────────┬───────────────┘
                │
     ┌──────────┴──────────┐
     ▼                     ▼
  Local workspace     YouTube Data API
  (sources, temp)     (publish — worker holds the token)
```

### 3.1 Decisions that differ from the initial brainstorm

These are deliberate revisions, each with a stated reason.

| # | Initial plan | Revised decision | Why |
| --- | --- | --- | --- |
| D1 | Worker polls Firestore for jobs | **`onSnapshot` listener plus a transactional lease claim** | Polling burns reads and has no crash semantics. A lease gives at-most-once execution and automatic recovery |
| D2 | A job is one monolithic unit | **A job is an ordered pipeline of idempotent, checkpointed stages** | Falls directly out of §2.1: you *must* be able to resume after a model swap or a crash without re-downloading a 2 GB source |
| D3 | Source videos go to Firebase Storage | **Only rendered clips and thumbnails are uploaded** | A 60-min 1080p source is 1–3 GB. Uploading it inverts the local-first thesis and costs real money. Clips are 5–20 MB |
| D4 | The LLM returns clip boundaries | **The LLM proposes semantic windows; a deterministic pass snaps boundaries to silence and sentence ends** | LLMs are unreliable at precise timestamps. Word-level Whisper output plus VAD is exact. Split the judgement from the arithmetic |
| D5 | The LLM returns a total score | **The LLM returns sub-scores; Python computes the total** | Makes the score auditable and reweightable without re-running inference, and removes LLM arithmetic error |
| D6 | One prompt over the whole transcript | **Chunked map-reduce over overlapping windows, then merge/dedupe by IoU** | A 60-min transcript is ~12K tokens. A 4B model at 16K context does poor holistic selection. Windowing is what makes a small model viable |
| D7 | OAuth tokens in Firestore | **Publishing credentials never leave the worker** | The PWA sends a publish *intent*; the worker holds the refresh token. Removes the highest-value secret from the cloud entirely |
| D8 | Types defined twice (TS and Python) | **One JSON Schema source generating TS interfaces and Pydantic models** | The PWA and the worker are two implementations of one protocol. Hand-syncing two definitions is how this project would rot |
| D9 | A 14-day calendar | **Milestones with exit criteria** | No deadline; quality first. Dates would be fiction |

ADRs will be written for D1, D2, D3, D4, D6, D7 and D8.

### 3.2 Firestore data model

```text
users/{uid}
  settings                        # render profile, model prefs, publishing toggles

workers/{workerId}                # heartbeat and capability advertisement
  status: ONLINE|OFFLINE|BUSY
  capabilities: { whisper, llm, render, publish }
  gpu: { name, vramTotalMb, vramFreeMb }
  lastSeenAt, version

sources/{sourceId}                # one ingested long-form video (owner: uid)
  provider: youtube|local
  externalId, title, channel, durationSec, contentHash
  localPath                       # never uploaded
  transcripts/{modelVersion}      # word-level segments, cached by contentHash

jobs/{jobId}                      # one pipeline run over one source
  status: QUEUED|RUNNING|COMPLETED|FAILED|CANCELLED
  stages: [{ name, status, startedAt, endedAt, checkpoint, error }]
  workerId, leaseExpiresAt, attempts, maxAttempts
  events/{eventId}                # append-only transition log (audit and debugging)

candidates/{candidateId}          # an LLM-proposed clip window
  sourceId, startSec, endSec, subScores{}, total, hook, reason
  modelVersion, promptVersion

clips/{clipId}                    # a rendered artefact
  candidateId, storagePath, thumbnailPath, durationSec, renderProfile
  review: PENDING|APPROVED|REJECTED
  rights: { basis, attestedBy, attestedAt, note }
  publications/{pubId}            # platform, externalId, state, attempts
  metrics/{yyyymmdd}              # daily analytics snapshot
```

### 3.3 Job and lease protocol

```text
QUEUED  ──claim (txn: CAS status, set workerId + leaseExpiresAt = now+90s)──▶ RUNNING
RUNNING ──heartbeat every 30s, extends the lease──▶ RUNNING
RUNNING ──all stages DONE──▶ COMPLETED
RUNNING ──stage FAILED, attempts < max──▶ QUEUED   (checkpoints retained)
RUNNING ──stage FAILED, attempts >= max──▶ FAILED
RUNNING ──lease expired (reaper Function, 60s cadence)──▶ QUEUED
any     ──user cancels──▶ CANCELLED
```

Per-stage status is `PENDING | RUNNING | DONE | SKIPPED | FAILED`. A stage that is `DONE` is never
re-executed on retry — this is what makes a crash during `ANALYZE` cost seconds instead of the twenty
minutes it took to download and transcribe.

---

## 4. Milestones and phases

| Milestone | Phases | Ships |
| --- | --- | --- |
| **M0 — Foundations** | 0, 1, 2 | A control plane and a worker that can run a no-op job reliably |
| **M1 — Pipeline** | 3, 4, 5, 6 | URL in, rendered vertical clip on disk. No UI |
| **M2 — Product** | 7 | **v0.1.0** — the phone review loop |
| **M3 — Feedback loop** | 8, 9 | **v0.2.0** — publish and measure |
| **M4 — Autonomy** | 10 | **v0.3.0** — trend-driven sourcing |
| **M5 — Release** | 11 | Public open-source launch |

---

### M0 — Foundations

#### Phase 0 — Toolchain and repository skeleton

**Goal.** A clean clone can be brought to a verified-working development environment by one command, on
a machine that has none of the dependencies.

**In scope**

- Monorepo layout (§5), MIT `LICENSE`, `README.md` skeleton, `CONTRIBUTING.md`, `.gitignore`.
- `uv` install; `uv python install 3.12`; worker venv pinned via `uv.lock`.
- Pinned `ffmpeg` install, with `h264_nvenc` capability verified.
- `tools/doctor.ps1` — checks every external dependency (Python 3.12, ffmpeg + NVENC, Ollama with the
  required models pulled, Node, Firebase CLI, GPU and VRAM, free disk) and prints a pass/fail table.
- GitHub Actions: lint, typecheck and unit tests for both `apps/web` and `apps/worker`. GPU-marked
  tests skipped in CI.
- `docs/adr/0001-record-architecture-decisions.md`.

**Explicitly out of scope.** Firebase project creation, any media handling, Docker.

**Deliverables.** Repo skeleton; `doctor` script; green CI on a trivial test in each app.

**Exit criteria** — ✅ **all met, 2026-09-07**

1. ✅ `tools/doctor.ps1` exits 0 with 15/15 checks passing, including `h264_nvenc`.
2. ✅ A real five-second CUDA transcription succeeds through CTranslate2 on Python 3.12.14.
   *(Revised: the original criterion was `torch.cuda.is_available()`. There is no PyTorch —
   see below and [ADR-0002](adr/0002-ctranslate2-without-pytorch.md). The replacement is a
   strictly stronger check because it exercises the real code path.)*
3. ✅ Lint, format, strict typecheck and tests pass for both apps; CI workflow committed.
4. ✅ Setup documented in `README.md` and verified by `doctor`.

**What Phase 0 actually found.** Three things that would each have cost a debugging session later:

| Finding | Consequence |
| --- | --- |
| **`faster-whisper` does not use PyTorch.** It runs on CTranslate2 | Dropped torch entirely: ~2.5 GB and a whole class of CUDA wheel-matching failures avoided. VRAM accounting uses NVML directly ([ADR-0002](adr/0002-ctranslate2-without-pytorch.md)) |
| **CTranslate2 could not load `cublas64_12.dll`.** The pip `nvidia-*` packages ship the DLLs into `site-packages/nvidia/*/bin`, which Windows never searches | Fixed properly in `clipforge/models/cuda.py` via `os.add_dll_directory()`, not as a documented PATH hack. This is the exact Phase 4 sharp edge, caught in Phase 0 as intended |
| **An unrelated Ollama session held 4.8 GB of VRAM** (`qwen3.6:27b` at 17% GPU offload), leaving 1.3 GB free | The `ModelBroker` must detect **foreign** VRAM consumers, not just manage its own. Added to Phase 2 scope |
| **Windows PowerShell 5.1 reads `.ps1` as ANSI without a BOM** | A non-ASCII character in `doctor.ps1` produced a parser error. Scripts are now ASCII + BOM, and CI enforces both |

**Delivered.** Monorepo skeleton · MIT licence · `.env.example` · `tools/doctor.ps1` (15 checks) ·
worker on Python 3.12 with ruff + strict mypy + 4 pytest tiers · Angular 22 + Tailwind 4 PWA
skeleton with ESLint and Vitest · GitHub Actions CI (worker, web, PowerShell parse) ·
ADRs [0001](adr/0001-record-architecture-decisions.md),
[0002](adr/0002-ctranslate2-without-pytorch.md), [0003](adr/0003-uv-managed-python-toolchain.md).

---

#### Phase 1 — Contracts and control plane

**Goal.** The job state machine, data model and security boundary exist and are tested before a single
line of media code is written.

This phase is deliberately first. The PWA and the worker are two independent implementations of one
protocol; defining that protocol once, in a generated form, is the difference between a project that
stays coherent and one that drifts.

**In scope**

- `packages/contracts`: JSON Schema definitions for `Job`, `Stage`, `Source`, `Transcript`,
  `Candidate`, `Clip`, `WorkerHeartbeat`, and the LLM's structured response.
- Codegen: JSON Schema → TypeScript interfaces (`json-schema-to-typescript`) and → Pydantic v2 models
  (`datamodel-code-generator`). A CI check fails if generated output is stale.
- Firebase project creation; Auth (Google provider); Firestore; Storage; FCM.
- `firestore.rules` and `storage.rules` enforcing strict per-`uid` isolation.
- `firestore.indexes.json` for the queries the review queue and analytics will need.
- Firebase Emulator Suite wired into local dev and CI.
- Cloud Function: the **lease reaper**, scheduled every 60s.

**Explicitly out of scope.** Any worker logic, any UI beyond a login stub, any media.

> #### ⚠️ Open item — Firebase project and billing tier
>
> **Constraint (recorded 2026-09-07).** The intended `ClipForge` Firebase project is blocked on upgrade
> issues. Until it is resolved, use the existing **`miltongore`** project as a temporary gateway. Every
> project id must therefore be config, never hardcoded — see the mitigation below.
>
> **Can we just stay on the free Spark plan?** Partly, and further than you would expect. Assessment
> to be confirmed when Phase 1 actually starts:
>
> | Phase 1 need | Spark (free)? | Notes |
> | --- | --- | --- |
> | **Emulator Suite** | ✅ Fully free | Runs locally against a fake project id. **No billing, no real project, not even an internet connection.** Exit criteria 1–4 of Phase 1 are all emulator-backed |
> | Firebase Auth | ✅ Free tier | Google provider is within the no-cost tier |
> | Firestore | ✅ Free tier | 1 GiB stored, 50K reads / 20K writes per day. Ample for this workload if progress writes are throttled (§7) |
> | FCM | ✅ Free | No billing requirement |
> | **Cloud Functions** | ❌ **Blaze required** | Hard blocker, no workaround |
> | **Cloud Storage** | ⚠️ **Verify** | Since roughly Oct 2024 a *new* default bucket requires Blaze. Projects with a pre-existing bucket may still use the no-cost tier. Must be checked against the actual `miltongore` project rather than assumed |
>
> **The useful consequence:** the only Spark-blocked items are Cloud Functions and possibly the Storage
> bucket, and *neither is needed to complete Phase 1's exit criteria*, which are all emulator-backed.
> The project/billing decision can be deferred until the end of Phase 1 without blocking any work.
>
> **Design mitigations to adopt now, so the decision stays cheap either way:**
>
> 1. **The lease reaper does not have to be a Cloud Function.** For a single-worker deployment the
>    worker can reclaim its own expired leases on a periodic task. Build the reaper as a *pure function*
>    over job documents (Phase 2), then bind it to either a worker task or a Cloud Function. This
>    removes the only hard Spark blocker in the v0.1 path, and is better design regardless.
> 2. **Clip hosting is a genuine fork if Storage needs Blaze.** Options, in preference order: Firebase
>    Storage → a plain GCS bucket → an S3-compatible bucket (R2) behind the same `BlobStore` interface.
>    Keep uploads behind a `BlobStore` port from day one so this is a one-adapter change.
> 3. **Never hardcode a project id.** Project id, bucket and emulator hosts come from `.env` only, so
>    moving from `miltongore` to `ClipForge` is a config edit and nothing else. **This applies from
>    Phase 0 onward** and is why `.env.example` carries these keys before any Firebase work begins.
>
> **To discuss when Phase 1 starts:** whether to run on `miltongore` + Blaze, or hold on Spark and defer
> Storage. Do not resolve it earlier — the emulator makes it a non-blocking decision.

**Deliverables.** `packages/contracts` with generated artefacts; deployed rules; emulator config; the
reaper Function; ADR-0002 (lease-based job claiming) and ADR-0003 (single-source contracts).

**Exit criteria**

1. Emulator-backed test: user A creates a job; user B receives `PERMISSION_DENIED` on read, write and
   list. Same for Storage paths.
2. Emulator-backed test: two simulated workers race to claim one `QUEUED` job; exactly one wins.
3. Emulator-backed test: a `RUNNING` job whose `leaseExpiresAt` is in the past is returned to `QUEUED`
   by the reaper, with `attempts` incremented.
4. Editing a JSON Schema without regenerating fails CI.

**Risks.** *Rules complexity growing untested* → every rule change requires a corresponding emulator
test in the same PR. Treat `firestore.rules` as production code.

---

#### Phase 2 — Worker skeleton: scheduling, leasing, resumability

**Goal.** A worker process that reliably executes a checkpointed pipeline and survives being killed.

**In scope**

- Worker entrypoint, config (`pydantic-settings`, `.env`), service-account credential loading.
- Firestore `onSnapshot` subscription to `QUEUED` jobs; transactional claim; a 30s heartbeat renewing
  the lease; a `workers/{workerId}` heartbeat document advertising capabilities.
- **Stage runner**: ordered stages, per-stage checkpoint persisted to the job document, `DONE` stages
  skipped on retry, structured error capture.
- **Two-lane scheduler**: GPU lane depth 1, CPU lane depth N (configurable, default 3).
- **`ModelBroker`**: exclusive GPU residency lock, explicit load/unload, VRAM probe via `pynvml`,
  refusing to load a model that does not fit the remaining budget. Release is **verified** through
  NVML rather than assumed — there is no PyTorch allocator to interrogate ([ADR-0002](adr/0002-ctranslate2-without-pytorch.md)).
- **Foreign VRAM consumers.** Observed during Phase 0: an unrelated interactive Ollama session was
  holding 4.8 GB of the 6 GB card, leaving 1.3 GB free. The broker must therefore measure *actual
  free* VRAM, not merely track its own allocations, and on a shortfall either wait with backoff or
  fail the stage with an actionable message naming the process holding the memory. A job that dies
  with an opaque CUDA OOM because a chat session was open is exactly the failure this prevents.
- Graceful shutdown: on SIGINT/SIGTERM, finish or checkpoint the current stage, release the lease, mark
  the worker `OFFLINE`.
- Structured JSON logging with a job/stage correlation id; per-stage timing and peak VRAM written back
  to the job document.
- A no-op `ECHO` job type of three artificial stages, used purely to test the harness.

**Explicitly out of scope.** yt-dlp, Whisper, Ollama, ffmpeg. This phase must not touch media.

**Deliverables.** `apps/worker` package; the `ECHO` job type; integration tests against the emulator.

**Exit criteria**

1. An `ECHO` job submitted to the emulator flows `QUEUED → RUNNING → COMPLETED`, with per-stage timings
   recorded and an append-only event log.
2. The worker is `taskkill /F`'d mid-stage-2; on restart it re-claims the job after lease expiry and
   resumes at stage 2 without re-running stage 1. Asserted in a test, not merely observed.
3. Two worker instances run concurrently against one queue with no double execution.
4. `ModelBroker` refuses a synthetic 8 GB allocation request and reports the budget shortfall.
5. With free VRAM artificially constrained, the broker reports the shortfall and names the holding
   process rather than surfacing a CUDA OOM.
6. `Ctrl-C` releases the lease within two seconds and flips the heartbeat to `OFFLINE`.

**Risks.** *Windows signal handling for graceful shutdown is unlike POSIX* → exercise the real shutdown
path on Windows in this phase, not at release time.

---

### M1 — Pipeline

#### Phase 3 — Ingestion

**Goal.** A YouTube URL or a local file becomes a `Source` on disk with metadata in Firestore, exactly
once, without ever filling the disk.

**In scope**

- A `SourceAdapter` interface with two implementations: `YouTubeAdapter` (yt-dlp) and
  `LocalFileAdapter` (watch folder or explicit path). The local adapter exists so the entire downstream
  pipeline is testable offline and in CI.
- Metadata extraction; content hashing for dedupe; re-submitting a known video is a no-op returning the
  existing `sourceId`.
- Format selection policy: cap at 1080p, prefer a single pre-muxed stream to avoid a remux step.
- **Workspace manager**: configurable disk cap, LRU garbage collection of source files whose clips are
  already rendered and uploaded, explicit `--keep` pinning.
- A failure taxonomy — unavailable, geo-blocked, age-gated, live, too long, rate-limited — each mapped
  to a distinct user-facing error rather than a stack trace.

**Explicitly out of scope.** Playlists, whole channels, YouTube-provided subtitles, trend search.

**Deliverables.** The ingest stage; the workspace GC; adapter tests using a locally generated fixture
video (`ffmpeg testsrc` plus `sine`) so CI needs no network.

**Exit criteria**

1. A URL submitted from a test client results in a source file on disk and a `sources/{id}` document
   with title, channel, duration and content hash.
2. Submitting the same URL twice creates one source and one job, not two.
3. With the cap set below the current workspace size, GC reclaims space and the pipeline still runs.
4. Each failure mode in the taxonomy produces its mapped error code, verified with stubbed responses.
5. The full downstream pipeline can be exercised through `LocalFileAdapter` with zero network access.

**Risks.** *yt-dlp breaks when YouTube changes* → pin the version, isolate every yt-dlp call behind the
adapter, and add a nightly non-blocking CI job that flags upstream breakage early. This is a
maintenance cost the README should state honestly.

---

#### Phase 4 — Transcription

**Goal.** Word-level, timestamped, cached transcripts produced within the VRAM budget.

**In scope**

- `faster-whisper` (CTranslate2) with `large-v3-turbo` at `int8_float16`; model choice configurable.
- Audio extraction via ffmpeg to 16 kHz mono.
- **Word-level timestamps** — mandatory, not optional. Phase 5's boundary snapping and Phase 6's
  karaoke captions both depend on them.
- A Silero VAD pass, persisted alongside the transcript: the silence map is reused by boundary snapping.
- Language detection with a configurable allow-list.
- Transcript caching keyed by `(contentHash, modelVersion)` — re-analysing a video never re-transcribes.
- `ModelBroker` integration: load → transcribe → unload → assert VRAM returns to baseline.

**Explicitly out of scope.** Diarisation, translation, custom vocabulary.

**Deliverables.** The transcribe stage; transcript and VAD schema and storage; a committed 60-second
fixture with a known reference transcript.

**Exit criteria**

1. A 10-minute video transcribes end to end, and every segment carries word-level timestamps.
2. Measured VRAM returns to within 100 MB of baseline after the stage completes.
3. Re-running the job is a cache hit: the transcribe stage reports `SKIPPED`.
4. WER against the committed fixture is below an agreed threshold; the test fails if it regresses.
5. Throughput is recorded in the job metrics as a realtime factor, so later model swaps are comparable.

**Risks.** *CTranslate2 CUDA/cuDNN DLL resolution on Windows is a known sharp edge* → `doctor` performs
a real five-second GPU transcription as a smoke check, so this fails in Phase 0 rather than mid-pipeline.

---

#### Phase 5 — Clip candidate selection

**Goal.** A transcript becomes a ranked, deduplicated set of clip candidates with exact, defensible
boundaries. This is the intellectual core of the project.

**The algorithm** — deliberately splitting *judgement* (the LLM) from *arithmetic* (deterministic code):

```text
1. WINDOW    Split the transcript into 120s windows, 30s stride (overlapping)
2. MAP       Per window: a schema-constrained LLM call → 0..3 candidates
             { startSec, endSec, hook, reason, subScores{...} }
3. REDUCE    Merge candidates with IoU > 0.5, keeping the higher total
4. SNAP      start → nearest silence >= 200ms in [-3.0s, +1.5s] of the first word
             end   → nearest silence >= 200ms in [0s, +3.0s] after the last word
             never split a word; clamp to source bounds
5. SCORE     total = 0.25*hook + 0.20*curiosity + 0.20*standalone
                   + 0.15*emotion + 0.10*pacing + 0.10*shareability
             computed in Python from the LLM's sub-scores
6. FILTER    enforce duration in [15s, 75s]; drop anything below the score floor
7. RANK      emit the top N (default 5)
```

**In scope**

- An Ollama client with **JSON-schema-constrained output** (Pydantic model → schema → `format`), plus a
  bounded repair-retry loop for the residual failure rate.
- Prompt templates versioned in-repo, with `promptVersion` stamped on every candidate so results stay
  attributable after a prompt change.
- The windowing, merge, snap, score, filter and rank steps as pure functions — fully unit-testable with
  no GPU and no network.
- A model registry with a documented VRAM cost per option, so a model can be swapped by config.

**Explicitly out of scope.** Visual analysis, training or fine-tuning anything, multi-model ensembles.

**Deliverables.** The analyze stage; prompt templates; a golden-file test suite over committed
transcripts.

**Exit criteria**

1. Golden test: a fixed transcript produces a stable ranked candidate set (deterministic seed, temp 0).
2. Across 50 consecutive windows, 100% of LLM responses parse against the schema after at most one
   repair retry, and the raw pre-repair rate is recorded.
3. No emitted boundary falls inside a word, and every boundary sits within the configured tolerance of a
   detected silence — property-tested over generated transcripts.
4. No two emitted candidates overlap by more than the configured IoU threshold.
5. The stage completes within the VRAM budget, with Whisper confirmed unloaded.
6. Sub-score weights can be changed and candidates re-ranked **without re-running inference**.

**Risks.** *A 4B model produces bland or repetitive selections.* Criterion (1) makes quality measurable
rather than vibes-based. Mitigation ladder, in order: prompt iteration → few-shot exemplars → an 8B
model at reduced context → two-pass (cheap shortlist, expensive rerank). The decision is recorded in an
ADR once measured.

---

#### Phase 6 — Render

**Goal.** A candidate becomes a finished, playable, captioned 1080×1920 MP4.

**In scope**

- ffmpeg pipeline: precise cut (re-encode, not stream-copy, for frame accuracy) → 9:16 transform →
  burned-in captions → loudness normalisation (EBU R128 `loudnorm`) → `h264_nvenc` encode → thumbnail
  extraction at a chosen frame.
- **9:16 transform, v0.1:** a configurable static crop (centre / left / right) with a fixed safe area.
  Face-tracked dynamic crop is deferred to Phase 6b, explicitly.
- **Captions:** generated ASS from word-level timestamps, using `\k` karaoke timing for word-by-word
  highlighting. Style belongs to a named, versioned render profile — never hardcoded.
- Render profiles as data (`profiles/*.toml`): crop mode, caption style, font, safe margins, bitrate.
- Deterministic output: the same candidate, profile and source produce a byte-comparable render.

**Explicitly out of scope (Phase 6b or later).** Face tracking, B-roll, zoom and motion effects, music
beds, background removal, multi-speaker cuts.

**Deliverables.** The render stage; two shipped render profiles; upload of clip and thumbnail to Storage
with a signed-URL accessor; an ADR on burned-in captions versus sidecar subtitles.

**Exit criteria**

1. A candidate renders to a 1080×1920 H.264 MP4 that plays correctly in Chrome, iOS Safari and the
   YouTube Shorts player.
2. Caption timing is frame-accurate against the word timestamps, verified by extracting frames at known
   word boundaries and asserting text presence.
3. Integrated loudness lands within ±1 LU of the −14 LUFS target.
4. A 60-second clip renders in under 30 seconds on the target hardware, using NVENC.
5. Re-rendering the same candidate is byte-identical.
6. Only the clip and thumbnail reach Storage; the source never does — asserted in a test.

**Risks.** *NVENC quality at low bitrates is worse than x264* → the profile carries the encoder choice;
benchmark both and record the outcome. The i9-14900K makes an x264 fallback perfectly viable.

---

### M2 — Product

#### Phase 7 — The PWA review loop → **v0.1.0**

**Goal.** The full milestone from §1.1, usable one-handed on a phone.

**In scope**

- Angular and Tailwind PWA; Firebase Auth (Google); route guards.
- **Submit**: paste a URL, validate it, create the source and job, watch it appear in the queue.
- **Progress**: live per-stage progress driven by `onSnapshot` — the user sees `TRANSCRIBE 40%`, not a
  spinner. This is where the checkpointed stage model pays off visibly.
- **Review queue**: a card per clip with an inline video player, the score and its sub-score breakdown,
  the hook line, the LLM's stated reason, and the source timestamp linking back to the original.
- Approve and reject with an undo window; swipe gestures on touch, keyboard shortcuts on desktop.
- **FCM push** when a job completes, deep-linking into the review queue.
- A worker status indicator (online/offline/busy, VRAM, current job) driven by the heartbeat document.
- PWA essentials: manifest, service worker, offline app shell, installable, responsive down to 360px.
- Empty, loading, error and offline states for every view. No dead ends.

**Explicitly out of scope.** Publishing, analytics, trends, and any settings beyond render-profile
selection.

**Deliverables.** `apps/web` complete for the review loop; Playwright E2E against the emulator with a
stubbed worker; screenshots for the README.

**Exit criteria**

1. **The demo:** on a physical phone, paste a URL, lock the phone, receive a push notification, open it,
   review the clips, approve one. Recorded as the README demo.
2. Playwright E2E covers submit → progress → review → approve against the emulator.
3. Lighthouse: PWA installable, accessibility ≥ 90.
4. Every view has defined empty, loading, error and offline states.
5. Killing the worker mid-job shows an honest "worker offline, job will resume" state rather than a hang.
6. **Tag `v0.1.0`.**

**Risks.** *iOS PWA push requires home-screen installation and has its own quirks* → verify on the
actual target device early in the phase; fall back to an in-app notification plus email if it proves
unreliable, and say so in the README.

---

### M3 — Feedback loop

#### Phase 8 — Publishing, with a rights gate → *toward v0.2.0*

**Goal.** An approved clip reaches YouTube — deliberately, credentialed locally, and with an audit trail
of why publishing it was legitimate.

**A note on the third-party source decision.** Ingesting and analysing someone else's video locally is
ordinary private use. *Publishing* a derived clip of third-party copyrighted material is a different
act, and the legal basis is the operator's to establish — it varies by jurisdiction, by the source's
licence, and by how transformative the clip is. Rather than pretend this does not exist, ClipForge makes
it explicit and auditable: publishing is **disabled by default**, and no clip can be published without a
recorded rights basis. That gate is a genuine design asset for a portfolio reviewer, not bureaucratic
overhead — it demonstrates the judgement most similar projects skip entirely.

**In scope**

- **The rights gate**: `clips.rights.basis` ∈ `{ OWN_CONTENT, LICENSED, PERMISSION_GRANTED,
  FAIR_USE_ASSERTED, PUBLIC_DOMAIN }`, plus the attesting user, a timestamp and a free-text note.
  Enforced in Firestore rules *and* in the worker — a clip without an attestation cannot enter the
  publish queue.
- A config flag `publishing.enabled`, defaulting to `false`, documented.
- YouTube Data API v3 OAuth **on the worker**; the refresh token stored encrypted in the local worker
  config directory. Never written to Firestore (D7).
- Resumable upload with retry; title, description, tags, category; privacy status defaulting to
  `unlisted`.
- Idempotency: a `publications` document is created *before* the upload attempt, so retries reconcile
  against the platform rather than double-posting.
- A publish audit log: who attested, on what basis, what was uploaded, when, and the resulting video id.
- Scheduling: a publish-at time honoured by the worker.

**Explicitly out of scope.** TikTok and Instagram (v0.3+ — both require app review with materially
harder approval paths, and are scoped separately).

**Deliverables.** The publish stage; the OAuth setup flow and its documentation; the rights gate in
rules, worker and UI; an ADR on worker-held credentials.

**Exit criteria**

1. An approved, attested clip uploads and appears as an unlisted Short on the target channel.
2. A clip without a rights attestation is rejected by Firestore rules *and* by the worker — two
   independent tests.
3. Interrupting the upload and retrying produces exactly one YouTube video, verified against the API.
4. A `grep` over a Firestore export finds no OAuth token or refresh token. Asserted in a test.
5. The audit log can reconstruct, for any published clip, who authorised it and on what basis.

**Risks.** Two real ones, both worth documenting in the README:

- *OAuth consent screen in "Testing" mode expires refresh tokens after 7 days.* The YouTube upload scope
  is **sensitive**, so moving to production requires Google verification. Build for the
  re-auth-every-7-days path first, and document verification as the production route.
- *Quota.* The default YouTube Data API quota is 10,000 units/day and an upload costs **1,600 units** —
  roughly **six uploads per day**. The scheduler must budget quota and surface it in the UI rather than
  failing opaquely on the seventh upload.

---

#### Phase 9 — Analytics and calibration → **v0.2.0**

**Goal.** Close the loop: find out whether the scores predicted anything.

**In scope**

- A scheduled Cloud Function (or worker cron) polling the YouTube Analytics API for published clips.
- Metrics stored as daily snapshots: views, likes, comments, average view duration, **retention curve**,
  shares, subscribers gained.
- Aggregations by hook type, clip duration bucket, predicted score band, topic, posting time, caption
  style and render profile.
- A **calibration report**: the correlation between the predicted `total` score and realised
  performance. Reweight the §5 rubric from the data — the weights are config (D5), so this needs no
  re-inference.
- Dashboard views in the PWA.

**Explicitly out of scope.** Training a model. Statistical description is the deliverable; ML is not.
With tens of clips, any model would overfit, and the honest thing is to say so.

**Deliverables.** The analytics poller; the metrics schema; the dashboard; a written calibration report
in `docs/`.

**Exit criteria**

1. A published clip accrues daily metric snapshots without gaps.
2. The dashboard renders retention curves and the cohort breakdowns above.
3. The calibration report states the observed correlation between predicted score and retention —
   **including if it is near zero.** A negative result reported honestly is a stronger portfolio signal
   than a fabricated positive one.
4. Adjusted weights re-rank historical candidates with no LLM calls.
5. **Tag `v0.2.0`.**

**Risks.** *Sample size.* Ten clips support no conclusions. The report must state confidence intervals
and refuse to over-claim.

---

### M4 — Autonomy

#### Phase 10 — Trend-driven sourcing → **v0.3.0**

**Goal.** The system proposes what to work on, with a human still deciding.

**In scope.** A manually triggered trend search (YouTube search API plus a configurable topic list); an
opportunity scorer combining topic velocity, channel size and recency; a queue of proposed sources the
user promotes into jobs with one tap; **explicitly no autonomous execution**.

**Explicitly out of scope.** Continuous background crawling, auto-publishing, cross-platform scraping.
The v0.5 "autonomous agent" from the original brainstorm stays a roadmap entry rather than a build item
until Phase 9 has produced evidence that the scoring is worth automating. Automating an uncalibrated
scorer just produces bad clips faster.

**Exit criteria.** A manual trend run produces a ranked opportunity list; promoting one creates a normal
job; quota consumption is bounded and displayed; nothing runs without a human trigger.

---

### M5 — Release

#### Phase 11 — Open-source hardening

**Goal.** A stranger can run this, and a reviewer can understand it in five minutes.

**In scope.** Docker Compose for the worker, with GPU passthrough documented; a complete installation
guide; `.env.example`; the architecture diagram; a 60–90 second demo video; screenshots; issue and PR
templates; `SECURITY.md`; a public roadmap; an honest **Limitations** section (the VRAM ceiling, yt-dlp
fragility, YouTube quota, OAuth verification, rights responsibility); and a README rewritten to lead
with the §1 thesis.

**Exit criteria.** A clean machine reaches a rendered clip by following the docs alone; the README
states limitations plainly; CI badges are green; the repository is made public.

---

## 5. Repository layout

```text
clipforge/
├─ apps/
│  ├─ web/                    # Angular + Tailwind PWA
│  └─ worker/                 # Python 3.12 worker (uv-managed)
│     ├─ clipforge/
│     │  ├─ stages/           # download · transcribe · analyze · render · publish
│     │  ├─ scheduler/        # lanes, lease, heartbeat, checkpoints
│     │  ├─ models/           # ModelBroker, whisper + ollama clients, registry
│     │  ├─ media/            # ffmpeg wrappers, ASS captions, profiles
│     │  └─ store/            # Firestore + Storage adapters
│     └─ tests/               # unit · integration · gpu (marked) tiers
├─ packages/
│  └─ contracts/              # JSON Schema → generated TS + Pydantic (single source of truth)
├─ firebase/
│  ├─ functions/              # lease reaper, analytics poller
│  ├─ firestore.rules
│  ├─ storage.rules
│  └─ firestore.indexes.json
├─ docs/
│  ├─ PLAN.md                 # this document
│  ├─ adr/
│  └─ diagrams/
├─ tools/                     # doctor.ps1, dev scripts
└─ .github/workflows/
```

## 6. Testing strategy

| Tier | Runs in CI | Needs | Covers |
| --- | --- | --- | --- |
| `unit` | ✅ every PR | nothing | Merge/snap/score/rank, ASS generation, schema validation, state machine |
| `integration` | ✅ every PR | Firebase emulator | Rules, lease/claim races, the reaper, the stage runner, resumability |
| `e2e` | ✅ every PR | emulator + stub worker | Playwright: submit → progress → review → approve |
| `gpu` | ❌ opt-in, local | RTX 3050, Ollama, ffmpeg | Real transcription, real inference, real encode |

The `unit`, `integration` and `e2e` tiers must run with **no GPU and no network**, which is what makes
`LocalFileAdapter` (Phase 3) and the committed fixtures load-bearing rather than nice-to-have.

## 7. Risk register

| Risk | Likelihood | Impact | Mitigation | Phase |
| --- | --- | --- | --- | --- |
| 6 GB VRAM insufficient for the chosen models | Medium | High | `ModelBroker` plus a serial GPU lane, and a documented model ladder from 4B upward | 2, 5 |
| CUDA/cuDNN DLL resolution fails on Windows | ~~Medium~~ **Hit** | High | ✅ **Resolved in Phase 0** — `clipforge/models/cuda.py` registers the DLL directories; `doctor` runs a real GPU transcription | 0, 4 |
| A foreign process holds the VRAM the pipeline needs | **High** | High | Broker measures actual free VRAM and names the holding process; observed in Phase 0 | 2 |
| yt-dlp breaks on a YouTube change | High | Medium | Pinned version, isolated behind the adapter, nightly canary, honest README note | 3 |
| A small model produces bland clip selections | Medium | High | Golden tests make quality measurable; a documented mitigation ladder | 5 |
| YouTube quota allows only ~6 uploads/day | Certain | Medium | Quota budgeting in the scheduler, surfaced in the UI | 8 |
| OAuth refresh tokens expire every 7 days in Testing mode | High | Medium | Build for re-auth; document Google verification as the production path | 8 |
| Rights exposure on third-party source material | Medium | High | Attestation gate in rules and worker; publishing off by default; documented | 8 |
| Firestore cost from chatty progress updates | Low | Medium | Throttle progress writes to ≥2s; batch event-log entries | 2, 7 |
| Scope creep into "autonomous viral agent" | **High** | High | Every phase names what it explicitly excludes; Phase 10 is gated on Phase 9 evidence | all |

## 8. Immediate next actions

**Phase 0 is complete** (2026-09-07) — `doctor` reports 16/16 passing and both apps are green on
lint, strict typecheck, tests and build. Next is **Phase 1 — Contracts and control plane**:

1. **Decide the Firebase posture.** Spark vs. Blaze on `miltongore` — see the Phase 1 addendum.
   This is a conversation, not a task, and it does **not** block items 2–4 because they are all
   emulator-backed.
2. **Define the contracts.** JSON Schema for `Job`, `Stage`, `Source`, `Transcript`, `Candidate`,
   `Clip`, `WorkerHeartbeat` and the LLM response, in `packages/contracts`. Wire the TypeScript and
   Pydantic codegen plus the CI staleness check.
3. **Stand up the Emulator Suite** and write the security rules, with the per-user isolation tests
   in the same commit.
4. **Build the lease reaper as a pure function** over job documents, bindable to either a worker
   task or a Cloud Function — this is what keeps the Spark option open (Phase 1 addendum,
   mitigation 1).

One correction worth carrying forward: Phase 0 found that `faster-whisper` needs no PyTorch, so
several assumptions in earlier drafts of §2 have been revised. See
[ADR-0002](adr/0002-ctranslate2-without-pytorch.md).
