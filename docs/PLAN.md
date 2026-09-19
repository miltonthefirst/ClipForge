# ClipForge — Execution Plan

> **A local-first AI content agent.** Ingest long-form video, transcribe and analyse it on your own
> hardware, cut high-potential vertical clips, review them from your phone, publish, and learn from
> what actually performed.

**Status** (2026-09-19): M0, M1 and M2 complete · M3 — Phases 8, 8b–8f and 9 built; `v0.2.0`
waits on one real upload · M4 — Phases 10 and 10b built, `v0.3.0` waits on a real run
**Target of record:** v0.2.0, with **v0.1.0 tagged 2026-09-14** · **Owner:** @miltonthefirst
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

**What "review" means.** *Rewritten 2026-09-14.* This section used to describe the Spark posture:
no bucket, so rendered clips stayed on the worker and a phone got stills. The project moved to Blaze
on 2026-09-10 and clips now get a short-lived bucket copy, so **video plays on the phone**
([ADR-0018](adr/0018-blaze-and-a-short-lived-bucket-copy.md), superseding
[ADR-0009](adr/0009-spark-tier-local-artefacts.md) in its central decision).

From the phone you get the clip itself, plus the poster frame, a four-frame filmstrip, the hook, the
score breakdown and the transcript excerpt — and approve or reject from anywhere, because that
decision travels through Firestore. The bucket copy expires after five days; the worker keeps the
master, and playback falls back to the local file server on `127.0.0.1:8765` when the copy is gone.

What made the change necessary is worth keeping: reviewing football is not like reviewing a talking
head. A poster frame cannot tell you whether the crop kept the ball, whether the narration lines up,
or whether a logo is still in the corner — which are precisely the questions Phases 8b and 8d exist
to answer.

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
git 2.54. (Billing tier on `bytepic-clipforge` is still unverified — see
[ADR-0004](adr/0004-dedicated-firebase-project.md).)

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
│  FIREBASE — control plane     │   Auth · Firestore (jobs, state, metadata,
│  Blaze, since Sept 2026       │   poster frames) · FCM · Hosting
│  Storage: 5-day clip copies   │   Rules gate on approval, not ownership
│  No Functions (see ADR-0018)  │   Reaper runs on the worker, not as a Function
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
  sources · clips     (publish — worker holds the token)
  served read-only
  on 127.0.0.1:8765
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

agents/{machineId}                # the supervisor that starts and stops a worker,
                                  # so the PWA can do it from anywhere. The only
                                  # document where a client may influence what runs.
                                  # See docs/adr/0012-machine-agent.md
  desired: RUNNING|STOPPED         # the WISH — the client's three writable fields
  requestedBy, requestedAt         # who asked, and when. Freshness is load-bearing
  state: STOPPED|STARTING|RUNNING|FOREIGN|STOPPING|FAILED
  detail, workerPid, lastExitCode, restarts, log[]
  lastSeenAt                       # keeps beating when no worker runs, which is
                                   # the one thing workers/{workerId} cannot do

sources/{sourceId}                # one ingested long-form video (uid: who submitted it)
  provider: youtube|local
  externalId, title, channel, durationSec, contentHash
  localPath                       # never uploaded
  transcripts/{modelVersion}      # TranscriptRef only: language, counts, localPath.
                                  # The word-level segments live on the worker — a
                                  # 60-min transcript approaches Firestore's 1 MiB
                                  # document limit and the PWA never needs it whole

jobs/{jobId}                      # one pipeline run over one source
  status: QUEUED|RUNNING|COMPLETED|FAILED|CANCELLED
  stages: [{ name, status, startedAt, endedAt, checkpoint, error }]
  workerId, leaseExpiresAt, attempts, maxAttempts
  events/{eventId}                # append-only transition log (audit and debugging)

candidates/{candidateId}          # an LLM-proposed clip window
  sourceId, startSec, endSec, subScores{}, total, hook, reason
  modelVersion, promptVersion

clips/{clipId}                    # a rendered artefact — the FILE stays on the worker
  candidateId, durationSec, renderProfile
  location: LOCAL|REMOTE           # which of the two below is authoritative
  localPath                        # always set
  playbackUrl                      # the bucket copy, for 5 days after the render
  review: PENDING|APPROVED|REJECTED
  preview/poster                   # base64 poster + filmstrip, ~40-60 KB.
                                   # A subcollection so the review-queue query does
                                   # not drag image bytes on every read
  publications/{pubId}            # platform, externalId, state, attempts
  metrics/{yyyymmdd}              # daily analytics snapshot

trends/{trendId}                  # one row of a RESEARCH run's ranked list (Phase 10)
  jobId, topic, rank, score, signals[], videos[], matchedTopics[]
  angle, relevance, worthClipping, compilationTitle, curated   # the model's verdict
  status: NEW|PROMOTED|DISMISSED   # the one thing a client may change

schedules/{scheduleId}            # a standing research request (ADR-0023)
  name, enabled, cadence, everyHours, at, timezone, options{}   # client-owned
  nextDueAt, lastRunAt, lastJobId, lastOutcome                  # worker-owned
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
| **M3 — Feedback loop** | 8, 8b, 8c, 8d, 8e, 8f, 9 | **v0.2.0** — publish, correct and measure |
| **M4 — Autonomy** | 10, 10b, 12 | **v0.3.0** — trend-driven sourcing and compilations, then **v0.4.0** — several channels |
| **M5 — Release** | 11 | Public open-source launch |
| **M6 — Synthesis** | 13, 14 | **v0.6.0** — an idea becomes a finished video |
| **M7 — Reach** | 15 | **v0.7.0** — every platform, correctly labelled |
| **M8 — Cadence** | 16 | **v0.8.0** — a few a week, without being asked |
| **M9 — Calibration** | 17 | **v0.9.0** — one rubric over both kinds of content |

