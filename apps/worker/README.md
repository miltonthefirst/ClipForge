# ClipForge Worker

The local half of ClipForge. Claims jobs from Firestore, then runs the pipeline
— download, transcribe, analyse, render, publish — on local hardware.

See [`docs/PLAN.md`](../../docs/PLAN.md) for the architecture, and the repository
[`README.md`](../../README.md) for setup.

> **Status (2026-09-19): the pipeline runs end to end, and then some.** Eight job
> types — `CLIP`, `REMAKE`, `MUSIC`, `PUBLISH`, `UPLOAD`, `RESEARCH`, `COMPILE`
> and the `ECHO` that exercises the scheduler — over the lease protocol,
> checkpointed stage runner and `ModelBroker` that Phase 2 laid down. The
> worker also fires research schedules on its own. Where things stand is in
> [`docs/PLAN.md`](../../docs/PLAN.md), by phase.

## Quick reference

```bash
uv sync                      # core deps only (what CI installs)
uv sync --extra gpu          # + faster-whisper / CTranslate2 CUDA runtime
uv sync --extra gpu --extra media   # + yt-dlp

uv run ruff check .
uv run mypy
uv run python -m clipforge.diagnostics
```

## Running the worker

Everything below talks to the **Firebase emulator** by default
(`CLIPFORGE_USE_EMULATORS=true`), so it costs nothing and needs no real project.

```bash
# Terminal 1 — the control plane
firebase emulators:start --config ../../firebase.json --project demo-clipforge --only firestore

# Terminal 2
uv run clipforge-worker submit          # enqueue an ECHO job, prints its id
uv run clipforge-worker submit URL --brief "the goals" --clips 3 --min 20 --max 45
uv run clipforge-worker run             # claim, run, heartbeat, reap; Ctrl-C to stop
uv run clipforge-worker status <job-id> # the job document and its event log
uv run clipforge-worker gpu             # what is currently holding VRAM
uv run clipforge-worker research -t "premier league"   # what is trending, as a job
uv run clipforge-worker research --category football --region GB   # steered, no topics needed
uv run clipforge-worker categories                      # the catalogue, by group
uv run clipforge-worker trends <job-id>                 # its ranked list, once it has run
uv run clipforge-worker compile URL1 URL2 --theme "…"   # one clip from several videos
```

`clipforge-worker gpu` is the one-command answer to "why did my job refuse to
start?" — Phase 0 found an unrelated Ollama session holding 4.8 GB of the 6 GB
card, and the broker will refuse to load a model rather than die with a CUDA OOM.

## Tests

```bash
uv run pytest -m unit                 # no GPU, no network, no emulator
uv run pytest -m gpu                  # real hardware, opt-in

# The integration tier needs Firestore. This starts it, runs, and shuts it down:
firebase emulators:exec --config ../../firebase.json --project demo-clipforge \
  --only firestore "uv run pytest -m integration -q"
```

The `demo-` project id prefix puts the emulator in fully offline mode: no
credentials, no billing, no network. That is what lets these run in CI on a pull
request from a fork with no secrets configured.

## How the pieces fit

| Module | Responsibility |
| --- | --- |
| `scheduler/lease.py` | The job state machine as **pure functions**. No I/O — it does not know Firestore exists |
| `scheduler/runner.py` | Executes a claimed job's stages in order, skipping `DONE` ones and persisting checkpoints |
| `scheduler/worker.py` | The process: claim loop, heartbeat, reaper, schedule tick, graceful shutdown |
| `scheduler/schedule.py` | When a standing research schedule is due, as pure functions, and the tick that fires it |
| `store/firestore.py` | Reads, applies and compare-and-swaps. Contains no rules of its own |
| `models/broker.py` | Exclusive GPU residency. Its lock **is** the depth-1 GPU lane |
| `models/vram.py` | NVML accounting, including *who else* is holding memory |
| `stages/` | The stage contract, and one module per job type: `download`, `transcribe`, `analyze`, `render`, `publish`, `music`, `remake`, `research`, `compile` |
| `stages/pipeline.py` | The stage list per job type — the one place a job's shape is declared — and the registry builders |
| `research/` | The trend providers behind one port, the arithmetic that ranks what they say, and the curate prompt |
| `analysis/` | What is put to the model and how its answers are read: clip selection, remake notes, preferences |
| `media/` | ffmpeg: framing, captions, obscuring, music, assembly |

The split between `lease.py` and `store/firestore.py` is deliberate: every
decision about whether a transition is legal is a pure function, so the adapter
cannot get the rules wrong — it does not contain any. It also makes the reaper
bindable to either a worker task or a Cloud Function without being written twice.
See [ADR-0006](../../docs/adr/0006-lease-based-job-claiming.md).

## Things that will bite you

**Do not import `faster_whisper` at module scope.**
`clipforge.models.cuda.register_cuda_dll_directories()` must run first, or
CTranslate2 fails to load `cublas64_12.dll` at first inference. See
[ADR-0002](../../docs/adr/0002-ctranslate2-without-pytorch.md).

**A stage must be idempotent.** It may be re-run after a crash that happened
anywhere inside it, including immediately before its checkpoint was persisted.
The runner cannot enforce this for you. See
[ADR-0007](../../docs/adr/0007-checkpointed-stage-pipeline.md).

**A stage that stops early must return `incomplete=True`.** The runner then
leaves it `PENDING`. Returning a normal outcome would mark it `DONE` and silently
skip work that never happened.

**The heartbeat must be comfortably shorter than the lease.** They are configured
independently, so `CLIPFORGE_HEARTBEAT_SECONDS` at or above
`CLIPFORGE_LEASE_SECONDS` would have every job reaped mid-flight. The worker
warns at startup if they are set that way.

## Why there is no PyTorch here

`faster-whisper` runs on CTranslate2, not PyTorch. Adding torch would cost
~2.5 GB and a class of CUDA wheel-matching problems for no benefit. VRAM
accounting uses NVML directly. See
[ADR-0002](../../docs/adr/0002-ctranslate2-without-pytorch.md).
