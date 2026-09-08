# Contributing to ClipForge

Thanks for looking. This document covers how to get set up, what the quality bar is, and the few
conventions that are load-bearing rather than taste.

## Before you start

Read [`docs/PLAN.md`](docs/PLAN.md). ClipForge is built in phases with explicit exit criteria, and
each phase names what it **deliberately excludes**. Those exclusions are the main defence against
scope creep, so a PR that adds face tracking during Phase 4 will be asked to wait — not because
the idea is bad, but because it belongs to Phase 6b.

## Setup

See [Getting started](README.md#getting-started) in the README, then:

```powershell
pwsh -File tools/doctor.ps1
```

`doctor` must exit 0 before you start. If it does not, fix that first — every failure it reports
is a dependency something later will need, and each one prints a suggested fix.

## Quality bar

CI runs exactly what you can run locally. There is nothing that only fails on the server.

```bash
# Worker
uv run --project apps/worker ruff check .
uv run --project apps/worker ruff format --check .
uv run --project apps/worker mypy                    # strict mode, no exceptions
uv run --project apps/worker pytest -m "not gpu"

# PWA
npm run --prefix apps/web lint
npm run --prefix apps/web typecheck
npm run --prefix apps/web format:check
npm run --prefix apps/web test:ci
npm run --prefix apps/web build
```

`mypy` runs in **strict** mode. If a third-party package has no stubs, add it to the override list
in `apps/worker/pyproject.toml` rather than sprinkling `# type: ignore`.

## Definition of Done

From `docs/PLAN.md`, applied to every change:

1. CI green.
2. Unit tests for pure logic; emulator-backed integration tests for anything touching Firebase.
3. An ADR in [`docs/adr/`](docs/adr) for any decision that closes off an alternative.
4. `README.md` and `.env.example` updated if setup changed.
5. Reproducible from a clean clone by following the docs.

Rule 5 is the one people skip. If your change needs a step that only exists in your shell history,
it is not done.

## Tests

Four tiers, declared as pytest markers and enforced with `--strict-markers`:

| Marker | Requires | Runs in CI |
| --- | --- | --- |
| `unit` | nothing — no GPU, no network, no subprocess | ✅ |
| `integration` | Firebase Emulator Suite | ✅ |
| `gpu` | real NVIDIA GPU, Ollama, ffmpeg | ❌ opt-in |

The `unit` tier being genuinely hermetic is what keeps CI fast and honest. If a unit test needs a
probe that touches the outside world, stub it with `monkeypatch` — there are examples in
`apps/worker/tests/unit/test_diagnostics.py`.

Run the `gpu` tier locally before submitting anything that touches inference or media:

```bash
uv run --project apps/worker pytest -m gpu
```

## Conventions worth knowing

**Never hardcode a Firebase project id, bucket or emulator host.** They come from `.env` only.
This is what makes swapping projects a one-file change — and it is not hypothetical: ClipForge moved
project once already. See [ADR-0004](docs/adr/0004-dedicated-firebase-project.md).

**Never run a bare `firebase deploy`.** Always pass an explicit `--project` and an explicit `--only`.
Firebase deploys **replace rather than merge**: `--only firestore:rules` overwrites a project's entire
rules file, `--only functions` deletes deployed functions that are absent from local source, and
`--only hosting` overwrites the live site. A mistyped project id is enough to damage an unrelated
project, and there are several similarly-named ones on the account this was developed against.

**Never edit anything under `packages/contracts/generated/`.** It is generated from
`schemas/clipforge.json` and CI regenerates it and fails on any difference. Change the schema and
regenerate — both languages, every time. See [ADR-0005](docs/adr/0005-single-source-contracts.md).

**Never commit secrets.** Service-account JSON, OAuth tokens and `.env` are gitignored. Publishing
credentials live on the worker and must never be written to Firestore — see ADR on worker-held
credentials.

**PowerShell scripts need a UTF-8 BOM.** Windows PowerShell 5.1 reads `.ps1` as ANSI without one,
so a single non-ASCII character produces a parser error on someone else's machine. CI enforces
both the BOM and that the script parses. Keep `tools/*.ps1` ASCII-only as well.

**GPU work is serialised deliberately.** The `ModelBroker` owns GPU residency and only one model
may be resident at a time. If you find yourself wanting two, re-read `docs/PLAN.md` §2.1 — the
6 GB budget is the reason the architecture looks the way it does.

**Import CUDA-dependent modules lazily.** `faster_whisper` must never be imported at module scope,
and `clipforge.models.cuda.register_cuda_dll_directories()` must run before it is imported at all.
See [ADR-0002](docs/adr/0002-ctranslate2-without-pytorch.md).

## Commits and PRs

- Branch from `master`. Conventional-commit prefixes (`feat:`, `fix:`, `docs:`, `chore:`) are
  appreciated but not enforced.
- One logical change per PR. A PR that both refactors the scheduler and adds a stage is two PRs.
- Say which phase the change belongs to, and if it is out of phase, say why.

## Reporting bugs

Use the issue templates. For anything environmental, include the output of:

```powershell
pwsh -File tools/doctor.ps1
```

That single command answers most of the questions a maintainer would otherwise have to ask.
