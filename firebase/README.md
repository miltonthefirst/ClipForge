# Firebase — control plane

**Status: Phase 1.** This directory is a placeholder until then.

| Path | Contents |
| --- | --- |
| `firestore.rules` | Per-user isolation. Treated as production code: every change needs an emulator test in the same PR |
| `storage.rules` | Clip and thumbnail access |
| `firestore.indexes.json` | Indexes for the review queue and analytics queries |
| `functions/` | Lease reaper (scheduled, 60s) and the analytics poller |

## Before working here

Read the **Phase 1 addendum** in [`docs/PLAN.md`](../docs/PLAN.md). Two things matter:

1. **Almost none of Phase 1 needs a real Firebase project.** The Emulator Suite runs locally
   against a fake project id, with no billing and no network. Every Phase 1 exit criterion is
   emulator-backed.
2. **Project ids, buckets and emulator hosts are configuration only.** They live in `.env` and
   must never appear in source or committed config — the target project is expected to change.
