# ADR-0007 — A job is a pipeline of checkpointed stages, not one unit of work

- **Status:** Accepted
- **Date:** 2026-09-08
- **Phase:** 2

## Context

The original brainstorm treated a job as one operation: the worker picks it up,
does everything, marks it done. That is decision **D2** in `docs/PLAN.md` §3.1,
and it does not survive contact with this project's constraints.

A ClipForge job downloads a source that may be gigabytes, transcribes it on a GPU
that can hold exactly one model, runs an LLM that requires the *other* model to
have been evicted first, and then encodes video. The download and the
transcription cost minutes each. The GPU stages cannot both be resident, so the
pipeline **must** stop and swap models partway through — a model swap is a normal
event, not an exception.

Under a monolithic job, any interruption — a crash, a model swap gone wrong, a
worker restart — costs everything already done. Re-downloading two gigabytes to
retry a failed LLM call is not a recovery strategy.

## Decision

A job carries an **ordered array of stages**, each with its own status, timings,
error and opaque checkpoint. The runner executes them front to back and **never
re-executes a stage that is `DONE`**.

Three rules make it work:

**A stage must be idempotent.** It may be re-run after a crash that happened
anywhere inside it, including immediately before its checkpoint was persisted.
This is the stage author's responsibility and cannot be enforced by the runner.

**A checkpoint is opaque to the scheduler.** It is persisted and returned
verbatim, never interpreted. A stage that needs the scheduler to understand its
checkpoint has put scheduling logic in the wrong place.

**The job document is written after every stage, not at the end.** A crash
between stages must leave a document that describes what actually happened.

A stage that is asked to stop mid-flight returns `incomplete`, and the runner
leaves it `PENDING` rather than `DONE`. Marking it `DONE` would silently skip
work that never happened — the single most dangerous bug this design could have.

## Consequences

- Crash recovery is cheap and *provable*. The Phase 2 exit criterion — kill the
  worker inside stage 2, restart, resume without re-running stage 1 — is an
  automated test, using a real killed subprocess. The ECHO stages call
  `os._exit()` from inside themselves, because a simulated crash would unwind the
  stack, run `finally` blocks and release the lease, proving nothing.
- Per-stage timings and peak VRAM are recorded on the job document, so the model
  ladder in `docs/PLAN.md` §2.1 can eventually be revised against measurements
  rather than estimates.
- One Firestore write per stage. Deliberately per *stage*, not per progress tick:
  Firestore bills per write, and a chatty progress loop is the easiest way to
  burn the free daily quota (ADR-0004).
- Stages are looked up through a registry rather than a match statement. That is
  what lets the `ECHO` job type register three artificial stages and exercise the
  entire harness — scheduler, lease, checkpointing, resume — while Phase 2's
  scope note still forbids touching any media.
- A retryable failure requeues the job with earlier stages intact. A
  non-retryable one fails it immediately: burning two more attempts on a
  malformed URL wastes twenty minutes to learn nothing.

## Alternatives considered

- **A monolithic job with a progress percentage.** Simpler, and loses everything
  on any interruption. Rejected on the model-swap argument alone: the swap is a
  designed step, so the pipeline must be resumable across it.
- **Separate job documents per stage, chained.** Genuinely resumable, but the
  chaining logic has to live somewhere, and it recreates a workflow engine inside
  Firestore. It also multiplies reads and writes, which ADR-0004 rules out.
- **Checkpoints in a side collection.** Keeps the job document small, at the cost
  of a second read per stage and a consistency problem between the two documents.
  Stage state and job state change together, so they belong in one document and
  one write.
- **Letting the scheduler interpret checkpoints** (to show meaningful progress,
  say). Rejected: it couples the scheduler to every stage's internals. If richer
  progress is needed later, it belongs in a typed field beside the checkpoint,
  not inside it.
