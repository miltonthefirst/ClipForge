# Firebase — control plane

**Status: implemented in Phase 1.**

| Path | Contents |
| --- | --- |
| `firestore.rules` | Per-user isolation. Treated as production code: every change needs an emulator test in the same PR |
| `storage.rules` | Clip and thumbnail access. **Kept and tested, but not deployed** — see below |
| `firestore.indexes.json` | Indexes for the claim query, the reaper, the review queue and analytics |
| `tests/` | Emulator-backed rules tests (`@firebase/rules-unit-testing` + Vitest) |
| `functions/` | Empty. The lease reaper runs as a worker task instead — see below |

The `firebase.json` that ties these together lives at the repository root, which
is where the Firebase CLI expects to find it.

## Running the tests

```bash
npm --prefix firebase/tests ci
npm --prefix firebase/tests test
```

That starts the Emulator Suite, runs the rules tests against it, and shuts it
down. It needs **Java** (the Firestore and Storage emulators are Java processes)
and nothing else — no credentials, no billing, no network, no real project.

The worker's half of the control-plane tests — the transactional claim and the
reaper — lives in `apps/worker/tests/integration` and runs the same way:

```bash
cd apps/worker
firebase emulators:exec --config ../../firebase.json --project demo-clipforge \
  --only firestore "uv run pytest -m integration -q"
```

## Two things to know before working here

**Deploys replace; they do not merge.** `firebase deploy --only firestore:rules`
overwrites the entire rules file for the target project, `--only functions`
deletes deployed functions absent from local source, and `--only hosting`
overwrites the live site. Always pass an explicit `--project` from `.env` and an
explicit `--only`. **Never run a bare `firebase deploy`.**

**These rules do not constrain the worker.** The worker authenticates with the
Admin SDK, which bypasses security rules entirely. `firestore.rules` is the PWA's
boundary, not the worker's. The design rule that follows is that the PWA may
*create* work and *review* results but may never write pipeline state — so a
buggy or compromised client cannot forge a `COMPLETED` job, invent a candidate,
or rewrite an event log. Anything the worker owns is read-only to the client.

That asymmetry is also why ClipForge has its own Firebase project rather than
sharing one: rules could not have protected a neighbouring app's data from a
worker bug. See [ADR-0004](../docs/adr/0004-dedicated-firebase-project.md).

## Why there is no Storage bucket

ClipForge runs on the **Spark free tier**, and since February 2026 Cloud Storage for
Firebase requires Blaze outright: on Spark there is no bucket at all, and bucket
API calls return 402/403. Rendered clips therefore stay on the worker and are
served to the PWA by a read-only local file server on `127.0.0.1:8765`.

`storage.rules` stays here and stays tested against the emulator, which does not
care about billing — so enabling Blaze later is a deploy, not a design exercise.
See [ADR-0009](../docs/adr/0009-spark-tier-local-artefacts.md).

## Why `functions/` is empty

The lease reaper is a **pure function over job documents**
(`clipforge.scheduler.lease.reap`), so the same logic binds to either a scheduled
Cloud Function or a periodic worker task without being written twice. ClipForge
uses the worker task: it costs nothing, and for a single-worker deployment it is
also the simpler of the two. See
[ADR-0006](../docs/adr/0006-lease-based-job-claiming.md).

## On the index definitions

Two entries in `firestore.indexes.json` are worth explaining, since JSON cannot
carry a comment.

The **`fieldOverrides`** disable indexing on `transcripts.segments` and
`jobs.stages`. Firestore indexes every field by default and bills an index write
per array entry. Both of those are large arrays that are only ever read whole,
never queried by their contents — left alone, `segments` would be the single
largest write-cost item in the system.

The **`status` + `leaseExpiresAt`** index exists for the reaper's range query,
and the **`status` + `createdAt`** index for the worker's FIFO claim query. Both
are deliberately narrow: an unfiltered listen over `jobs` would bill a read per
document on every reconnect, which is the easiest way to burn the free daily
quota.
