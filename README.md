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
│  FIREBASE - control plane     │   Auth · Firestore · Storage (clips only) · FCM
│  rules enforce per-user       │   Functions: lease reaper, analytics poller
│  isolation                    │
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
└───────────────────────────────┘
```

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
- That attestation is enforced in both Firestore rules and the worker, and every publish is
  written to an audit log.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Start with [`docs/PLAN.md`](docs/PLAN.md) — it explains
what is being built and in what order, and every phase names what it deliberately excludes.

## License

[MIT](LICENSE)
