# ADR-0005 — Define the wire protocol once, in JSON Schema, and generate both languages

- **Status:** Accepted
- **Date:** 2026-09-08
- **Phase:** 1

## Context

The PWA and the worker are two independent implementations of one protocol. They
never call each other; they communicate entirely through documents in Firestore.
Nothing in the runtime forces their understanding of a `Job` to agree.

Hand-maintaining a TypeScript interface and a Pydantic model of the same document
is the failure this project is most likely to suffer, because it fails *quietly*.
Renaming `maxAttempts` on one side does not break a build. It produces a worker
that writes a field the PWA does not read, and a PWA that renders a field the
worker never sets — discovered days later, from a screen showing nothing.

This is decision **D8** in `docs/PLAN.md` §3.1.

## Decision

The protocol is defined once, as JSON Schema, in
`packages/contracts/schemas/clipforge.json`, and generated into both languages:

- **TypeScript** via `json-schema-to-typescript`, emitted as a `.d.ts`.
- **Pydantic v2** via `datamodel-code-generator`, emitted as an installable
  package `clipforge_contracts`.

Generated output is **committed**, so neither consumer needs the generator
toolchain to build. CI regenerates both halves and fails if the committed output
differs — this is Phase 1's exit criterion 4, and it was verified by introducing
a deliberate schema change and confirming both checks exit non-zero.

Three sub-decisions are worth recording because each closes off an alternative:

1. **One schema file, not one per type.** Both generators emit only what is
   reachable from the schema root, so the root is a registry object referencing
   every definition. Separate files would need cross-file `$ref` resolution and
   would produce duplicated shared types in the Python output.

2. **Timestamps are ISO 8601 strings in the schema, not Firestore `Timestamp`s.**
   The store adapters convert at the boundary. This keeps the document
   language-neutral and — more usefully — lets the unit tier compare documents as
   plain JSON with no emulator running.

3. **Python is snake_case; the wire is camelCase.** The generator emits
   snake_case fields with camelCase aliases and `populate_by_name`, so worker
   code says `job.max_attempts` and `model_dump(by_alias=True)` produces the wire
   form. Without `populate_by_name` the worker would have been forced to
   construct models by their camelCase alias — precisely the leak this package
   exists to prevent.

## Consequences

- Adding a field is a schema edit plus a regeneration. Forgetting the
  regeneration fails CI rather than reaching production.
- The generator flags are pinned rather than defaulted — including the
  formatters, because `datamodel-code-generator` warns that its defaults will
  change, and a formatter change would surface as a spurious staleness failure on
  an unrelated PR.
- Enabling the `pydantic.mypy` plugin became necessary: without it, mypy types a
  generated model's `__init__` from its aliases and rejects the snake_case field
  names the models are meant to be constructed with.
- The tests on both sides read the schema and assert the generated output against
  it, rather than restating field names. A test that hardcoded the shape would
  pass happily while the models went stale.
- Generated code is exempt from the repository's lint and typing standards. It is
  verified by being regenerated and compared, which is a stronger check than
  linting it would be.

## Alternatives considered

- **Hand-written types on both sides.** The default, and the thing this ADR
  exists to prevent. Cheap until the first silent divergence.
- **Generate from the Pydantic models** (Python as the source of truth). Tempting
  since the worker is where the documents originate, but it makes the PWA a
  second-class consumer of a Python artefact, and Pydantic's JSON Schema output
  is a lossy round-trip.
- **Generate into each consumer's tree rather than a shared package.** Avoids the
  path dependency, but then the two outputs have no single home and nothing
  stops them being regenerated from different schema revisions.
- **Protobuf or Avro.** Genuinely better at schema evolution, and wrong here: the
  transport is Firestore documents, which are JSON. A binary IDL would mean
  hand-writing the mapping to and from Firestore — reintroducing exactly the
  duplication being eliminated.
