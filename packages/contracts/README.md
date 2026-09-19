# `@clipforge/contracts`

**Status: implemented in Phase 1.**

The PWA and the worker are two independent implementations of one protocol: the job state machine,
the pipeline stage model, and the documents they exchange through Firestore. Defining those types
twice — once in TypeScript, once in Python — is how this project would silently rot.

So they are defined **once**, as JSON Schema, and generated into both languages:

```text
schemas/clipforge.json  ──┬──▶ json-schema-to-typescript  ──▶ generated/typescript/index.d.ts
    (source of truth)     │
                          └──▶ datamodel-code-generator   ──▶ generated/python/clipforge_contracts/
```

CI regenerates both halves on every PR and fails if the committed output differs, so drift is caught
at review time rather than at runtime. See
[ADR-0005](../../docs/adr/0005-single-source-contracts.md).

## Regenerating

After **any** edit to `schemas/clipforge.json`, run both:

```bash
npm --prefix packages/contracts run generate
uv run --project apps/worker python packages/contracts/scripts/generate_python.py
```

To verify without writing (what CI does):

```bash
npm --prefix packages/contracts run check
uv run --project apps/worker python packages/contracts/scripts/generate_python.py --check
```

The generated files carry a DO-NOT-EDIT banner. Editing them by hand is not merely discouraged, it is
pointless: CI regenerates and compares.

## How each side consumes it

| Consumer | Mechanism | Why |
| --- | --- | --- |
| `apps/web` | `paths` mapping in `tsconfig.json` to `index.d.ts` | The output is types-only, so there is nothing to build, install, or keep in a lockfile. A declaration file also sidesteps `rootDir` complaints that a `.ts` outside the project would raise |
| `apps/worker` | `uv` path dependency (`[tool.uv.sources]`) | Makes it a real installed package, so `clipforge_contracts` imports resolve identically in the editor, in pytest and in CI |

## Conventions the generators enforce

- **Python is snake_case; the wire is camelCase.** Models are built with snake_case field names and
  serialised with `model_dump(by_alias=True)`. Both directions are tested.
- **Timestamps are ISO 8601 strings in the schema and timezone-aware `datetime`s in Python.** They are
  deliberately *not* Firestore `Timestamp`s — the store adapters convert at the boundary, which keeps
  this document language-neutral and lets the unit tier compare documents as plain JSON with no
  emulator running.
- **Unknown fields are rejected** (`additionalProperties: false` → `extra="forbid"`). A typo'd field
  name fails loudly instead of vanishing into Firestore.
- **Enum members are UPPER_CASE in Python**, matching their wire values.

## Schemas defined here

| Type | Used by |
| --- | --- |
| `Job`, `Stage`, `StageError`, `JobEvent` | Worker scheduler, PWA progress view |
| `Source`, `Transcript` | Ingestion and transcription stages |
| `Candidate`, `SubScores` | Clip selection and scoring |
| `LlmClipResponse`, `LlmClipProposal` | The model's schema-constrained reply |
| `Clip` | Render output, review state, where the file lives |
| `WorkerHeartbeat`, `GpuInfo` | Worker status indicator in the PWA |
| `Publication`, `MetricSnapshot`, `CalibrationReport` | Publishing, analytics, and whether the scoring predicted anything |
| `RemakeOptions`, `AppliedRemake`, `Preference` | Correcting a clip, and what the worker learned from the correction |
| `ClipOptions` | The brief on a clip job: what to clip, how many, how long |
| `ResearchOptions`, `Trend`, `LlmTrendVerdict` | A trend run's question, its rows, and the model's verdict on each |
| `ResearchSchedule` | A standing research request the worker fires on its own |
| `CompileOptions`, `AppliedCompile` | One clip from several videos, and the provenance of each piece |
| `AgentReport`, `Channel`, `UserProfile` | The machine agent, YouTube channels, and who may use the app |

## The category catalogue

`data/categories.json` is the one piece of *data* here rather than a type: the categories a
research run may say it is about — a code, a label and group, aliases to search by, and what the
worker does with it (a hint for video lookups, search terms for a run with no topics, subreddits
for a run that names none). Both generators render it, into `generated/typescript/categories.ts`
and `clipforge_contracts.categories`, and both `--check` modes compare it. Add a category by
adding an entry and regenerating; the rules check only the code's shape, so no rules deploy is
needed. See [ADR-0025](../../docs/adr/0025-a-category-catalogue-as-data.md).

The root `ClipForgeContracts` object exists so that every definition is reachable from the schema
root: both generators emit only what is referenced, and a type that nothing referenced would silently
fail to generate.

## Tests

Neither side restates the schema in test code — both read `schemas/clipforge.json` and assert the
generated output against it, so a test cannot pass while the models are stale.

- Python: [`apps/worker/tests/unit/test_contracts.py`](../../apps/worker/tests/unit/test_contracts.py)
- TypeScript: [`apps/web/src/app/contracts.spec.ts`](../../apps/web/src/app/contracts.spec.ts) — a
  compile-time test; if the generated types stop matching, the file stops compiling.
