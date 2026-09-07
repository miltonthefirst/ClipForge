# ADR-0003 — Pin the worker to a uv-managed Python 3.12

- **Status:** Accepted
- **Date:** 2026-09-07
- **Phase:** 0

## Context

The development machine's only Python is 3.14.5. Neither CTranslate2 nor several
of the worker's other native dependencies publish wheels for 3.14, so
`faster-whisper` cannot be installed against it at all. Building from source on
Windows is not a reasonable ask of a contributor.

More generally, "whatever Python happens to be on PATH" is the single most common
reason a Python project fails to build on someone else's machine — which directly
contradicts `docs/PLAN.md`'s Definition of Done rule 5: a reviewer cloning the
repository must be able to reach the same working state.

## Decision

The worker declares `requires-python = "==3.12.*"` and the interpreter is
provisioned by `uv`, not by the system. `apps/worker/.python-version` pins 3.12,
and `uv.lock` is committed so dependency resolution is byte-reproducible.

No tool in the repository may invoke the system Python. `tools/doctor.ps1`
asserts that the worker environment resolves to 3.12.x and fails loudly if it
resolves to anything else — including 3.14, which would otherwise fail much later
with a confusing native build error.

## Consequences

- A contributor needs `uv` and nothing else; `uv sync` provisions the interpreter
  itself. The system Python is irrelevant and may be any version.
- CI uses the same mechanism (`astral-sh/setup-uv` plus `uv python install 3.12`),
  so local and CI environments cannot drift.
- `uv sync --locked` in CI fails if `uv.lock` is stale, which keeps the lockfile
  honest.
- Moving to 3.13 later is a deliberate edit to two files plus a CI run, rather
  than an accident that happens when someone upgrades their machine.

## Alternatives considered

- **Downgrade the system Python to 3.12.** Breaks whatever else on the machine
  depends on 3.14, and does nothing for other contributors.
- **`pyenv-win` / `conda`.** Both work. `uv` was chosen because it handles
  interpreter provisioning, dependency resolution, locking and virtual
  environments in one tool, and is fast enough that the reproducibility does not
  cost developer patience.
- **Allow a version range such as `>=3.11,<3.14`.** Tempting for library code,
  but this is an application with native dependencies. A range means the lockfile
  no longer describes a single reproducible environment, which is the whole point.
