# ADR-0018 — Blaze, and a short-lived bucket copy of each clip

- **Status:** Accepted
- **Date:** 2026-09-14 (recording a change made 2026-09-10)
- **Phase:** 7, carried forward
- **Supersedes:** [ADR-0009](0009-spark-tier-local-artefacts.md) in its central
  decision, and keeps the rest of it

## Context

[ADR-0009](0009-spark-tier-local-artefacts.md) chose to run on the Spark free
tier, because Cloud Storage for Firebase began requiring a paid plan in February
2026. Its consequence was stated plainly: a rendered clip never leaves the
worker, and a phone gets the poster frame, the hook, the scores and a transcript
excerpt — *"enough to approve or reject, which is the decision that actually
matters"*.

That held up for a while and then stopped. Reviewing football is not like
reviewing a talking head: a poster frame cannot tell you whether the crop kept
the ball, whether the narration lines up, or whether a logo is still in the
corner. Every correction built since — framing, hiding, re-voicing — is a
judgement about motion, and none of them can be made from a still.

The project moved to Blaze on 2026-09-10. This records what that changed, which
was never written down.

## Decision

**The bucket holds a copy for watching. The worker still holds the master.**

An `UPLOAD` job puts a clip in Cloud Storage and stamps `storagePath` and
`playbackUrl` on it, so the PWA plays real video on any device. `ClipLocation`
becomes REMOTE, and the clip page prefers the bucket copy when it exists and the
worker's own file server when it does not.

**Clips expire after five days** (`clip_retention_days`, enforced by a bucket
lifecycle rule applied with `tools/storage-lifecycle.ps1`, since the Firebase
CLI does not manage lifecycle). The copy exists so somebody can watch and
decide; a decision that has not been made in five days is not waiting on video.

The local file remains the master and is what every later job re-cuts from. That
is unchanged and is the half of ADR-0009 that was always right: the source of
truth is the machine that made it, and the bucket is a cache.

## What survives from ADR-0009

Nearly all of it, which is why this supersedes one decision rather than the
document.

- **The `BlobStore` port.** ADR-0009 predicted that enabling a paid plan later
  would be "one adapter and one environment variable, not a redesign". It was.
  That prediction is the reason this ADR is short.
- **Nothing is deleted to save money.** The lifecycle rule expires a cache, not
  a record: a clip whose bucket copy has gone still has its row, its scores and
  its file on the worker.
- **The free-tier read discipline.** Every listener is still bounded, because
  Blaze bills the same reads Spark rationed — it removes the cliff, not the
  cost.

## Consequences

**Billing is now possible**, which it was not before. The caps that mattered on
Spark are the ones that matter on Blaze: bounded listeners, a workspace size
cap, and a retention rule that stops the bucket growing without limit.

**A clip can be watched from a phone**, which is what made the correction
channel usable at all. ADR-0013's remake loop assumes the reviewer has seen the
problem; on Spark they frequently had not.

**Two places to look for a file.** `Clip.location` says which, and
`PlaybackService` resolves it. The Storage screen added in
[ADR-0017](0017-deleting-a-record-is-not-deleting-a-file.md) lists only the
local side, deliberately: the bucket copy expires on its own, and the disk does
not.

## Alternatives considered

**Stay on Spark and improve the poster.** A filmstrip and a longer transcript
excerpt were both tried. They make a clip easier to *recognise* and no easier to
*judge*, and judging is the thing the queue exists for.

**Keep clips in the bucket indefinitely.** Storage is cheap and unbounded growth
is not a cost problem, it is a tidiness problem — and ClipForge now has a whole
screen about tidiness. Five days matches how long a review actually takes.
