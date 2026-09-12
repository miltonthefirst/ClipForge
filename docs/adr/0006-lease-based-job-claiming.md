# ADR-0006 — Claim jobs with a transactional lease, and keep the state machine pure

- **Status:** Accepted
- **Date:** 2026-09-08
- **Phase:** 1

## Context

The original brainstorm had the worker poll Firestore for pending jobs and mark
them as it went. That is decision **D1** in `docs/PLAN.md` §3.1, and it has two
problems that only appear once something goes wrong.

**Polling has no crash semantics.** If a worker dies mid-job, the document sits
in `RUNNING` forever. Nothing distinguishes "a worker is working on this" from "a
worker died holding this", so recovery needs a human.

**Polling has no exclusion.** Two workers reading the same queue will both see
the same pending job. Marking it `RUNNING` after reading it is a
check-then-act race, and the window is exactly as long as the round trip.

There is a third consideration specific to this project: Firestore bills per
document read, and ClipForge is engineered to stay inside the free daily quota
(ADR-0004). A polling loop spends reads whether or not there is work to do.

## Decision

A worker takes a **lease** on a job: a transactional compare-and-swap that sets
`status = RUNNING`, `workerId`, and `leaseExpiresAt = now + 90s`. The worker
renews the lease every 30 seconds while it works. A job whose lease has lapsed is
claimable by anyone — which is what makes a crashed worker recoverable with no
operator involvement.

Two details carry the correctness:

**The claim re-checks its predicate inside the transaction.** Firestore
transactions are optimistic: a conflicting write causes a *retry*, not a failure.
A claim that only checked claimability before opening the transaction would let
the loser succeed on retry, and two workers would run the same job. Re-reading
inside the transaction means the loser sees the winner's `RUNNING` state and
correctly abandons the claim. This is verified by a test that races eight workers
at one document — two can pass by luck of scheduling; eight cannot.

**The state machine is a set of pure functions.** `clipforge.scheduler.lease`
takes a `Job` and a clock reading and returns the job that should replace it plus
the events to append. It has no I/O and does not know Firestore exists. The
adapter only reads, applies, and compare-and-swaps.

That split is what makes the **reaper** — the thing that reclaims lapsed leases —
a deployment choice rather than a rewrite. `reap()` is a pure function over a job
document, so the same logic binds to a periodic worker task or to a scheduled
Cloud Function. ClipForge uses the worker task: it is free, and for a
single-worker deployment it is also simpler.

Reclaiming an expired lease costs an attempt; taking a `QUEUED` job does not.
Without that distinction a job whose worker keeps dying would retry forever.

## Consequences

- Crash recovery is automatic and bounded by the lease duration. Phase 2's
  "kill the worker mid-stage and watch it resume" criterion depends on it.
- `leaseExpiresAt` must be stored as a real Firestore timestamp, not a string.
  The reaper filters on a range query, and a string comparison would be wrong the
  moment a timezone offset differed. There is an integration test asserting
  exactly this, because the failure would otherwise be silent and intermittent.
- The heartbeat deliberately emits **no event**. At one every 30 seconds it would
  dominate both the event log and the write budget while telling a reader nothing
  they could not infer from `leaseExpiresAt`.
- A worker whose renewal returns "you no longer own this" must stop working
  immediately. Otherwise two workers write conflicting stage results for the same
  job. The store surfaces this as `None` rather than an exception, because it is
  an expected outcome after a slow stage, not a bug.
- The heartbeat interval and the lease duration are configured independently, so
  a misconfiguration could set a heartbeat longer than the lease and have every
  job reaped mid-flight. `Settings.heartbeat_fits_in_lease()` exists to check it.
- Any illegal transition raises rather than returning `None`. The one genuinely
  expected "no" — a job someone else already claimed — is answered by
  `is_claimable`, so a `LeaseError` always indicates a scheduler bug.

## Alternatives considered

- **Polling with a status flag** (the original D1). Rejected above.
- **A dedicated queue product** — Cloud Tasks, Pub/Sub, Redis. All solve this
  properly, and all add a second piece of infrastructure to a design whose entire
  thesis is that the cloud does three things and no more. Firestore is already
  present and already transactional.
- **A distributed lock rather than a lease.** Locks need explicit release, so a
  crashed holder blocks the resource until a human intervenes. A lease expires on
  its own, which is the property actually wanted here.
- **Longer leases to reduce heartbeat writes.** Cheaper, but the lease duration
  is also the worst-case time a crashed job stays stuck. 90 seconds against a
  30-second heartbeat gives two missed renewals of slack, which is the right
  trade for a pipeline whose stages run for minutes.
