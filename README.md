<div align="center">

<img src="logo.png" alt="ClipForge" width="140">

# ClipForge

**A local-first AI content agent.**
Ingest long-form video, transcribe and analyse it on your own hardware, cut high-potential
vertical clips, review them from your phone, publish, and learn from what performed.

[![CI](https://github.com/miltonthefirst/ClipForge/actions/workflows/ci.yml/badge.svg)](https://github.com/miltonthefirst/ClipForge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-3776ab.svg)](apps/worker/pyproject.toml)
[![Angular 22](https://img.shields.io/badge/angular-22-dd0031.svg)](apps/web/package.json)

</div>

---

> [!NOTE]
> **Status: Phase 0 complete.** The toolchain, repository skeleton and CI are in place and
> verified. The pipeline itself is not built yet — see [`docs/PLAN.md`](docs/PLAN.md) for the
> milestone plan and where things stand.

## Why

Cloud AI video pipelines are metered per-minute and per-token. ClipForge moves every expensive
operation — transcription, semantic analysis, encoding — onto commodity local hardware, and uses
the cloud only for the three things it is genuinely good at: **identity**, **small structured
state**, and **reaching your phone**.

The reference machine is an RTX 3050 with **6 GB of VRAM**. That constraint is not incidental; it
shapes the architecture. Whisper and the analysis LLM cannot be resident at the same time, so GPU
residency is scheduled explicitly and jobs are checkpointed pipelines that can resume mid-run.

## Architecture

```text
┌───────────────────────────────┐
│  PWA - Angular + Tailwind     │   Submit, watch progress, review, approve
│  installable, offline shell   │
└───────────────┬───────────────┘
                │ Firebase SDK (as the signed-in user)
                ▼
┌───────────────────────────────┐
│  FIREBASE - control plane     │   Auth · Firestore · FCM · Hosting
│  Spark free tier              │   rules enforce per-user isolation
│  no Storage, no Functions     │   the lease reaper runs on the worker
└───────────────┬───────────────┘
                │ Admin SDK - onSnapshot, transactional lease claim
                ▼
┌───────────────────────────────┐
│  LOCAL WORKER - Python 3.12   │
│                               │
│  Scheduler ── GPU lane (1) ───┼─▶ faster-whisper ─┐
│            └─ CPU lane (N) ───┼─▶ Ollama LLM  ────┼─ ModelBroker (exclusive)
│                               │   yt-dlp ─────────┤
│  Stage runner + checkpoints   │   ffmpeg/NVENC ───┘
│  Local file server 127.0.0.1  │
└───────────────────────────────┘
        rendered clips stay here
```

**Rendered clips never leave the machine.** Cloud Storage for Firebase has required a paid plan since
February 2026, and ClipForge is built to run without one. The phone gets the poster frame, the hook,
the score breakdown and the transcript excerpt — enough to approve or reject, which is the decision
that actually matters — and the video itself plays when the PWA is opened on the worker machine. All
artefact writes go through a `BlobStore` port, so enabling the paid plan later is one adapter and one
environment variable, not a redesign. See
[ADR-0009](docs/adr/0009-spark-tier-local-artefacts.md).

Full detail, including the data model and the job/lease protocol, is in
[`docs/PLAN.md`](docs/PLAN.md).

## Repository layout

| Path | What lives there |
| --- | --- |
| [`apps/web/`](apps/web) | Angular 22 + Tailwind PWA |
| [`apps/worker/`](apps/worker) | Python 3.12 worker — the whole pipeline |
| [`packages/contracts/`](packages/contracts) | JSON Schema → generated TypeScript + Pydantic types |
| [`firebase/`](firebase) | Security rules, indexes, Cloud Functions |
| [`docs/`](docs) | [Plan](docs/PLAN.md), [ADRs](docs/adr) |
| [`tools/`](tools) | `doctor.ps1` and dev scripts |

## Getting started

### Prerequisites

Install these once. `doctor` will tell you if anything is missing or wrong.

| Tool | Install | Why |
| --- | --- | --- |
| [uv](https://docs.astral.sh/uv/) | `winget install astral-sh.uv` | Provisions Python 3.12 and locks dependencies |
| [ffmpeg](https://www.gyan.dev/ffmpeg/builds/) | `winget install Gyan.FFmpeg` | **Full build required** — the essentials build lacks `libass` |
| [Node 20+](https://nodejs.org) | `winget install OpenJS.NodeJS.LTS` | Builds the PWA |
| [Ollama](https://ollama.com/download) | — | Runs the analysis LLM |
| NVIDIA driver | — | Only for GPU inference; the repo builds and tests without one |

> You do **not** need to install Python yourself. `uv` provisions Python 3.12 for the worker, and
> whatever Python is on your PATH is irrelevant. See
> [ADR-0003](docs/adr/0003-uv-managed-python-toolchain.md).

### Setup

```bash
git clone https://github.com/miltonthefirst/ClipForge.git
cd ClipForge

# Worker. Omit --extra gpu on a machine with no NVIDIA card;
# everything except real inference still works.
uv sync --project apps/worker --extra gpu --extra media

# PWA
npm ci --prefix apps/web

# Analysis model (~3.4 GB)
ollama pull qwen3.5:4b

# Configuration
cp .env.example .env
```

### Verify

```powershell
pwsh -File tools/doctor.ps1
```

```text
  ClipForge doctor
  ----------------
  [PASS] git                    git version 2.54.0.windows.1
  [PASS] node                   v24.16.0
  [PASS] uv                     uv 0.12.10
  [PASS] ffmpeg                 ffmpeg version 9.0.1-full_build
  [PASS] ollama                 ollama version is 0.33.3
  [PASS] python (worker venv)   Python 3.12.14
  [PASS] ffmpeg                 h264_nvenc, libx264, ass, loudnorm, silencedetect present
  [PASS] gpu                    NVIDIA GeForce RTX 3050 * 6144 MiB total * 5444 MiB budget
  [PASS] cuda_libs              registered 3 DLL director(ies): cublas, cuda_nvrtc, cudnn
  [PASS] cuda_transcribe        CTranslate2 ran on device='cuda' compute_type='int8_float16'

  All checks passed. This machine can build and run ClipForge.
```

`doctor` exits with the number of failed checks and prints a suggested fix for each, so it works
in a script as well as by eye. Use `-SkipGpu` on a machine without an NVIDIA card.

### Publishing to YouTube (optional)

Off by default, and meant to stay off until you have read
[Rights and responsible use](#rights-and-responsible-use). Nothing is uploaded while
`CLIPFORGE_PUBLISHING_ENABLED=false`.

**1. Create an OAuth client.** In the [Google Cloud console](https://console.cloud.google.com/),
enable the *YouTube Data API v3*, then create an OAuth client of type **Desktop app**. Add
`http://127.0.0.1:8766/` as an authorised redirect URI. Download the JSON.

**2. Point the worker at it.**

```dotenv
CLIPFORGE_PUBLISHING_ENABLED=true
CLIPFORGE_YOUTUBE_CLIENT_SECRETS=./.clipforge/youtube-client.json
```

**3. Authorise, once.**

```bash
uv run --project apps/worker clipforge-worker youtube-auth
```

This opens a consent page, catches the redirect on loopback, and stores a refresh token at
`CLIPFORGE_YOUTUBE_TOKEN_STORE`. The token is encrypted to this machine and this user, is
**never written to Firestore**, and never reaches the PWA — see
[ADR-0010](docs/adr/0010-worker-held-publishing-credentials.md) for exactly what that
encryption does and does not protect against.

**4. Check it.**

```bash
uv run --project apps/worker clipforge-worker quota    # what today's allowance still permits
uv run --project apps/worker python -m clipforge.diagnostics --skip-gpu
```

Then, in the PWA: approve a clip on **Review**, record its rights basis on **Publish**, and press
publish. The worker uploads it as **unlisted**.

#### Two things that will bite you

**Refresh tokens expire after 7 days.** While the OAuth consent screen is in *Testing* mode, Google
expires refresh tokens weekly. The upload scope is *sensitive*, so leaving Testing means submitting
the app for Google verification. Until you do, re-run `youtube-auth` when `doctor` tells you to — it
reports the token's age precisely because that is the number which predicts the next failure.

**Quota allows about six uploads a day.** The default YouTube Data API allowance is 10,000 units and
an upload costs **1,600**. The worker budgets this and refuses *before* starting an upload rather
than failing on the seventh; `clipforge-worker quota` reports what is left.

### Develop

```bash
# Worker
uv run --project apps/worker ruff check .
uv run --project apps/worker mypy
uv run --project apps/worker pytest -m "not gpu"   # what CI runs
uv run --project apps/worker pytest -m gpu         # real hardware, opt-in

# PWA
npm run --prefix apps/web lint
npm run --prefix apps/web test:ci
npm run --prefix apps/web start
```

## Deploying

ClipForge is local-first, so "deploying" means two different things. The **worker
never deploys** — it runs on your machine, holds the video files and the publishing
credentials, and reaches out to Firestore. What deploys is the **control plane**:
Firestore rules, indexes, and the PWA you review from.

### One-time console setup

Four things can only be done in the [Firebase console](https://console.firebase.google.com/),
because they create resources rather than configure them:

1. **Create the Firestore database.** Build → Firestore Database → Create. The
   **location is permanent** and cannot be changed later, so pick the region
   closest to where you will actually use the phone, not where the servers feel
   familiar. Start in production mode; the rules in this repository replace the
   defaults on first deploy.
2. **Enable Google sign-in.** Build → Authentication → Sign-in method → Google.
3. **Register a Web app.** Project settings → Your apps → Web. Copy the
   `apiKey`, `authDomain` and `appId` into `.env` as `CLIPFORGE_WEB_*`. These are
   not secrets — a Firebase web API key identifies a project, it does not
   authorise anything. What protects the data is `firestore.rules` plus Auth,
   which is why those are the parts with tests.
4. **Generate a service account key** for the worker. Project settings → Service
   accounts → Generate new private key. Save it **outside** the repository and
   point `CLIPFORGE_GOOGLE_APPLICATION_CREDENTIALS` at it.

All four are free on Spark.

### Deploy

```powershell
# See exactly what would happen, and to which project. Nothing is published.
pwsh -File tools/deploy.ps1 -DryRun

# Build and inline the configuration, then stop — inspect the artefact first.
pwsh -File tools/deploy.ps1 -Only hosting -BuildOnly

# Rules and indexes only; useful on their own after a rules change.
pwsh -File tools/deploy.ps1 -Only rules,indexes

# Everything.
pwsh -File tools/deploy.ps1
```

**Never run a bare `firebase deploy`.** A deploy *replaces* rather than merges,
and it takes its project from whatever is ambient. `tools/deploy.ps1` exists to
make that mistake impossible: it reads the project id from `.env`, passes it
explicitly, always names its targets with `--only`, prints the exact command
before running it, and refuses outright if the configured project is one it has
been told not to touch.

It also refuses to publish a PWA build still pointed at the emulators. That
failure is otherwise silent and remote — the site loads perfectly, then every
read hangs against `127.0.0.1:8080` on a phone that has no emulator to reach.

### Point the worker at the real project

```dotenv
CLIPFORGE_USE_EMULATORS=false
CLIPFORGE_GOOGLE_APPLICATION_CREDENTIALS=C:/keys/clipforge-service-account.json
```

Then `clipforge-worker run`. The worker refuses to start if credentials are
missing rather than discovering it on its first write, by which point it would
already have advertised itself as healthy.

Storage rules are deliberately **not** deployable: the Spark tier has no bucket
at all, so `--only storage` fails by definition. The file stays in the repository,
stays tested against the emulator, and deploys as-is on the day Blaze is enabled
([ADR-0009](docs/adr/0009-spark-tier-local-artefacts.md)).

## Testing tiers

| Tier | In CI | Requires | Covers |
| --- | --- | --- | --- |
| `unit` | ✅ | nothing | Pure logic. No GPU, no network, no subprocess |
| `integration` | ✅ | Firebase emulator | Rules, lease races, resumability |
| `e2e` | ✅ | emulator + stub worker | Submit → progress → review → approve |
| `gpu` | ❌ opt-in | NVIDIA GPU, Ollama, ffmpeg | Real transcription, inference, encode |

CI runs everything except `gpu` — GitHub runners have no NVIDIA hardware. The `gpu` tier is the
canonical check for anything `doctor` verifies.

## Limitations

Stated plainly, because they are real:

- **6 GB VRAM is the ceiling.** Whisper and the LLM cannot be co-resident. Models above ~5.4 GB
  will not load, and a *separate* Ollama session holding a large model will starve the pipeline.
- **yt-dlp breaks when YouTube changes.** Ingestion is pinned and isolated behind an adapter, but
  this is an ongoing maintenance cost, not a solved problem.
- **YouTube publishing is quota-bound.** An upload costs 1,600 of the default 10,000 daily units —
  about six uploads per day. An unverified OAuth app also expires refresh tokens every 7 days.
- **No remote video playback on the free tier.** Cloud Storage requires a paid Firebase plan, so
  clips stay on the worker. You review from your phone against a poster frame and metadata, and watch
  the actual video on the machine. Publishing is unaffected — the worker holds both the file and the
  OAuth token.
- **Publishing third-party content is your responsibility.** See below.
- **Windows-first.** The worker is developed and tested on Windows. Nothing is deliberately
  platform-locked, but Linux and macOS are unverified.

## Rights and responsible use

ClipForge can analyse video you did not create. Doing that locally, for your own use, is ordinary
private use. **Publishing** a derived clip of someone else's copyrighted material is a different
act, and establishing the legal basis for it is the operator's responsibility — it varies by
jurisdiction, by the source's licence, and by how transformative the clip is.

ClipForge does not decide that for you, and it does not pretend the question does not exist:

- Publishing is **disabled by default** (`CLIPFORGE_PUBLISHING_ENABLED=false`).
- No clip can be published without a recorded **rights basis** — one of `OWN_CONTENT`,
  `LICENSED`, `PERMISSION_GRANTED`, `FAIR_USE_ASSERTED` or `PUBLIC_DOMAIN` — attributed to a user
  and timestamped.
- That attestation is enforced in **three** places, and the redundancy is deliberate:
  `firebase/firestore.rules` (which is the only thing that can stop a client enqueueing publish
  work), `apps/worker/clipforge/publish/rights.py` (the only thing that constrains the component
  actually holding the credentials, since the Admin SDK bypasses rules), and
  `apps/web/src/app/core/rights.ts`, which is advisory and exists only to explain a refusal in
  the interface rather than fail opaquely after the button is pressed.
- A fair-use assertion additionally requires written reasoning. It is a judgement, not a status,
  and an empty note would make the audit log say "because I said so".
- Every publish writes a `Publication` record carrying the attestation **as it stood at upload
  time**, copied rather than referenced — so a later edit to the clip cannot rewrite the reason a
  past upload happened. "Who authorised this, on what basis, and what went out?" is answerable
  from the phone.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Start with [`docs/PLAN.md`](docs/PLAN.md) — it explains
what is being built and in what order, and every phase names what it deliberately excludes.

## License

[MIT](LICENSE)