M6 onwards is [the synthesis track](#the-synthesis-track--m6-to-m9), and **none of it starts until M5
is done**.

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
- `firestore.rules` and `storage.rules` gating every collection on approval. *(Originally written
  as strict per-`uid` isolation. That described two private workspaces that happened to share a
  worker — a job submitted from a phone was invisible on the desktop beside it — so access is now
  decided by approval and `uid` records who submitted the work rather than who may see it. The
  client still may not write pipeline state; that half never changed.)*
- `firestore.indexes.json` for the queries the review queue and analytics will need.
- Firebase Emulator Suite wired into local dev and CI.
- Cloud Function: the **lease reaper**, scheduled every 60s.

**Explicitly out of scope.** Any worker logic, any UI beyond a login stub, any media.

> #### ✅ Resolved — Firebase project and billing tier
>
> **Decided 2026-09-08. See [ADR-0004](adr/0004-dedicated-firebase-project.md).**
>
> ClipForge runs in its own Firebase project, **`bytepic-clipforge`**. The earlier plan to borrow
> `miltongore` was dropped once that project turned out to host a live site — and once three facts
> made in-project namespacing both weaker and more expensive than a separate project: Firestore IAM
> is database-level rather than per-collection and the Admin SDK bypasses rules entirely; the
> no-cost tier applies to only one database per project; and a shared database shares the daily
> read quota, not merely the namespace.
>
> Two consequences for the work below. The data model in §3.2 stands unchanged, because generic
> top-level collection names are safe in a project we own outright. And no uid allowlist is needed,
> because a separate project means a separate Auth user pool.
>
> **Settled since.** `bytepic-clipforge` ran on Spark from Phase 2 and moved to **Blaze on
> 2026-09-10** ([ADR-0018](adr/0018-blaze-and-a-short-lived-bucket-copy.md)). The paragraph below is
> the question as it stood when Phase 1 was written, kept because the shape of the answer — a port,
> not a branch — is why the move cost one adapter and one environment variable.
>
> **Unverified at the time:** whether `bytepic-clipforge` is on Blaze. This blocks nothing in Phase 1 or
> Phase 2 — every exit criterion in both is emulator-backed — but it decides whether the reaper's
> Cloud Function binding is deployable and whether clips land in Firebase Storage or behind the
> `BlobStore` port's alternative. The Firestore location must also be chosen deliberately when the
> database is first created: it is permanent.

**Deliverables.** `packages/contracts` with generated artefacts; deployed rules; emulator config; the
reaper; [ADR-0004](adr/0004-dedicated-firebase-project.md) (dedicated Firebase project),
[ADR-0005](adr/0005-single-source-contracts.md) (single-source contracts) and
[ADR-0006](adr/0006-lease-based-job-claiming.md) (lease-based job claiming).

**Exit criteria** — ✅ **all met, 2026-09-08**

1. ✅ 42 emulator-backed rules tests. User B is denied read, write, list and delete on user A's job,
   and denied their Storage objects. Also covers the narrower boundary: a client may create only a
   `QUEUED` job with no worker and no lease, may drive only cancellation, and may write only the
   review fields of a clip.
2. ✅ Two workers race one job; exactly one wins. Also run with **eight** workers, since two can pass
   by luck of scheduling.
3. ✅ A `RUNNING` job with a lapsed lease returns to `QUEUED` with `attempts` incremented, retaining
   its stage checkpoints. Exhausting `maxAttempts` fails it instead.
4. ✅ Verified by deliberately drifting the schema and confirming both the TypeScript and Pydantic
   checks exit non-zero.

**Delivered.** `packages/contracts` (one JSON Schema → TS `.d.ts` + Pydantic, CI staleness gate) ·
`firestore.rules`, `storage.rules`, indexes with cost-motivated `fieldOverrides` · emulator wiring for
local dev and CI · the lease protocol as pure functions plus its transactional Firestore adapter ·
ADRs [0004](adr/0004-dedicated-firebase-project.md), [0005](adr/0005-single-source-contracts.md),
[0006](adr/0006-lease-based-job-claiming.md).

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

**Exit criteria** — ✅ **all met, 2026-09-08**

1. ✅ An `ECHO` job flows `QUEUED → RUNNING → COMPLETED` with per-stage timings, checkpoints and an
   eight-entry append-only event log.
2. ✅ A **real subprocess** is killed inside stage 2 with `os._exit(137)`, a second worker reclaims the
   job after the lease lapses, and stage 1 is never re-executed — asserted by counting stage
   executions in a trace file, not by inspection. A simulated crash would have unwound the stack and
   released the lease, proving nothing.
3. ✅ Six jobs, two workers, concurrent. Proven from the event log rather than final status, because a
   doubly-executed job would still *end* `COMPLETED`.
4. ✅ The broker refuses a synthetic 8 GB request on a 6 GB card and reports the shortfall.
5. ✅ With free VRAM constrained to the 1.3 GB Phase 0 actually observed, the refusal names
   `ollama.exe`, its pid and its 4800 MiB.
6. ✅ Shutdown returns running jobs to `QUEUED` and flips the heartbeat to `OFFLINE`, asserted under a
   two-second budget. Releasing rather than letting the lease lapse is what lets another worker take
   the work immediately.

**Delivered.** Config · structured logging with job/stage correlation · the two-lane scheduler (CPU
pool, and the `ModelBroker` lock *as* the depth-1 GPU lane) · checkpointed stage runner · NVML VRAM
accounting that names foreign consumers · graceful shutdown exercised on Windows · the `ECHO` job type
· `clipforge-worker run | submit | status | gpu` · ADRs
[0007](adr/0007-checkpointed-stage-pipeline.md),
[0008](adr/0008-model-broker-owns-gpu-residency.md).

**Two bugs this phase's tests caught**, both of which would have been intermittent in production:
a worker and its store could hold *different* worker ids, so a worker failed to recognise its own jobs
when releasing them on shutdown; and `claim_next` only ever looked at `QUEUED`, so a crashed job was
invisible to it until the reaper happened to run.

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

**Exit criteria** — ✅ **all met, 2026-09-08**

1. ✅ A submission produces a source file and a `sources/{id}` document carrying title, duration,
   content hash, size and `lastAccessedAt`.
2. ✅ Submitting the same media twice creates **two jobs and one source** — the user asked twice, so
   two jobs is correct; re-fetching gigabytes is the waste. Dedupe happens on identity *before* any
   download, and on content hash afterwards to catch the same video under two URLs.
3. ✅ A workspace over its cap forces a collection pass and the stage still completes. GC is
   least-recently-used, never touches pinned or in-use sources, and **reports** a shortfall rather
   than raising from underneath an unrelated stage.
4. ✅ Fifteen stubbed yt-dlp messages, each mapping to its own `IngestErrorCode`, plus a retry-policy
   test asserting that only `RATE_LIMITED` and `NETWORK` are worth retrying. This caught a real gap:
   yt-dlp phrases geo-blocking two different ways and the narrower pattern silently downgraded one to
   `UNKNOWN`.
5. ✅ The whole tier runs through `LocalFileAdapter` against a committed `ffmpeg testsrc` fixture with
   **no network access at all**.

**Delivered.** The `BlobStore` port with its `local` adapter (moved here from Phase 6, since ingestion
is the first thing to write an artefact) · workspace manager with LRU GC and a disk cap · `ffprobe`
wrapper · `SourceAdapter` with local and YouTube implementations · the ingest failure taxonomy ·
`DownloadStage` · `SourceStore` · the CLIP pipeline assembled from the stages that exist ·
`clipforge-worker submit <url|path>` and `workspace` · a nightly, non-blocking yt-dlp canary.

**One design note worth carrying forward.** A local-file source is **never copied into the workspace
and never evicted**. The file is the user's, it may be very large, and duplicating it would double the
disk cost of the one thing already straining the disk budget — which matters more on the free tier,
where rendered clips also never leave the machine.

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

> **Free tier.** The word-level transcript and the VAD silence map are written to the **workspace**,
> not Firestore. A 60-minute word-level transcript approaches Firestore's 1 MiB document limit, and the
> PWA never needs one whole — `Candidate.transcriptExcerpt` carries the part a reviewer reads.
> Firestore keeps a `TranscriptRef`: language, duration, word and segment counts, and the local path.
> See [ADR-0009](adr/0009-spark-tier-local-artefacts.md).

**Exit criteria** — ✅ **all met, 2026-09-08**

1. ✅ Every segment carries word-level timestamps, verified on real hardware, including that word
   timings advance monotonically — boundary snapping picks the wrong cut point otherwise.
2. ✅ Measured through NVML before and after: under 100 MB unreleased. There is no allocator to
   interrogate, so this reads the card directly.
3. ✅ A second job over the same media reports **`SKIPPED`**, and the substituted transcriber records
   that it was never called. Keyed on `(contentHash, modelVersion)`, so the same video under a
   different path is also a cache hit.
4. ✅ WER against a **public-domain LibriVox fixture** stays under 0.15. The reference is Poe's
   *published* text verified by ear — not Whisper's own output, which would prove only
   self-consistency. `assets/fixtures/speech/custom/` is a gitignored slot for a recording of your own
   voice, picked up automatically when present.
5. ✅ Realtime factor recorded in the stage checkpoint, so a later model swap is comparable against a
   measurement rather than a remembered impression.

**Delivered.** Audio extraction to 16 kHz mono · `WhisperTranscriber` under the broker's exclusive
lease · word-level transcripts and a voice-activity map on disk, `TranscriptRef` in Firestore ·
content-hash caching · `SKIPPED` as a first-class stage outcome · WER and text normalisation.

**Two things this phase settled.** Normalisation strips punctuation and casing before scoring, because
Whisper's punctuation is a formatting choice and a threshold that measured typography would be
meaningless — but numbers are deliberately *not* normalised, since "1846" and "eighteen forty-six"
really are different transcriptions. And the extracted WAV is deleted after transcription: it is
reconstructible from the source and can be hundreds of megabytes, while the transcript is the artefact
worth keeping.

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

**Exit criteria** — ✅ **all met, 2026-09-08**

1. ✅ A fixed transcript through a scripted model produces a byte-identical ranked set on repeated
   runs; ties break by start time so equal scores cannot shuffle. Temperature 0 and a fixed seed.
2. ✅ Verified against the **real** local model: 100% of responses parsed first time, zero repairs.
   The mocked half scripts malformed responses to exercise the repair path precisely. Pre-repair and
   post-repair rates are recorded separately — folding them together would hide the one signal worth
   watching.
3. ✅ Property-tested over 25 generated transcripts. This found a real defect: the snapper trusted the
   silence map, but silence and word timings come from different passes and disagree by milliseconds,
   so a "silent" midpoint could land a hair inside a word. The invariant is now *enforced* rather than
   inferred.
4. ✅ Asserted over 20 deliberately-overlapping candidates.
5. ✅ The whole map step runs under one broker lease, and `keep_alive: 0` makes Ollama release rather
   than hold the model — without which the next transcription cannot load.
6. ✅ Reweighting re-ranks with no model involved. The plan states the rubric twice and the two
   statements look contradictory; they coincide exactly when the weights are the rubric proportions,
   which is why the default weights reproduce the plain sum.

**Delivered.** Windowing · IoU merge · boundary snapping · weighted scoring · filter and rank, all as
pure functions · schema-constrained Ollama client with a bounded repair loop and call statistics ·
versioned prompts · `AnalyzeStage` · `CandidateStore`.

**The bug worth remembering.** `qwen3.5:4b` is a *reasoning* model: it puts its chain of thought in a
separate `thinking` field and, when constrained by `format`, returns an **empty** `response`. The call
succeeds, the model reasons at length, and nothing usable comes back. `think: false` is what makes
structured output work at all on such a model — and it is the right trade anyway, since selection is a
bounded judgement against an explicit rubric rather than a problem that rewards extended reasoning.
This was found by running the real model, not by reading documentation.

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

**Deliverables.** The render stage; two shipped render profiles; the **`BlobStore` port** with its
`local` adapter; poster-frame and filmstrip extraction written to `clips/{clipId}/preview/poster`; an
ADR on burned-in captions versus sidecar subtitles.

> **Free tier, as Phase 6 shipped.** There was no Storage bucket
> ([ADR-0009](adr/0009-spark-tier-local-artefacts.md)), so renders were written to the workspace and
> `Clip.localPath` — never uploaded. Every write goes through the `BlobStore` port, whose `firebase`
> adapter is the one thing that needed writing the day Blaze was enabled. The poster frame and
> filmstrip *do* go to Firestore, base64-encoded at ~40-60 KB, which is what makes a phone review
> show the clip rather than a placeholder.
>
> *Since 2026-09-10* that adapter exists and the workspace copy is the master rather than the only
> copy ([ADR-0018](adr/0018-blaze-and-a-short-lived-bucket-copy.md)). The port is why this paragraph
> needed amending rather than the render stage.

**Exit criteria** — ✅ **all met, 2026-09-08**

1. ✅ Renders to a real 1080×1920 H.264/AAC MP4, verified with ffprobe. `+faststart` is asserted by
   checking the `moov` atom is near the front — without it iOS Safari refuses to begin playback at
   all, which reads as a broken clip rather than a container detail.
2. ✅ Verified by rendering the same frame with and without captions and asserting the pictures
   differ. The subtitles filter fails *non-fatally* on a path-escaping mistake, so a test that only
   checked the render succeeded would have passed on silently uncaptioned video.
3. ✅ Measured with `ebur128` on the rendered output, within tolerance of −14 LUFS.
4. ✅ A 60-second clip renders well inside 30 seconds.
5. ✅ Byte-identical on re-render. Requires `-map_metadata -1` and `-fflags +bitexact`; without them
   an embedded creation time differs every run and nothing downstream could ever cache a render.
6. ✅ Asserted against the `BlobStore` port rather than against Storage, so the same test covers both
   the free-tier and Blaze configurations.
7. ✅ Poster and filmstrip together stay under half the 1 MiB document limit.

**Delivered.** Render profiles as versioned TOML data · ASS caption generation with word-level karaoke
timing · the ffmpeg pipeline (seek, crop, scale, burn, loudnorm, encode) · poster and filmstrip
extraction · `RenderStage` · `ClipStore` · two shipped profiles.

**The finding worth carrying forward.** `ffmpeg -encoders` lists `h264_nvenc` on any build compiled
with it, **whether or not the installed driver can run it** — and on this project's own reference
machine it cannot: the driver is one NVENC API version behind the ffmpeg build. Every render would
have failed with a driver error at the last stage of a twenty-minute pipeline.

This is the same class of problem as the cuBLAS DLL in Phase 0 — *present* and *usable* are different
questions — and it gets the same treatment. `doctor` now **encodes a real frame** with NVENC instead
of trusting the listing, and the render stage falls back to libx264 on an encoder-initialisation
failure rather than failing the job. The fallback is deliberately narrow: it matches
encoder-initialisation wording only, because falling back on a genuine parameter error would hide a
real bug behind a slower encode.

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
- **Review queue**: a card per clip with the score and its sub-score breakdown, the hook line, the
  LLM's stated reason, the transcript excerpt, and the source timestamp linking back to the original.
- **Playback, resolved through one documented precedence** ([ADR-0009](adr/0009-spark-tier-local-artefacts.md)):
  `playbackUrl` if set → the worker's local file server if reachable → poster frame and filmstrip
  otherwise. One component, three sources; enabling Blaze later lights up the first branch and changes
  nothing else in the product. *It did, on 2026-09-10, and it changed nothing else in the product —
  which is the clearest evidence the precedence was worth writing down before it was needed.*
- **The worker's local file server**: read-only, bound to `127.0.0.1:8765`, every path confined to the
  workspace root. It is what makes full video review work when the PWA is opened on the machine —
  browsers exempt `localhost` from mixed-content blocking, so it works even over HTTPS. It is a
  convenience, not an authenticated surface, and must not quietly become one.
- Approve and reject with an undo window; swipe gestures on touch, keyboard shortcuts on desktop.
- **FCM push** when a job completes, deep-linking into the review queue.
- A worker status indicator (online/offline/busy, VRAM, current job) driven by the heartbeat document.
- PWA essentials: manifest, service worker, offline app shell, installable, responsive down to 360px.
- Empty, loading, error and offline states for every view. No dead ends.

**Explicitly out of scope.** Publishing, analytics, trends, and any settings beyond render-profile
selection.

**Deliverables.** `apps/web` complete for the review loop; Playwright E2E against the emulator with a
stubbed worker; screenshots for the README.

**Exit criteria** — mostly met, 2026-09-08; two items are honestly outstanding

1. ⏸ **The physical-phone demo is blocked on the real Firebase project.** Everything it exercises is
   built and tested, but it needs a project with Auth and FCM enabled, which the free-tier decision
   deliberately deferred. The rest of Phase 7 does not depend on it.
2. ✅ Ten Playwright tests cover submit → progress → review → approve against the Emulator Suite, in a
   real browser, with a stubbed worker.
3. ⏸ **Lighthouse not yet run.** The audit needs a served build and is worth doing alongside item 1.

   *Corrected 2026-09-09.* This originally read "the manifest, icons and installability are in
   place". The first two were; the third was not. `@angular/service-worker` was an installed
   dependency that nothing used — no `ngsw-config.json`, no `serviceWorker` build option, no
   `provideServiceWorker` — and Chrome does not offer to install a PWA without one. It is wired up
   now, gated on `useEmulators` rather than `isDevMode()` so the emulator-backed E2E build does not
   register a worker, and asserted in `apps/web/e2e/pwa.spec.ts`.
4. ✅ Empty, loading and error states on every view, asserted in the E2E suite.
5. ✅ A failed job shows the worker's own reason — "this video is age-restricted" — rather than a
   stack trace, which is what the Phase 3 taxonomy was for.
6. ✅ A clip with no `playbackUrl` and no reachable local server renders as poster-plus-metadata with
   an explicit "playable on the worker machine" affordance, asserted in E2E.
7. ⏸ **`v0.1.0` not tagged**, pending items 1 and 3.
8. ❌ **FCM push was never built.** It is listed in this phase's scope and no exit criterion tested
   it, which is exactly how it went unnoticed until the deployment review in Phase 8. There is no
   messaging code in either the worker or the PWA. Carried to Phase 11 with the rest.

Items 1, 3 and 7 are consolidated into [Phase 11](#phase-11--open-source-hardening), which is where
everything blocked on a real account or a real device now lives.

**Delivered.** The worker's read-only local file server on 127.0.0.1 · Angular PWA with Google
sign-in, a jobs view with live per-stage progress, and the review queue · the three-tier playback
precedence · PWA manifest and icons · Playwright E2E against the emulator, wired into CI.
The service worker landed later, during the Phase 8 deployment work.

**Two things worth recording.** The E2E suite found a genuine isolation defect in itself on first run:
a stray worker holding port 8765 made the "no playable URL" test fail, because the probe *correctly*
found a server. The local-server origin is now pinned per test rather than depending on what happens
to be running. And the review page originally created an Angular `effect` inside another `effect` —
which is invalid, and is the natural shape for a watcher that re-subscribes when the user changes. The
store now delivers through callbacks so the component needs exactly one effect.

**Risks.** *iOS PWA push requires home-screen installation and has its own quirks* → verify on the
actual target device early in the phase; fall back to an in-app notification plus email if it proves
unreliable, and say so in the README.

---

### M3 — Feedback loop

#### Phase 8 — Publishing, with a rights gate → *toward v0.2.0*

> **The rights gate described below was removed on 2026-09-14** — see
> [ADR-0020](adr/0020-removing-the-rights-attestation.md). What survives is the rest of this
> phase: the PUBLISH job type, worker-held credentials, the resumable upload, the audit record,
> and the two conditions that were always load-bearing — publishing off by default, and nothing
> published that a person has not approved. The gate is left described here because this section
> is the record of what was built and what it taught — including the Content ID claim below,
> which is half the reason it went.

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

**Exit criteria** — met, 2026-09-09; criterion 1 verified on a real channel 2026-09-12

1. ✅ **A real upload reached a real channel**, twice — `PDvHibmo34k` on 2026-09-11 and
   `e16x3LA05W8` on 2026-09-12, both through ClipForge's own publish path, both carrying a
   `PERMISSION_GRANTED` attestation and both still on the channel as unlisted.

   *Corrected 2026-09-14.* This criterion read "unverified" for three days after it had been met.
   The record was in Firestore the whole time and nobody looked — which is worth more than the
   correction itself, because the same habit is what let a service worker be signed off without
   existing in Phase 7. **A criterion that can be checked by reading the database should be
   checked by reading the database, not by remembering.**

   What those two uploads then taught, which no test could have:

   - **One was claimed by Content ID.** The rights basis recorded was `PERMISSION_GRANTED`, and
     the claim landed anyway. That is not a contradiction and not a bug: an attestation is the
     operator's assertion of why they believe they may publish, and Phase 8 built it as an audit
     trail rather than a shield. Content ID matches the *picture*, and no attestation changes what
     the picture is. [Phase 8b](#phase-8b--the-correction-channel) says exactly this about
     re-voicing — "it does not make footage safe to publish" — and this is the first evidence from
     a real channel that it was right to say so.
   - **The other went out with its title in French**, quoting the transcript:
     *"On voit la passe de Michael Olysee, magnifique"*. That is precisely the defect
     [Phase 8e](#phase-8e--looking-at-the-clip-before-speaking-about-it) was built to fix, and it
     shipped on 2026-09-13 — **one day after** this clip was published. The operator withdrew the
     clip from consideration over it. The fix exists and is tested; what has *not* happened is a
     publication made after it, so Phase 8e's value is still unproven where it counts.
2. ✅ Enforced twice, tested independently: 17 pure-function tests in
   a dedicated `test_rights.py` (both since removed — see ADR-0020), and 15 emulator tests in
   `firebase/tests/publishing.rules.spec.ts`. Neither copy is redundant — the worker uses the Admin
   SDK and bypasses rules entirely, and a rule is the only thing that can stop a client enqueueing
   publish work in the first place. A third, *advisory* copy lives in the PWA
   (the PWA's own copy, now `apps/web/src/app/core/publishable.ts`) so a refusal is explained in
   the interface instead of failing
   opaquely after the button is pressed.
3. ✅ Asserted three ways, by counting videos on a fake platform across a crash: interrupt
   mid-upload and resume the checkpointed session; find a PUBLISHED record and skip; and the case
   naive implementations get wrong — a previous attempt whose acknowledgement was lost, which is
   reconciled by *asking the platform* rather than guessing.
4. ✅ `test_no_oauth_token_reaches_firestore` runs a full publish with a real token on disk, then
   walks every document the emulator holds, subcollections included, asserting the token does not
   appear. Written as an explicit walk rather than a collection-group query, because the latter would
   only search the subcollections someone remembered to name.
5. ✅ Every publish writes a `Publication` carrying the attestation **copied at upload time**, not
   referenced — so a later edit to the clip cannot rewrite the reason a past upload happened. Readable
   by the owner from the phone.

**Delivered.** The rights gate in all three places · `PublishStage` and the `PUBLISH` job type ·
worker-held OAuth with PKCE, `clipforge-worker youtube-auth`, and an encrypted token store ·
the YouTube client with resumable upload and a quota ledger · `PublicationStore` and the audit trail ·
the PWA publish queue · `clipforge-worker publish` and `quota` · a `publishing` check in `doctor` ·
[ADR-0010](adr/0010-worker-held-publishing-credentials.md).

**Scheduling, and why it is one line.** `publishAt` became `Job.notBefore`, consulted by
`lease.is_due()` — the single predicate every claim path already used. A separate scheduler would have
been a second authority on when a job may start, and the two could disagree. A consequence worth
stating: a scheduled job whose worker died still waits for its time rather than going out the moment
the reaper notices it, which is the behaviour scheduling exists to guarantee.

**Three defects this phase surfaced, all pre-existing.**

- **`RenderStage` was never registered.** It was implemented and unit-tested in Phase 6 but absent
  from `build_clip_stages`, so no job could ever reach it.
- **`clipforge-worker submit` created one-stage jobs.** `build_clip_stages` emitted only the stages
  whose machinery the caller passed in, and `submit` passes almost none — so a job submitted from the
  CLI downloaded a video and stopped. The pipeline's *shape* is now a single declaration
  (`CLIP_PIPELINE`) separate from the code that builds runnable stages, with an assertion that the
  two cannot drift.
- **The rules tests raced each other.** Every spec shares one emulator project and calls
  `clearFirestore()` between tests; run in parallel, one file's wipe deleted another's fixtures
  mid-test. It surfaced as a rules `get()` returning null, which reads exactly like a broken rule and
  was not one. Latent with two spec files, reproducible on the third.

**Risks.** Two real ones, both worth documenting in the README:

- *OAuth consent screen in "Testing" mode expires refresh tokens after 7 days.* The YouTube upload scope
  is **sensitive**, so moving to production requires Google verification. Build for the
  re-auth-every-7-days path first, and document verification as the production route.
- *Quota.* The default YouTube Data API quota is 10,000 units/day and an upload costs **1,600 units** —
  roughly **six uploads per day**. The scheduler must budget quota and surface it in the UI rather than
  failing opaquely on the seventh upload.

---

#### Phase 8b — The correction channel

**Goal.** A reviewer who thinks a clip was made wrong can say so and get a better one.

**Why it jumped the queue.** Not planned here; it arrived from use. Reviewing football clips surfaced
two complaints that no existing screen could answer, and both were structural rather than cosmetic:

- **The framing loses the ball.** `RENDER` took one 9:16 window, anchored by the render profile, and
  held it for the whole clip. On a 1920-wide broadcast frame that keeps 608 pixels — 31.6% of the
  width, measured in `tests/integration/test_framing_renders.py` — and it never moves. Worse, `crop`
  was a field of the *profile*, which is one global setting: changing it for a football clip changed
  it for every talking head too.
- **The voice and language cannot be changed.** Planned for M6 ([Phase 13](#phase-13--the-synthesis-spine--v050)),
  gated behind M5, and therefore a long way off.

Underneath both: there was no feedback channel at all. Approve, reject, retitle, score with music,
publish — every one of those accepts the clip as rendered.

**In scope**

- A `REMAKE` job type and a `RemakeOptions` contract, following `MUSIC` exactly: it names a clip, it
  produces a *new* clip carrying `derivedFromClipId`, and it never alters the one that was reviewed.
- **Framing as a per-clip decision**, with the three answers that are actually different —
  `FIT` (the whole frame, nothing croppable), `TRACK` (a window that follows the motion), `PAN` (a
  window the reviewer moves), plus `AS_RENDERED` for today's fixed crop, now overridable per clip.
- **`clipforge.media.tracking`**: a motion-saliency pass over the source, ffmpeg to numpy, no new
  dependency and no model. It scores window *positions* rather than taking a motion centroid, and it
  is rate-limited so it lags the action rather than whip-panning to meet it.
- **Narration** behind a `SpeechSynth` port, Kokoro-82M on onnxruntime as the first adapter — the
  ONNX build rather than the PyPI one, which depends on the PyTorch this project does not have
  ([ADR-0002](adr/0002-ctranslate2-without-pytorch.md)). This is D11 arriving early, on the CPU lane,
  never touching the broker.
- **Translation and note-reading** on the Ollama model `ANALYZE` already uses. No second model class.
- Caption `REBUILD`: transcribe the generated narration *back* to recover word timings that match
  what was said rather than what was scripted, then feed `build_ass` unchanged. D-level machinery
  from Phase 13, built here because a new voice makes the burned-in captions a lie.
- The remake panel on the clip page, and `clipforge-worker remake` / `fetch-voices`.

**Explicitly out of scope.** A real object detector for tracking — that is a model, a VRAM budget and
a third broker-managed class, and `plan_track` is the seam it goes behind if it ever pays. Generated
visuals, scripts and briefs: those are M6 and stay there.

**Exit criteria** — met, 2026-09-12; one item is honestly outstanding

1. ✅ Each framing mode does what it claims, measured rather than inspected. The source is a
   horizontal gradient, so brightness encodes horizontal position: a left window comes back dark, a
   right window bright, a pan gets brighter over time, and `FIT` spans the full gradient because it
   crops nothing. 12 tests in `tests/integration/test_framing_renders.py`, plus 28 pure-function tests
   on the filtergraph strings themselves — a wrong crop expression does not fail, it renders the wrong
   third of the pitch.
2. ✅ Tracking follows a moving subject into the rendered clip, settles on a stationary one, reports
   still footage as nothing-to-follow rather than guessing, and honours its speed limit.
   Three bugs surfaced here and all three were invisible in a rendered clip: ties broken leftward
   pinned a stationary subject to the edge of frame; a silent instant voted for dead centre, so the
   crop drifted to the halfway line every time play stopped; and edge-padding was needed to stop the
   smoothing pass sagging at both ends of a short clip.
3. ✅ The picture is never retimed to fit the narration. A translated script routinely runs 20-30%
   longer; the overrun is recorded on the clip and shown in the UI, and the video is untouched.
4. ✅ A note the reviewer writes is read into settings, never overrides a setting they stated, and
   never touches a topic the note was not about. `tests/gpu/test_remake_notes.py`, against the real
   model — the shape of `LlmRemakeNote` is the record of two simpler shapes that each failed in their
   own direction (see [ADR-0013](adr/0013-remake-as-a-job.md)).
5. ✅ Refusals name the alternative. Reframing needs the source, which the workspace collector
   reclaims; re-voicing does not, because it copies the video stream. A remake that cannot reframe
   says so *and* says that a voice change would still work.
6. ✅ **Kokoro runs end to end**, verified 2026-09-12 after installing the extra and fetching the
   model: 54 voices load, English and Spanish both synthesise, and three real remakes — tracked
   reframe, fit-plus-Spanish-narration, and voice-only — each produced a valid 1080x1920 clip with
   audio. This was outstanding at the time the phase was written and is no longer.

**One defect this phase surfaced, and it was not in this phase's code.**

`extract_poster` read its dimensions through the strict media probe, which refuses a file with no
duration. ffmpeg picks a demuxer per file — a detailed JPEG is read by `image2` and reports a
nominal 0.04s, a plain one by `jpeg_pipe` and reports `N/A` — so the poster measured successfully or
not *according to how busy the frame happened to be*, and on failure reported a height of 0.
`ClipPreview` requires positive dimensions, so a render that had already finished then died saving
its thumbnail. A flat green pitch reproduces it; the test suite's colour-bar pattern does not, which
is why it survived. Fixed at the root — the poster helper asks ffprobe for width and height and
nothing else — with a regression test on plain footage, plus a guard in both RENDER and REMAKE so an
unmeasurable poster costs a thumbnail rather than a clip.

**Delivered.** `RemakeStage` and the `REMAKE` job type · `clipforge.media.framing` (now the single
crop implementation, with `RENDER` delegating to it) · `clipforge.media.tracking` ·
`clipforge.media.speech` and `clipforge.media.narration` · `clipforge.analysis.remake` ·
the remake panel on the clip page · `remakeOptionsOk` in the rules, with 27 emulator tests ·
`clipforge-worker remake` and `fetch-voices` · [ADR-0013](adr/0013-remake-as-a-job.md).

**A note on what re-voicing is for.** It changes the soundtrack and nothing else. On third-party
footage the picture is still the picture, and it is the picture a rights holder's matching runs
against. It helps with a claim on commentary or music, and it opens a clip to an audience that does
not speak the original language; it does **not** make footage safe to publish. The rights attestation
is what does that, and the review screen says so where the option is offered.

**What this changes about M6.** Three of Phase 13's pieces now exist: the `SpeechSynth` port and its
Kokoro adapter, the transcribe-our-own-narration alignment trick, and the CPU-lane discipline for
both. Phase 13 inherits them rather than building them, and `COMPOSE` should reuse `SpeechSynth`
rather than introducing a second path to a voice.

---

#### Phase 8c — One clip, and a system that remembers

**Goal.** Stop the queue filling with versions of the same clip, and stop the reviewer typing the
same correction every time.

**Why it jumped the queue.** Both came from using 8b for an afternoon. Thirteen rows in the review
queue were nine actual clips — a remake is a new clip, a remake is always PENDING, and its parent
usually still is too. And two of the first five corrections carried the same two asks, written out
longhand both times, because nothing in the system could notice.

**In scope**

- `Clip.lineageId` and `Clip.version`: a clip and every correction of it are one row in the queue,
  showing the latest, with the rest as history carrying each step's note, reading and refusals.
  `MUSIC` joins the same lineage.
- A `Preference` contract and the `preferences` collection: what the system has worked out about a
  reviewer, **proposed and never applied** until accepted.
- `clipforge.analysis.preferences`: propose after a remake that carried a note, dedupe against
  everything already held in any status, feed accepted ones into the note prompt and into the
  remake's defaults.
- Rules giving the PWA exactly one power over a preference — the decision — and the review-queue UI
  to exercise it.

**Explicitly out of scope.** Applying a preference without being told to. Inferring preferences from
approve/reject decisions rather than from notes — a rejection says *no*, not *why*, and that kind of
statistical inference is Phase 9's business, over published performance.

**Exit criteria** — met, 2026-09-13

1. ✅ The live queue collapses from 13 rows to 9 on the existing clips, and the two lineages with
   three and four versions each show one row apiece. Backfilled by walking `derivedFromClipId`.
2. ✅ A note teaching a standing preference produces one; *"it cuts in three seconds too late on
   this one"* produces none; something already accepted and something already rejected both produce
   none. Measured against the real model in `tests/gpu/test_preference_learning.py`.
3. ✅ Nothing is applied without a decision. 11 emulator tests in
   `firebase/tests/preferences.rules.spec.ts` hold the write surface to `status`, `decidedBy` and
   `decidedAt` — no creating, no editing the wording, no deleting a rejection.
4. ✅ An accepted preference fills only what the current request left open, and the most recent of
   two that disagree wins.

**The shape lesson, paid for twice.** `LlmPreferenceProposal` first asked only for a list of
preferences, and qwen3.5:4b returned an empty one on every case measured, including a note that
plainly taught two things — an empty array satisfies an array schema trivially, so it is the
cheapest answer available. Making it answer a required boolean and justify it *before* the list
exists fixed it. That is the same failure `LlmRemakeNote` records about nullable fields. **Where a
schema offers a lazy path, a small model takes it**, and prompting does not fix what the shape
permits.

**Borrowed from `sarungano`.** The propose-never-apply loop, the dedupe-against-every-status rule,
and the scoping of a lesson to one work versus all of them come from that project's chapter
feedback loop, which solves the same problem for adapted prose. See
[ADR-0014](adr/0014-learning-from-feedback.md).

---

#### Phase 8d — Hiding what the broadcaster burnt in

**Goal.** Cover a channel bug, a score bar or a watermark, find them without being told where they
are, and remember them against the channel so nobody has to ask twice.

**Why it jumped the queue.** A reviewer asked for the same thing three times in three separate
remakes. Every time the note was read correctly, filed as `UnsupportedAsk.REMOVE_WATERMARK`, and
answered with a clip that still had the Canal+ logo on it. The refusal was honest and useless: a
pipeline that cuts shorts from broadcast footage and cannot cover a channel bug produces clips that
cannot be published, which makes this a missing floor rather than a missing feature.

**In scope**

- `clipforge.media.obscure`: find regions that hold still while the rest of the frame does not, and
  build the filtergraph that hides them — `delogo` for a mark small enough to reconstruct, blur or
  mosaic for anything larger, a flat box when flat is the point.
- `ObscureRegion` in percentages of the **source** frame, applied at the head of the graph before
  any crop, so a tracked window does not drag the blur across the picture.
- `RemakeOptions.obscure` and `AppliedRemake.obscured`: ask for it, and see exactly what was hidden
  and where each rectangle came from.
- `NoteTopic.OBSCURE` and `NoteObscure`, plus **absorbing** `REMOVE_WATERMARK` and
  `REMOVE_OVERLAY_TEXT` rather than refusing them.
- `Source.obscure`, applied by RENDER: the rectangles become a property of the channel, so the next
  clip arrives clean instead of arriving wrong.
- `propose_obscure`: the one lesson written without consulting the model, because its value is its
  coordinates.

**Explicitly out of scope.** A rectangle editor in the PWA. The regions are percentages of the
source frame and the app never sees one — media does not leave the worker, and the poster it does
see is the finished 9:16 clip, already cropped out of those coordinates. Worth building only if
detection turns out to miss.

**Exit criteria** — met, 2026-09-13

1. ✅ On the reviewer's own football source, detection finds the Canal+ bug at 0.98 confidence on
   one cut and 0.96 on another, plus the score bar and the competition clock, with no false
   positives. The rendered frame has the logo gone.
2. ✅ All four methods leave nothing readable, and the rest of the frame is byte-identical. 14
   integration tests against real ffmpeg in `tests/integration/test_obscure_renders.py`.
3. ✅ A note saying "blur the canal+" turns detection on by both routes — the OBSCURE topic and a
   model that still files it as impossible — and is not also refused.
4. ✅ 17 emulator tests hold the write surface: six regions at most, geometry inside the frame, no
   unrecognised field, and exactly one writable field on a source.

**Two things the first real render taught.** `delogo` over a 30%-wide score bar drew a smear more
conspicuous than the graphic, so the limits are on the sides as much as the area. And a match
clock's digits change every second, so only the badge beside them is static — detection covered
the badge and left "34:37" in the open, which reads as a fault. Two finds at the same height with a
small gap are now treated as one plate.

**The bug a test measured rather than read.** Pixelation scaled down with `flags=neighbor`, which
samples one pixel per cell instead of averaging, so a mosaic kept whichever bars it landed on and
the mark stayed legible while the filtergraph looked correct. See
[ADR-0015](adr/0015-hiding-what-is-burnt-into-the-picture.md).

---

#### Phase 8e — Looking at the clip before speaking about it

**Goal.** Stop the pipeline narrating and naming footage it has never seen.

**Why it jumped the queue.** A reviewer's clip went out captioned *"7,000 flaps go. She is
magnificent one and"*, titled with a lowercase French transcript fragment, and described with the
analyst's note explaining why the window had been selected. Every component was working correctly;
none of them had seen a football.

**In scope**

- `clipforge.media.vision`: three frames to a multimodal model, described plainly — subject, what
  happens, text on screen. Optional everywhere, None on any failure.
- `clipforge.analysis.narrate`: the spoken line is **written** from the transcript and the pictures
  together, not translated. `reads_as` counts function words to verify the language, because a model
  that has just produced French reports that it produced English.
- `clipforge.analysis.metadata`: a title, a description and tags written for a feed, in the clip's
  own language. RENDER writes them; REMAKE rewrites them whenever the voice changes.
- Grounding: every word of a tag must trace to the material or to a short generic list; ungrounded
  words in prose are reported on the clip rather than removed.

**Explicitly out of scope.** A vision pass per candidate during a harvest — a minute each is not
affordable for a dozen clips, and the reviewer who corrects one gets the grounded version then.

**Exit criteria** — met, 2026-09-13

1. ✅ Both real cuts from the reviewer's match produce coherent English. The word-salad one becomes
   *"A beautiful left-sided cross from the corner"*; the good one becomes *"Pavlovitch makes a good
   pass to break through the first line."*
2. ✅ French returned as English is refused by `reads_as`, which is the bug that shipped.
3. ✅ `laliga`, `bayer leipzig`, `lck`, `liverpool vs bayern` and `diaz real madrid` are all dropped
   from tags; `bayern munich`, `kane` and `futbol` survive.
4. ✅ A description naming a competition the material never mentions is flagged by name on the clip.

**The lesson, again.** The prompt forbade every one of those invented tags in as many words and the
model produced them anyway. Prompts do not fix what the shape permits, and they do not fix
fabrication either — only a check outside the model does. See
[ADR-0016](adr/0016-looking-at-the-clip-before-speaking.md).

---

#### Phase 8f — Tidying up, without losing anything

**Goal.** Make the database deletable from the web app, and the media deletable only from the
machine that holds it.

**Why it jumped the queue.** Everything accumulated and nothing could be removed: `clips`, `sources`
and `candidates` were all `allow delete: if false`. That rule was protecting the wrong thing — the
expensive artefact is the media, not the record, and the records were being guarded as though they
were the costly half.

**In scope**

- Delete on `clips`, `sources` and `candidates`; `deleteSource` cascades in batches from the client,
  counting the tree before it offers the confirmation.
- `clipforge.media.trash`: a bin at `workspace/trash/<id>/`, file plus manifest, no database.
  Restore refuses to overwrite; nothing empties it on a schedule.
- `clipforge.scheduler.storage`: `GET /storage` and the bin's routes on the loopback API, because
  nothing in Firestore knows what is on a particular disk.
- **Settings ▸ Storage** in the PWA: what is on the disk, what is in the bin, what each costs.

**Explicitly out of scope.** The Windows Recycle Bin. It auto-purges on size, which contradicts
"keep them forever" exactly when the bin is large enough to matter, and the app cannot list or purge
its own items there without shell APIs.

**Exit criteria** — met, 2026-09-14

1. ✅ A real clip moves to the bin over the local API, disappears from `clips/`, is listed with its
   original path, and is restored to exactly where it was.
2. ✅ A path outside the workspace is refused by name, as is `..` out of the workspace and a trash id
   whose parent is not the bin.
3. ✅ 187 emulator specs: clips, sources and candidates delete; publications and preferences do not;
   candidates still cannot be edited.
4. ✅ Binned bytes do not count against the disk budget, so the collector never evicts a live source
   to make room for a deleted clip.

**The decision worth remembering.** The bin sits outside `Workspace.used_bytes` on purpose. Counting
it would be more truthful about the disk and catastrophic in practice, because `collect` evicts
sources — live data would be deleted to house dead data. The price is that the bin can fill a disk
while the workspace reports itself comfortable, which is why its size is shown next to the button
that empties it. See [ADR-0017](adr/0017-deleting-a-record-is-not-deleting-a-file.md).

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

**Exit criteria** — built 2026-09-14; four of five met, and the fifth needs one re-authorisation

1. ⏸ **A published clip accrues daily metric snapshots without gaps.** Not yet met, and no longer
   blocked on publishing: two clips are on the channel, `PDvHibmo34k` and `e16x3LA05W8`. What
   stands between here and this criterion is one consent screen — the stored token holds
   `youtube.upload` and `youtube.readonly` and not `yt-analytics.readonly`, confirmed against the
   live token store, so `poll-metrics` refuses by name until somebody re-runs `youtube-auth`.

   Everything on this side of that is built and tested: the poller zero-fills every day the API did
   not mention, so a quiet Tuesday is a Tuesday of zeroes rather than a missing one, and
   `MetricStore.missing_days` names the gaps rather than counting the snapshots — a publication
   with 20 of 25 days looks healthy until somebody asks *which five*.

   **Expect very little to measure.** Both clips are unlisted, which is the correct default and
   also means near-zero organic traffic, and YouTube withholds the retention curve entirely below a
   privacy threshold of a few hundred views. So the first real poll will almost certainly produce
   rows of small numbers and no curves at all. That is the system working: `n` will report what
   survived rather than inventing measurements, and the report will say it cannot conclude anything.
   The criterion this closes is *"snapshots arrive daily and without gaps"*, which is answerable on
   two unlisted videos. Whether the scoring predicted anything is not, and will not be for a while.
2. ✅ The dashboard renders retention curves and every cohort breakdown, at `/insights`. Curves are
   inline SVG on **one shared scale**, so two clips can be compared — per-curve normalisation would
   draw every clip as identically well retained, which is the opposite of the point, and it is one of
   the two mistakes `rollup.spec.ts` exists to catch.
3. ✅ The calibration report states the correlation whatever it is, with its 95% interval and its
   sample size, and says in words when the interval contains zero. Proven by planting noise and
   checking it is reported as noise, and by planting a perfect relationship *below* the threshold and
   checking no conclusion is drawn anyway.
4. ✅ `clipforge-worker rescore` re-ranks every historical candidate under new weights with no model
   call at all — decision D5 collecting on the promise it made in Phase 5. It prints the movement
   first and needs `--apply` to write, and it writes `total` and nothing else.
5. ⏸ **`v0.2.0` not tagged.** Gated on criterion 1: the version that closes the feedback loop should
   not be cut before the loop has closed once. It is now one consent screen and one poll away.

**Delivered.** `clipforge.analytics` — `youtube` (Analytics API v2, its own scope, sharing the
publishing quota ledger), `cohorts`, `calibration`, `report` and `poller`, of which three are pure and
need no account · `MetricSnapshot`, `RetentionPoint`, `ScoreWeights`, `CohortStat`,
`CalibrationCorrelation` and `CalibrationReport` in the contracts · `MetricStore` and
`CalibrationStore` · `poll-metrics`, `calibrate` and `rescore` · the **Insights** page ·
read-only rules over `metrics` and `calibrations` with 16 emulator specs ·
[ADR-0019](adr/0019-reporting-a-result-we-do-not-have-yet.md).

**The scope is new, and somebody has to re-authorise.** `yt-analytics.readonly` is not implied by
either scope publishing already holds, so a token minted before this phase is refused — by name,
against the recorded scopes, with the command to run, rather than as a 403 from a poll three days
later. A token with *no* recorded scopes is let through: tokens predate the scope list, and refusing
those would break a working setup to guard against a hypothetical one.

**Quota is shared with publishing on purpose.** A report query costs about one unit against an
upload's 1,600, so the poller will not exhaust an allowance — but it could still take the last units
on the day a clip needed publishing, and publishing is the side that cannot simply run again in an
hour. Google meters the two APIs separately in the console; sharing one ledger is conservatism, not a
claim about their billing, and if they are wholly independent the only cost is polling slightly less
aggressively than necessary.

**Seven bugs, and a different thing found each one — which is the point.**

A review of the finished phase found four more, and all four were one mistake in
different clothes: **a number presented as measured that was actually assumed.**
`--window` was decorative, so the report named a window in its header and
computed over all history; a zero-filled day was written as *settled*, so one
dropped row became a permanent fabricated zero that `missing_days` then reported
as no gap; a cohort mean was withheld on the bucket's size rather than on how
many clips actually contributed a value, so a bucket of five with one retention
figure published it as five clips' worth; and the dashboard's `orderBy('date')`
with a limit kept the *oldest* two thousand snapshots, so past that it would
freeze on the first clips ever published and silently never show a new one.

Not one of those would have thrown, failed a test, or looked wrong on screen.
Each produces a plausible number. That is exactly the failure this phase exists
to resist, and its own first draft contained four instances — which is the best
argument available for why the honesty rules here are enforced in code and tests
rather than left to intention. All four now have tests.

**Three earlier ones, each found by a different thing.**

`The analytics queries were scoped by uid.` Every one of them — publications, snapshots, reports,
candidates — filtered on the reader's own account. This repository had already made and corrected
exactly that mistake in the review queue, where the note reads *"filtering here was what made one
system look like two"*, and it was made again anyway three screens later. Reading the live database
is what caught it: **the two clips this project has published went out under two different
accounts**, so `poll-metrics` would have measured one video, skipped the other, and reported a
complete-looking dashboard of half a channel. No test would have found it, because every fixture in
the suite used one uid — the bug was invisible to a test suite that shared the assumption. All four
queries are workspace-wide now, matching the rest of the app, and the integration test seeds two
accounts on purpose.

**Two more, and only one kind of test could find each.**

`Firestore has no date type.` `MetricSnapshot.date` is a calendar day and the client raises
`TypeError` on `datetime.date`. Every unit test passed throughout — they compare documents as plain
JSON, which is exactly what makes them fast and exactly what makes them blind to this. The emulator
found it on the first run. Dates now convert to their ISO string at the store boundary, with the
`datetime` check first, because `datetime` *is* a `date` and without that ordering every timestamp in
the system would flatten to a day.

`"first" and "last" are not superlatives.` The hook classifier filed *"Pavlovitch breaks the first
line"* as SUPERLATIVE. In football commentary "first half", "last man" and "the first line" are
positional language, and a plain description of a pass was being counted as a hyped one. Found by
writing the test case from a real clip rather than from the word list. The classifier stays
English-only, which is a genuine limitation now that Phase 8b produces Spanish and French narration —
so the report prints that caveat above the breakdown rather than letting it read as a finding about
hooks.

**Risks.** *Sample size.* Ten clips support no conclusions. Handled by making the sample gate the
*claim* rather than the computation: coefficients are always shown, and below n=20 every
interpretation says the sample cannot support a conclusion — including when the coefficient is 1.0,
which the suite asserts. No weights are fitted below n=40. Both are module constants, not parameters,
because moving a threshold after seeing a result is not the same act as choosing one before.

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

> *Nothing runs without a human trigger* is read, since ADR-0023, as: nothing that produces a clip.
> A person may set a research run on a timer; what the run produces is a list, and every step from
> the list is still a press.

**Delivered** (2026-09-19), and run once for real against the live feeds on the emulator: 61 signals
from three providers, 8 rows ranked in under a minute (most of it looking up the videos Reddit
linked), all 8 explained by the model in 37 seconds. That
run rewrote two scoring weights before the day was out — see ADR-0021's last section. A `RESEARCH`
job type of two stages — `RESEARCH` on the CPU lane asks
the providers and ranks; `CURATE` on the GPU lane puts each row to the local model for an angle and
a relevance score, and degrades to `SKIPPED` without one — and a `trends/` collection the worker
writes and a person decides about. The Trends page asks, lists, and offers two actions per video:
*Clip it* creates an ordinary `CLIP` job from the URL, and *+ Compile* gathers it for Phase 10b.
See [ADR-0021](adr/0021-trend-research-as-a-job.md).

Two divergences from the scope above, both deliberate. **No YouTube Data API.** It needs a key and
draws on the quota publishing already budgets; the three providers built — Google Trends' daily
RSS feed, Reddit's Atom feeds, and a view-sorted, date-filtered YouTube search through yt-dlp —
need no key at all, and each sits behind one `TrendProvider` port so the Data API is one adapter if
a key is ever worth asking for. **"Channel size" is recorded, not scored.** Follower counts arrive
from the full YouTube lookup and are kept on the hit; the score weights agreement between providers,
strength, recency, and whether there is video to cut. Channel size has no evidence behind it yet as
a predictor, and Phase 9's posture applies. The exit criteria hold as written: the run is manual, the
list is ranked, promoting creates a normal job, and the worst case is a bounded number of requests
— twelve topics, ten lookups each, thirty rows — reported per provider on the stage's checkpoint.

**And on a schedule** (later the same day). A `ResearchSchedule` document — name, cadence, the same
options as a manual run — that the worker fires on its own while it is running, creating an
ordinary `RESEARCH` job marked with the schedule's id. Two cadences: every N hours (six to a
week) or daily at a time of day in the browser's zone. Firing is a transaction so two workers
cannot fire one schedule twice; a run still going is deferred rather than fired over; a worker
that was off fires each due schedule once on return. This is the first thing in ClipForge that
starts work unasked, and it starts the one kind of work that changes nothing — the list still goes
no further than the Trends page. See [ADR-0023](adr/0023-research-on-a-schedule.md).

**And a brief on every clip job** (2026-09-19). `ClipOptions` — instructions, a count, a length
range — on a `CLIP` job, from the Jobs page's *What to look for* panel and the CLI. The brief is a
filter: only moments that fit come back, and a job whose brief matches nothing finishes with no
clips and says so. Candidates chosen under a brief are stamped `v1-brief`. *Clip it* on the Trends
page passes the model's angle as the brief. See [ADR-0024](adr/0024-a-brief-on-the-job.md).

**Where, and what kind** (2026-09-19). The region became optional and the list became every
country Google Trends' feed answers for — 124, probed one by one — behind a searchable combobox;
null resolves to a new worker setting. A **category** joined it: a catalogue of 224 entries in 17
groups, held as data in the contracts package and rendered into both consumers under the same
staleness check as the schema. A category fills in what a run left blank — subreddits, YouTube
search terms — puts a hint on every feed-phrase lookup, and is told to the curator; it never
overrides the run's own words, and the feed it cannot steer (Google Trends ignores category) is
said so rather than pretended. See [ADR-0025](adr/0025-a-category-catalogue-as-data.md).

**And it fits a phone** (2026-09-19). The Compile page scrolled sideways on a phone, and so, it
turned out, did the Trends, publication, jobs and YouTube-settings pages once they held a real
URL, a file path or a one-word title. One cause, five places: a grid with only a breakpoint
column list has an implicit `auto` column below it, and an `auto` column grows to the longest
unbreakable thing inside it rather than clipping it — so a `truncate` that worked on a desktop
did nothing on a phone. The same for a `1fr` definition-list column, whose floor is its content,
and for an inline link with `truncate`, which clips nothing because overflow applies to blocks.
`e2e/responsive.spec.ts` now renders every page at 390px with that awkward content seeded and
fails on the symptom — the document being wider than the viewport, naming what sticks out — so
the next cause is caught whatever it is.

The gate — *Phase 9 producing evidence* — was settled in a way the plan did not anticipate. Phase 9's
honest output is "we cannot tell yet" for months, and what it is waiting on is volume. A trend list
does not automate the scorer; it feeds it.

---

#### Phase 10b — Compilation → *toward v0.3.0*

**Goal.** Several videos about one thing become one clip, with the best moment of each.

**In scope.** A `COMPILE` job type — `GATHER`, `SELECT`, `ASSEMBLE` — that ingests every item
through the CLIP pipeline's own ingest, transcribes each, asks the model for one moment per source
*for the theme*, renders each moment exactly as `RENDER` would, and joins them behind a two-second
title card. A basket on the Trends page and a Compile page that turns it into the job. Provenance
on the clip: every piece, its window, and whose idea the window was.

**Explicitly out of scope.** Narration over the compilation (the synthesis track's business);
remaking a compilation (there is no single source to re-cut from — the Compile page is the
correction channel); any transition fancier than a dip to black.

**Exit criteria.**

1. Two sources that differ in frame rate and sample rate join into one 1080×1920 file.
2. The result enters the review queue, plays, takes music, and publishes with no change to those
   stages.
3. A dead link among four produces three, and the clip says which was left out and why.
4. A crash after `GATHER` resumes without re-fetching; a crash inside `SELECT` resumes without
   re-transcribing.
5. Candidates chosen for a theme are stamped with their own prompt version and never rescored
   against a harvest's.

**Delivered** (2026-09-19). All five, the first four verified by test — criterion 1 in
`tests/integration/test_compile_assemble.py` against generated 25 fps/44.1 kHz and 30 fps/48 kHz
sources — and the fifth by construction (`THEMED_PROMPT_VERSION`). Criterion 2 is verified for the
queue and playback by the e2e and clip-page changes, and for music and publish by those stages
operating on the file and nothing else; **a real compilation through a real MUSIC and PUBLISH job
has not yet been run.** See [ADR-0022](adr/0022-compilation-as-a-job.md).

---

#### Phase 12 — Many channels → *toward v0.4.0*

**Goal.** One ClipForge, several channels with different content, without the
operator holding which-is-which in their head.

**Already done, deliberately.** The data model was built for this before it was
needed: `channels/{channelId}` is a collection, a `Publication` records the
`channelId` it went to, and a PUBLISH job carries `publishOptions.channelId`.
That was not speculative generality — it is the difference between adding a
channel later and migrating every publication record to work out where it went.

Since then the *resolution* half landed too, with the per-publish overrides:
`clipforge/publish/metadata.py` collapses per-publish, per-channel and per-install
preferences into one answer, the publish screen reads every channel rather than a
hardcoded id, and the picker renders as soon as a second channel document exists.
What remains for this phase is genuinely per-channel **credentials** and quota —
the parts that need a second token file and a second authorisation, not a second
code path.

**In scope**

- Several `Channel` documents, each with its own label, defaults and connection
  state; one marked `isDefault`.
- **Per-channel credentials on the worker.** Today's single token file becomes
  one per channel (`youtube-token-{channelId}.enc`). The client can be shared or
  separate; separate is cleaner and costs a second OAuth client.
- ~~Channel picker on the publish page, defaulting to `isDefault`.~~ **Done**
  — built with the per-publish overrides, and it appears only when there is
  more than one channel, so it costs a single-channel install nothing.
- Per-channel publish defaults, so a cooking channel and a tech channel get
  different tags and categories without editing either every time.
- Channel-aware review: filter the queue by intended channel, since a clip is
  usually *for* somewhere.

**The awkward part, stated up front.** YouTube quota is per **Google Cloud
project**, not per channel — 10,000 units a day shared across every channel
authorised through the same OAuth client. Six uploads total, not six each. Either
each channel gets its own Cloud project (more setup, independent quota) or they
share and the UI must show a *pooled* budget. The second is simpler and honest;
the first is what someone actually publishing daily will want. This phase should
support both and default to sharing.

**Explicitly out of scope.** Cross-posting one clip to several channels at once,
and any per-channel scoring or model tuning — that is Phase 9's business once it
has evidence.

**Exit criteria**

1. Two channels are connected and a clip publishes to the chosen one, verified by
   the video appearing on that channel and not the other.
2. Revoking one channel's authorisation leaves the other publishing.
3. Quota is reported per Cloud project with the pooling made explicit, so "six
   uploads" is never silently six each.
4. A publication written before this phase still resolves to a channel, rather
   than becoming an orphan.

---

### M5 — Release

#### Phase 11 — Open-source hardening

**Goal.** A stranger can run this, and a reviewer can understand it in five minutes.

**In scope.** Docker Compose for the worker, with GPU passthrough documented; a complete installation
guide; `.env.example`; the architecture diagram; a 60–90 second demo video; screenshots; issue and PR
templates; `SECURITY.md`; a public roadmap; an honest **Limitations** section (the VRAM ceiling, yt-dlp
fragility, YouTube quota, OAuth verification, rights responsibility); and a README rewritten to lead
with the §1 thesis.

**Carried here deliberately.** Four items were deferred from earlier phases rather than faked, and
they are consolidated here because they share one blocker — they all need real accounts, real
hardware in someone's hand, or a served build, and none of them can be satisfied by a test:

| Item | From | Status, 2026-09-10 |
| --- | --- | --- |
| The physical-phone review demo | 7 | **Unblocked, and now worth more.** Deployed at `bytepic-clipforge.web.app` with Auth enabled, and since Blaze the phone plays the clip rather than showing a still ([ADR-0018](adr/0018-blaze-and-a-short-lived-bucket-copy.md)). Needs doing, not building |
| A Lighthouse PWA audit | 7 | **Unblocked.** There is a served build with a real service worker |
| `v0.1.0` not yet tagged | 7 | ✅ **Tagged 2026-09-14**, on the deployed build. Cut without waiting on the two above: both test the *review experience* on real hardware, and neither can change what the tag points at |
| A real upload appearing on a real channel | 8 | ✅ **Closed.** Two clips published 2026-09-11 and 2026-09-12 through the app's own path. One was claimed by Content ID and one went out titled in French; both are recorded at [Phase 8](#phase-8--publishing-with-a-rights-gate) because both are findings, not mishaps |
| FCM push on job completion | 7 | Still absent. Never built; needs a VAPID key from the console once it is |

Three remain, and none gates the code: everything each one exercises is built and tested up to the
point where the real device begins. FCM is different and is marked differently — it is not
unverified, it is absent, and the honest reason it survived a phase sign-off is that nothing in Phase
7's exit criteria tested it.

The upload row is worth a second look now that it is closed. It sat in this table as "still blocked"
for three days *after* two clips had gone out, because the table was written from memory and the
evidence was two documents in Firestore. The lesson is [Phase 8's](#phase-8--publishing-with-a-rights-gate)
and it generalises: **a status that can be read from the system should be read from the system.**

**Exit criteria.** A clean machine reaches a rendered clip by following the docs alone; the README
states limitations plainly; every item in the table above is either closed or restated in the README
as a known limitation; CI badges are green; the repository is made public.

---

### The synthesis track — M6 to M9

**Gate: none of this begins until M5 is done and the repository is public.** Everything below assumes
Phases 9, 10, 11 and 12 have shipped. It is written down now, well ahead of being needed, for two
reasons: the research behind it is perishable — platform policies and API terms surveyed in September
2026 will have moved by the time the track starts — and three of its conclusions change decisions
*inside* the current plan, which is far cheaper to know now than to retrofit later (see §8).

**The thesis, in one sentence.** ClipForge harvests clips out of video that already exists; the tools
it would otherwise be compared against — AutoShorts, Faceless.so, Autopostr, Viibeo — synthesise video
that does not. That difference is one new pipeline, not a new product. Script, narrate, gather
visuals, assemble; everything from assembly onwards is already built and already tested.

#### The design volume, and why it decides almost everything

**3 to 7 videos a week.** Not a day. This is a personal, open-source project, and that number is a
deliberate ceiling rather than a starting point to grow out of. Most of what the commercial platforms
are architected for simply evaporates at that volume:

| At ~60 videos/month — their design point | At 12–30 videos/month — ours |
| --- | --- |
| Review is the bottleneck; batch approval is mandatory | One or two clips a day. The Phase 7 review screen is already enough |
| Auto-publish is the product | Auto-publish is never needed, and is **explicitly out of scope** |
| Posting must be automated or the product does not work | Manual upload is a few taps a day and costs nothing |
| Throughput per video is the metric | **Quality** per video is the metric. Spend the machine time |
| YouTube's ~6 uploads/day is a hard ceiling | It is roughly 6× more headroom than this plan needs |

The fourth row is the one that changes engineering decisions. Five videos a week is about half an hour
of machine time, which means the *expensive* option is affordable everywhere: write three candidate
scripts and keep the best, generate stills rather than settle for whatever stock returns, spend a few
pounds a month on an API-generated hero shot when one is worth it. The constraint stops being capacity
and becomes **ideas worth making a video about** — which is Phase 10's business, and raises its value
rather than lowering it.

#### What the hardware will and will not do

Measured against §2's budget — the same 5.4 GB, the same broker, the same two lanes:

| Component | Verdict | Cost here | Note |
| --- | --- | --- | --- |
| Script and planning | **Local** | ~3.4 GB VRAM | `qwen3.5:4b`, already pulled and already brokered. A 4B is weak at holistic judgement over a whole transcript — hence D6 — but strong at generating 250 words against a rubric |
| Narration (TTS) | **Local, CPU** | 0 GB VRAM | Kokoro-82M: 82M parameters, Apache 2.0, 54 voices, faster than real time on CPU. It never touches the broker. Chatterbox (MIT, ~0.5B) is the GPU upgrade if a cloned voice ever becomes the point |
| Caption timing | **Local** | ~1.6 GB, ~5 s | Transcribe *our own* narration to recover word timings, then feed `build_ass` unchanged. No new alignment dependency, and captions match what was said rather than what was scripted |
| Stock b-roll | **Local** | £0 + network | Pexels and Pixabay are free for commercial use, no attribution, monetisation permitted. Neither may be resold *as* stock, and identifiable people must not be made to imply endorsement |
| Generated stills | **Local** | ~5–6 GB, 8–15 s/image | Fits, but contends with Whisper and the LLM for the same budget, so it is a third **broker-managed model class**, never a stage that grabs VRAM behind the scheduler's back |
| Generated video | ❌ **Not on this card** | 8 GB+, minutes/shot | Wan 2.2's 5B GGUF wants ~8 GB even with offloading; LTX is the fastest open model and still measures in minutes per shot on a 3050. Viable as a *garnish* through an API, budgeted per series — never as the body of a video |
| Talking-head avatar | Deferred | mid-range card | Wav2Lip runs on consumer hardware and MuseTalk is near real time; LatentSync, the one that looks good, wants an A10 or 4090. Least load-bearing, most likely to look cheap. Not in this track |
| Generated music | Licence-gated | — | MusicGen's *output* is CC BY-NC — self-hosting does not launder a licence. ACE-Step and YuE are Apache 2.0. The `MUSIC` job already accepts a track from anywhere, so a curated library is the zero-risk default |

The single most useful finding: **narration and stock gathering both sit on the CPU lane**, so the
synthesis pipeline barely competes with `CLIP` for the GPU at all.

#### Decisions, taken now

Continuing the numbering from §3.1.

| # | Decision | Why |
| --- | --- | --- |
| D10 | **`COMPOSE` is a new job type**, not extra `CLIP` stages | A job's `stages` array is authoritative for its whole life. Bolting synthesis onto `CLIP` would give every harvest job stages it can never run. `PUBLISH` and `MUSIC` already set this pattern |
| D11 | **Narration runs on the CPU lane** | Kokoro is 82M parameters and faster than real time on 24 cores. Putting it on the GPU lane would serialise it behind Whisper for no gain |
| D12 | **Delivery, not posting**, for any platform we cannot authorise | See below. This is the decision that unblocks the whole track |
| D13 | **Generated scripts are scored by `RUBRIC_MAXIMA`** — the clip rubric, unchanged | It is the only thing that makes Phase 17 an experiment rather than two unrelated dashboards |
| D14 | **Licence-clean generators only** | Kokoro (Apache 2.0) and Chatterbox (MIT) in, XTTS v2 (CPML) and F5-TTS (CC BY-NC) out on licence rather than on quality; MusicGen out entirely. An open-source project cannot ship a pipeline whose output its users may not use |
| D15 | **No auto-publish, at any volume this plan targets** | One or two clips a day is not a queue that needs automating, and the human approval is precisely what YouTube's inauthentic-content policy rewards. Naming it here so it can be refused later |
| D16 | **The critic loop lives *inside* the `SCRIPT` stage** | The pipeline is a linear ordered list of stages with no branching, so "rewrite and try again" cannot be a stage that jumps backwards. It is a bounded loop inside one stage, checkpointing each attempt so a crash mid-loop resumes rather than restarting |

ADRs will be written for D10, D12, D13 and D16 when the phase implementing each begins.

#### D12 in full: why we deliver rather than post

Generating for every platform is easy. *Posting* to every platform is five separate app reviews.

| Platform | Route | What stands in the way |
| --- | --- | --- |
| YouTube | **Direct, working** | Nothing new. Quota is ~6 uploads/day pooled per Cloud project — 6× the headroom this plan needs |
| TikTok | Gated | `video.publish` (Direct Post) needs explicit approval beyond developer access; until audited an app may only post private/self-only. Two-step chunked upload, no native scheduling, and `creator_info` must be queried first so the creator's privacy, duet and stitch settings are honoured |
| Instagram / Facebook | Gated | Graph API, a Business or Creator account linked to a Page, and app review for content publishing. Reels eligibility is 5–90 s at 9:16; outside that it silently posts as a plain video |
| X, LinkedIn, Threads | Gated | Four more reviews, four more token lifecycles, four more sets of media constraints |

The commercial answer is an aggregator — upload-post at ~$16–24/mo, Blotato at ~$29/mo — which holds
the approvals so you never file for them. **We are not taking it**, for three reasons that all point
the same way: at 3–7 videos a week it is a subscription per upload that a human could perform in
thirty seconds; an open-source project should not require a paid third-party account to be usable from
a clean clone; and it would put a metered cloud dependency at the centre of a project whose entire
thesis (§1) is that the expensive parts run locally.

So the default for every platform we cannot authorise is a **delivery handoff**: the worker produces
the correctly-sized file, the caption written for that platform, and the list of disclosure toggles
that must be set by hand — and the operator uploads it. The aggregator adapter stays a named,
refusable option behind the same port, reconsidered only if volume passes roughly 150 posts a month or
manual upload becomes genuinely annoying. Direct TikTok and Instagram adapters land if and when those
approvals are ever granted, and nothing above them has to change when they do.

The part that makes this more than a download button is §M7's `AWAITING_MANUAL` state. A manual upload
that is never recorded is a hole in the experiment log exactly where most of the content went — so the
handoff is not complete until the operator pastes the published URL back, which gives `Publication`
its join key and keeps Phase 9 and Phase 17 whole.

---

### M6 — Synthesis

#### Phase 13 — The synthesis spine → **v0.5.0**

**Goal.** A sentence typed on the phone becomes a finished, captioned, narrated vertical video in the
review queue — with every *judgement* in it still made by a human.

**In scope**

- A `COMPOSE` job type and a `Brief` contract — hook, promise, payoff, audience, call to action —
  typed by hand at this stage. No model decides what the video is about yet.
- `NARRATE` on the CPU lane, behind a `SpeechSynth` port. Kokoro adapter first; the port exists so
  Chatterbox is an adapter later rather than a rewrite.
- `ALIGN` on the GPU lane, reusing the existing Whisper loader against the generated narration and
  feeding `build_ass` unchanged.
- `GATHER` on the CPU lane behind a `StockProvider` port (Pexels first), recording licence, author and
  source URL for every asset it takes — the asset library is an attestation surface, not a cache.
- `IMAGINE` on the GPU lane for generated stills, as a broker-managed model class (D11's sibling).
  The job's stage list is built from the brief's visual mode at creation time, so a stock-visual job
  carries no `IMAGINE` stage at all rather than carrying one that is skipped.
- `ASSEMBLE` on the CPU lane — a Ken Burns and shot-sequence filtergraph alongside the existing
  crop-and-caption one, deterministic to the same standard `render.py` already meets.
- An asset library with its own quota and GC, separate from the source/clip workspace.

**Explicitly out of scope.** Any model choosing the topic, the angle or the script (Phase 14); any
platform beyond the existing YouTube path (Phase 15); anything unattended (Phase 16); avatars and
lip-sync, permanently.

**Exit criteria**

1. A 45-second video composes end to end with no network call except the stock fetch.
2. It appears in the review queue with poster and filmstrip, and plays in the desktop shell.
3. Re-composing from the same brief and seed produces byte-identical output.
4. Every asset in the finished video carries a recorded licence and source.
5. A crash during any stage resumes without re-narrating or re-fetching.

**Risks.** The Ken Burns assembly is a new filtergraph and filtergraphs are wrong in ways you find
after a two-minute encode — so the plan is a value and unit-tested before ffmpeg ever sees it, exactly
as `media/music.py` already does it.

---

#### Phase 14 — The editorial chain → **v0.6.0**

**Goal.** The brief stops being typed and starts being argued for, by models that are allowed to say
no.

**In scope**

- `PLAN` and `SCRIPT` as prompt *roles* over one resident 4B, each with its own `promptVersion` and a
  schema-validated output. Chaining roles rather than weights is what makes this viable on 6 GB: a
  prompt swap is free, a model swap is a broker lease and a reload.
- The critic inside `SCRIPT` (D16): write → score against `RUBRIC_MAXIMA` → rewrite, bounded, with
  every attempt checkpointed and the best-scoring attempt retained.
- **Best-of-N rather than first-acceptable**, because at 3–7 videos a week we can afford to generate
  three and discard two. This is the single place where the low volume buys real quality.
- An optional larger second opinion — `qwen3.5:9b` on the CPU, outside the broker lease — for
  installs that want a harsher judge and have the cores to spare.
- Shot-list generation: script → per-shot visual prompts and stock query terms, so `GATHER` and
  `IMAGINE` stop guessing.

**Explicitly out of scope.** Fine-tuning anything; per-series prompt tuning (that needs Phase 17's
evidence); and any model with a licence that restricts commercial output (D14).

**Exit criteria**

1. Given only a topic, the chain produces a scored script.
2. Scripts below the floor are visibly rejected and rewritten rather than rendered — observable in the
   job event log, not merely asserted.
3. Every candidate carries the `modelVersion` and `promptVersion` that produced it.
4. A prompt change does not silently rescore history.
5. Best-of-N is demonstrable: three attempts recorded, one rendered, the scores explaining why.

**Risks.** **A 4B writes bland scripts.** This is the real quality risk of the whole track and it
cannot be settled before the phase runs. The hedges are the critic loop, best-of-N, prompt versioning,
and a cheap escape hatch to a larger model on the CPU lane. If all four fail, the honest outcome is a
documented limitation, not a louder prompt.

---

### M7 — Reach

#### Phase 15 — Delivery to every platform → **v0.7.0**

**Goal.** One approved clip reaches every platform it is meant for, at the right aspect ratio, with
the right caption and the right AI disclosure — by whichever route that platform actually allows.

**In scope**

- A `PublishTarget` port with two adapters: **`direct`** (YouTube, unchanged) and **`manual`** (D12).
  `PublishPlatform` grows beyond `YOUTUBE`; `Publication` gains `deliveryMethod` so analytics can tell
  a hand-upload from an API one.
- `PublicationState` gains `AWAITING_MANUAL`. The handoff card in the PWA offers the file, the
  caption, and a checklist of the toggles the operator must set in that app by hand; pasting the
  published URL back completes the record.
- Per-platform variants as **derived clips** — `derivedFromClipId` already exists for exactly this —
  rather than re-rendering from source. Instagram's 5–90 s Reels window is enforced here, not
  discovered after posting.
- Per-platform metadata: title, description and hashtags to each platform's conventions and limits.
- A `synthesis` provenance record on `Clip` — which of script, voice, visuals and music were
  machine-made, and the model and prompt version for each — written by the stages that did the work,
  never ticked by a human afterwards. Unlike the rights attestation
  ([ADR-0020](adr/0020-removing-the-rights-attestation.md)), this is a record of what the machine
  did rather than a restatement of a human judgement — which is why the same argument for removing
  that one does not apply to this.
- Disclosure derived from that record: YouTube's altered-content declaration, TikTok's AIGC label,
  Meta's label. **A publish whose provenance record is unset fails closed.**

**Explicitly out of scope.** The aggregator adapter (D12 — named so it can be refused); direct TikTok
and Instagram adapters until those approvals exist; auto-publish (D15).

**Exit criteria**

1. One approval produces correct deliverables for YouTube plus at least two manual platforms.
2. YouTube still publishes directly, with quota reported as before.
3. Every deliverable carries the correct AI disclosure, derived from the provenance record.
4. A publication left in `AWAITING_MANUAL` is visible as unfinished business, not silently lost.
5. Pasting a URL back completes the record with an `externalUrl` Phase 17 can join on.
6. A clip whose remote copy has expired can still be delivered — see the lifecycle risk in §7.

---

### M8 — Cadence

#### Phase 16 — Series → **v0.8.0**

**Goal.** The machine proposes the week's videos. A human approves them over coffee.

**In scope**

- `series/{seriesId}` — topic, persona, voice, render profile, target platforms, cadence and a
  monthly spend cap for any paid shot. The thing the commercial tools charge extra for, or only allow
  one of.
- An **inventory target** per series ("keep three approved clips ahead") driving job creation, rather
  than a cron that generates regardless of backlog. At this volume a cron would produce a queue nobody
  asked for.
- Publish times as `notBefore` on `PUBLISH` jobs — the existing mechanism (§3.3), no second scheduler
  that could disagree with the first.
- **Rotation as a functional requirement**: voice, hook structure, render profile and pacing vary
  across a series by construction. This is not polish — "generic, repetitive or template-based" is the
  exact language of the policy that demonetises channels, and rotation is the mechanical answer to it.
- A kill switch that halts in-flight work from the phone.

**Explicitly out of scope.** Auto-publish (D15, again, deliberately). Per-series model or prompt
tuning, which needs Phase 17's evidence first. Cross-posting one clip to every series.

**Exit criteria**

1. Two or three series run different formats concurrently at 3–7 videos a week combined.
2. A week's content is produced unattended and reviewed in a sitting.
3. Generation stops on its own when the inventory target is met.
4. Rotation is observable across a week's output rather than asserted.
5. The kill switch demonstrably stops in-flight work.

---

### M9 — Calibration

#### Phase 17 — One rubric over both kinds of content → **v0.9.0**

**Goal.** Find out whether any of it worked — on one rubric, across harvested and synthesised alike.

This phase **extends Phase 9 rather than repeating it.** Phase 9 built the machinery; this points it
at a second kind of content and asks whether the scores ever predicted anything.

**In scope**

- Metrics for manually delivered publications, not only API-published ones — which is what the
  `AWAITING_MANUAL` paste-back exists to make possible.
- Harvested versus synthesised on one leaderboard, which only works because D13 made them share a
  rubric.
- Rubric reweighting against outcomes, with historical scores preserved rather than overwritten — the
  warning in §8 applies here unchanged and with more force, since there are now two populations to
  confound.
- Series-level retirement: a format that stops earning attention stops earning GPU time.

**Exit criteria**

1. Every publication has metrics attached, whichever route it took.
2. A recalibration changes future scores without rewriting past ones.
3. The leaderboard can answer "is synthesised content earning its machine time" with evidence.
4. At least one format or series is retired on evidence rather than on taste.

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
| YouTube quota allows only ~6 uploads/day | Certain | Medium | ✅ **Addressed in Phase 8** — `QuotaLedger` refuses *before* an upload starts rather than failing on the seventh; `clipforge-worker quota` reports what is left | 8 |
| OAuth refresh tokens expire every 7 days in Testing mode | High | Medium | ✅ **Addressed in Phase 8** — a rejected refresh names the 7-day limit and the command that fixes it, and `doctor` reports the token's age before it expires; Google verification documented as the production path | 8 |
| Rights exposure on third-party source material | Medium | High | ✅ **Addressed in Phase 8** — attestation gate in rules *and* worker (the Admin SDK bypasses rules, so both are load-bearing), publishing off by default, every upload audit-logged with the attestation copied at publish time | 8 |
| A refresh token leaks through Firestore, a backup or a log | Low | **Critical** | Worker-held and encrypted at rest, never written to Firestore, never logged unredacted ([ADR-0010](adr/0010-worker-held-publishing-credentials.md)); asserted by a test that sweeps every emulator document | 8 |
| Firestore cost from chatty progress updates | Low | Medium | Throttle progress writes to ≥2s; batch event-log entries | 2, 7 |
| ~~**No Cloud Storage on Spark**, so no remote clip playback~~ — **closed 2026-09-10** | was Certain | Medium | Retired by moving to Blaze. The mitigation is what made it cheap: clips sat behind a `BlobStore` port and playback already resolved `playbackUrl` → local server → poster, so the fix was one adapter and one environment variable ([ADR-0018](adr/0018-blaze-and-a-short-lived-bucket-copy.md)) | 6, 7 |
| The 5-day bucket copy expires while a clip still matters | Medium | Low | The worker's copy is the master and does not expire; playback falls back to the local file server, and delivery re-uploads on demand ([ADR-0018](adr/0018-blaze-and-a-short-lived-bucket-copy.md)) | 7, 15 |
| A large transcript exceeds Firestore's 1 MiB document limit | Medium | Medium | Transcripts live on the worker; Firestore holds a `TranscriptRef` only | 4 |
| The local file server becomes an unauthenticated file-read surface | Low | High | Bind `127.0.0.1` only, read-only, every path confined to the workspace root; never bind `0.0.0.0` without adding auth first | 7 |
| Synthesised content demonetised as "inauthentic" | Medium | **High** | The human approval gate stays mandatory (D15); disclosure is derived from a provenance record rather than a checkbox; rotation across a series is a build requirement, not polish | 15, 16 |
| TikTok and Instagram never approve direct posting | **High** | **Low** — by design | D12 makes this cost a few taps a day rather than a capability. The direct adapters are additive if approval ever lands | 15 |
| A manual upload is never recorded, so the feedback loop has a hole where most of the content went | **High** without mitigation | High | `AWAITING_MANUAL` plus a paste-back that completes the `Publication`; unfinished deliveries stay visible as unfinished business | 15, 17 |
| The 5-day Storage lifecycle deletes a clip still waiting to be uploaded by hand | Medium | Medium | The local copy is authoritative and outlives the remote one; delivery re-uploads on demand rather than assuming `playbackUrl` still resolves. Local retention must exceed the longest expected manual delay | 15 |
| A licence-restricted model's output reaches a published video | Low | **High** | D14 is enforced as an allowlist in code, not a note in a doc — self-hosting does not launder CC BY-NC | 13, 14 |
| A 4B writes bland *scripts* — distinct from bland selections | Medium | High | Critic loop inside `SCRIPT`, best-of-N (affordable only because the volume is low), prompt versioning, and an escape hatch to a larger model on the CPU lane | 14 |
| The asset library outgrows the disk alongside sources and clips | Medium | Medium | Its own quota and GC, separate from `CLIPFORGE_WORKSPACE_MAX_GB`; 148 GB free on P: is the number to watch | 13 |
| Scope creep into "autonomous viral agent" | **High** | High | Every phase names what it explicitly excludes; Phase 10 is gated on Phase 9 evidence; the synthesis track caps itself at 3–7 videos a week and refuses auto-publish outright (D15) | all |

## 8. Immediate next actions

**Milestones M0, M1 and M2 are complete; M3 is built and waiting on evidence; M4's first two phases are built**
(2026-09-19). Phases 0 through 8f have met their exit criteria and Phase 9 has met four of its five, with five items consolidated into Phase 11 — three of which the
deployment work has since unblocked (see the table there). The pipeline runs end to end: a YouTube
URL becomes a transcript, a ranked set of candidates, a rendered vertical clip with captions and a
written title, a phone review with **video that plays on the phone**, a recorded rights basis, and
an unlisted upload — and a clip the reviewer disagrees with can be re-cut, re-voiced, re-framed and
have a broadcaster's logo covered, with the corrections learned rather than re-typed.

**Five phases arrived from use rather than from this plan** — 8b through 8f, all in three days, each
answering a complaint that reviewing real football surfaced and no existing screen could. They are
written up in full above; the short version is that the gap between "the pipeline works" and "the
clips are publishable" was five phases wide, and none of them were visible from here beforehand.

**It is also deployed and in use**, which the phases never covered because none of them asked for it.
Recorded here rather than retrofitted into a phase it did not belong to:

| Landed outside the plan | What it is |
| --- | --- |
| Firebase deployment | A hosting target, `tools/deploy.ps1` that cannot deploy to the wrong project, rules and indexes live at `bytepic-clipforge.web.app` |
| The service worker | Closed a Phase 7 gap that had been recorded as complete and was not |
| Branding and theming | Icons generated from `logo.png`; light and dark themes on semantic tokens, with contrast checked rather than assumed |
| The desktop shell | Tauri wrapping the same Angular build, so clips can actually be *watched* — the one thing a phone cannot do |
| Accounts and approval | Email/password sign-in, and access gated on an admin approving the account rather than merely authenticating it |
| The local control API | Loopback-only, token-authenticated, so a YouTube client secret can be typed into the app and still never leave the machine ([ADR-0011](adr/0011-local-control-api.md)) |
| Per-publish overrides | Title, description, privacy, category, tags and channel, chosen for one upload. Three layers of preference resolve in one place (`publish/metadata.py`), and the publication record holds what was actually sent |

Two of those were corrections rather than additions, and both are worth keeping visible: the service
worker had been signed off without existing, and FCM still has not been built.

Current test coverage (2026-09-19), all runnable from a clean clone with no GPU and no network except
where noted:

| Suite | Count | Needs |
| --- | --- | --- |
| Worker unit | 1082 | nothing |
| Worker integration | 125 | Firestore emulator; the five ASSEMBLE tests need only ffmpeg |
| Security rules | 264 | Auth + Firestore + Storage emulators |
| Web unit | 321 | nothing |
| Playwright E2E | 82 | Auth + Firestore emulators, stubbed worker |
| Worker GPU (opt-in) | 27 | RTX 3050, Ollama, ffmpeg |
| `doctor` | 18 checks | the real machine |

**Twelve of those unit tests were not running until 2026-09-14, and the count is why this table now
says where it came from.** The tier was a `@pytest.mark.unit` decorator on each test, so it had to be
remembered, and twelve times it was not — including every regression for the broker's nested-lease
deadlock, for the attempt-budget reclaim, and for hiding a broadcaster's mark on the RENDER path. All
twelve passed when run by hand. None were selected by `-m unit`, so CI ran neither the bugs' fixes
nor their proofs, and the total in the log went on looking healthy because a deselected test is
subtracted from the denominator too.

`tests/conftest.py` now derives the tier from the directory the test lives in and fails collection
outright for anything it cannot place; an explicit marker still overrides it, which is how two GPU
tests go on living in `tests/integration/`. This is the second time this repository has shipped a
test suite that was not running what it reported — the first is recorded at Phase 8, where
`RenderStage` was implemented, unit-tested and never registered. Both had the same shape: **a thing
that must be remembered, in a place where forgetting is silent.** The fix, both times, was to derive
it instead.

### The billing posture: Spark until 2026-09-10, Blaze since

**This section described a free-tier project until 2026-09-14, four days after that stopped being
true.** It is rewritten rather than deleted, because the on-ramp it predicted is the part worth
keeping: the migration cost one adapter and one environment variable, exactly as forecast, and that
is the only evidence that designing behind a port paid for itself.

ClipForge ran on **Spark** from Phase 2 to 2026-09-10 ([ADR-0009](adr/0009-spark-tier-local-artefacts.md)).
Since 3 February 2026 Cloud Storage for Firebase requires Blaze outright — on Spark there is no
bucket at all and bucket API calls return 402/403 — so rendered clips stayed on the worker and a
phone review got stills. It now runs on **Blaze**, and each clip gets a bucket copy that expires
after five days while the worker keeps the master
([ADR-0018](adr/0018-blaze-and-a-short-lived-bucket-copy.md)). Cloud Functions are still designed
around, by choice rather than by billing ([ADR-0006](adr/0006-lease-based-job-claiming.md)).

What the tier did and did not cost, now that both sides of it have been lived in:

| | On Spark | On Blaze |
| --- | --- | --- |
| Submit a job from the phone | ✅ | ✅ |
| Live per-stage progress | ✅ | ✅ |
| Push notification on completion | ✅ | ✅ (still unbuilt — see Phase 11) |
| Approve / reject from the phone | ✅ the decision travels through Firestore | ✅ |
| Record a rights basis and publish from the phone | ✅ the phone sends an intent, not a file | ✅ |
| **Watch the clip on the phone** | ❌ poster frame + filmstrip + metadata | ✅ **for five days**, then the poster again |
| Watch the clip on the machine | ✅ through the worker's local file server | ✅ unchanged, and still the fallback |
| **Publish to YouTube** | ✅ the worker holds both the file and the token (D7, [ADR-0010](adr/0010-worker-held-publishing-credentials.md)) | ✅ unchanged |
| Analytics (v0.2) | ✅ the Analytics API is not a Firebase product | ✅ unchanged |

**What the on-ramp got right**, recorded because a prediction that held is worth as much as one that
did not:

1. `Clip` already carried `localPath`, `playbackUrl` and a `location` discriminator, so switching
   populated a field rather than migrating a model. ✅ held.
2. All artefact writes went through the `BlobStore` port, and the `firebase` adapter selected by
   `CLIPFORGE_BLOB_STORE` was the only code the upgrade needed. ✅ held.
3. Playback resolved through one documented precedence, so enabling Blaze lit a branch the UI already
   had and changed nothing else. ✅ held.
4. `storage.rules` stayed in the repository and stayed tested against the emulator, which does not
   care about billing, and deployed as-is. ✅ held.
5. A `backfill-storage` command uploads existing clips and fills `playbackUrl`. ✅ written on the day,
   against a contract that already supported its result.

The one thing the on-ramp did not anticipate is the expiry. Five days is a cost decision, not a
capability one, and it puts a clock on remote playback that the Spark posture did not have — a clip
older than five days is back to the poster frame unless the local file server is reachable. That is
now a risk row of its own above.

Publishing is deliberately *not* on that list. It never depended on Blaze, and it should not acquire a
dependency on it: the credentials belong on the worker whichever tier the project is on.

### Next: Phase 9 — Analytics and calibration

Phase 9 is the one that makes the rest of the project falsifiable. Everything up to here produces
scores; Phase 9 finds out whether they predicted anything. Three notes for whoever starts it:

1. **The `Publication` record is already the join key.** It carries the video id, the attestation and
   the publish time, so retention and view data has somewhere to attach without a new model. Phase 8
   wrote it as an audit log; Phase 9 reads it as an experiment log.
2. **Quota is the constraint again, and it is the same budget.** The Analytics API draws on the same
   10,000 daily units an upload spends 1,600 of. A poller that ignores this will starve publishing —
   `QuotaLedger` already exists and should be shared rather than duplicated.
3. **Do not let calibration silently rewrite history.** Candidate documents record `modelVersion` and
   `promptVersion` precisely so a score can be attributed to the thing that produced it. A
   recalibration that overwrites past scores destroys the only evidence the phase exists to gather.

Deliberately **not** doing now: any additional publishing platform — TikTok and Instagram both need
app review with materially harder approval paths and are scoped separately. Trend-driven sourcing
*was* gated here on Phase 9 producing evidence; it was built on 2026-09-19 once it was clear that
Phase 9's evidence is months of volume away and that a trend list is how the volume arrives — see
Phase 10's "Delivered" note for the reasoning.

One correction carried forward from Phase 8: a stage that exists is not a stage that runs.
`RenderStage` was implemented, unit-tested and never registered, and `submit` built job documents
from whichever stage implementations its caller happened to construct. Both were invisible to a test
suite that assembled its own pipelines. The pipeline's shape is now declared once, in `CLIP_PIPELINE`,
and asserted against the runnable stages — and a phase that adds a stage should extend that
declaration first.

### Three things the synthesis track asks of the phases still to come

[The synthesis track](#the-synthesis-track--m6-to-m9) is gated behind M5 and starts no earlier. But it
is cheap to keep three doors open while building Phases 9, 10 and 12, and expensive to reopen them
afterwards. None of these is a scope change; each is a shape to prefer where the choice is free.

1. **Phase 9 — do not assume a publication was made through an API.** Keep the metrics fetch keyed on
   whatever `externalId` the `Publication` carries, without caring how it got there. Phase 15 adds
   publications the operator created by hand, and they must be able to join the same way or most of
   the evidence Phase 17 needs will be missing precisely where the new content is.
2. **Phase 12 — resist making `Channel` YouTube-shaped.** It already carries `platform`, which is the
   hard part. The trap is per-channel *credentials*: a manual-delivery platform has none at all, so
   `ChannelConnection` should tolerate a channel that is configured and usable without ever having
   been authorised.
3. **Phase 10 — the opportunity scorer becomes an input, not just a queue.** What it ranks for a human
   to promote is the same signal `PLAN` will later consume. Building it as a YouTube-search-only
   scorer would mean rewriting it; keeping the source behind a port would not.

The research these come from was done on 2026-09-11 and is summarised inline above rather than linked,
because a link to a survey of platform pricing and API terms ages into a liability.
