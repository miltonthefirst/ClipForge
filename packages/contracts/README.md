# `@clipforge/contracts`

**Status: Phase 1.** This package is a placeholder until then.

The PWA and the worker are two independent implementations of one protocol: the job state machine,
the pipeline stage model, and the documents they exchange through Firestore. Defining those types
twice — once in TypeScript, once in Python — is how this project would silently rot.

So they are defined **once**, as JSON Schema, and generated into both languages:

```text
schemas/*.json  ──┬──▶ json-schema-to-typescript   ──▶ generated/typescript/
   (source of     │
    truth)        └──▶ datamodel-code-generator    ──▶ generated/python/  (Pydantic v2)
```

CI fails if a schema changes and the generated output is not regenerated, so drift is caught at
review time rather than at runtime.

## Schemas to be defined here

| Schema | Used by |
| --- | --- |
| `Job`, `Stage` | Worker scheduler, PWA progress view |
| `Source`, `Transcript` | Ingestion and transcription stages |
| `Candidate` | Clip selection; the LLM's schema-constrained response |
| `Clip` | Render output, review state, rights attestation |
| `WorkerHeartbeat` | Worker status indicator in the PWA |

See [`docs/PLAN.md`](../../docs/PLAN.md) Phase 1, and decision **D8** in §3.1.
