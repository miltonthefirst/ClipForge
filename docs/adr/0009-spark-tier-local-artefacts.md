# ADR-0009 — Run on the Spark free tier; rendered clips stay on the worker

- **Status:** Accepted
- **Date:** 2026-09-08
- **Phase:** 2 (adopted before Phase 3)
- **Amends:** [ADR-0004](0004-dedicated-firebase-project.md), which assumed Blaze would be available

## Context

[ADR-0004](0004-dedicated-firebase-project.md) put ClipForge in its own Firebase
project and left the billing tier unverified, on the grounds that nothing in
Phases 1 or 2 needed it. That was true, and both phases shipped without it.

The tier question is now answered by a constraint rather than a preference:
**ClipForge must run with no Blaze plan.** Upgrading is not currently possible,
and waiting for it would stall the project indefinitely.

What that actually costs, verified 2026-09-08:

| Capability | On Spark |
| --- | --- |
| Firestore | ✅ 1 GiB, 50K reads / 20K writes per day |
| Auth (Google) | ✅ |
| FCM | ✅ |
| Hosting | ✅ 10 GB storage, 360 MB/day transfer |
| Cloud Functions | ❌ Blaze only |
| **Cloud Storage** | ❌ **No access to any bucket.** Since 3 February 2026 Cloud Storage for Firebase requires Blaze outright; on Spark, bucket API calls return 402/403 |

Cloud Functions were already a non-issue: [ADR-0006](0006-lease-based-job-claiming.md)
made the reaper a pure function bound to a worker task precisely so this stayed
open.

Cloud Storage is the real loss, and it lands on the one thing v0.1 exists to do:
review a rendered clip from your phone. Without a bucket there is no URL for a
phone to play.

## Decision

**Rendered clips never leave the worker.** They are written to the local
workspace, and the browser reaches them by whatever means is available.

Three things make that work rather than merely tolerable.

### 1. A `BlobStore` port with two adapters

All artefact writes go through one interface, selected by `CLIPFORGE_BLOB_STORE`:

- `local` — writes to the workspace, returns a filesystem path. The default.
- `firebase` — uploads to Cloud Storage, returns an object path. Written when
  Blaze arrives; it is one adapter, not a refactor.

The `Clip` contract carries **both** `localPath` and `playbackUrl` from day one,
plus a `location` discriminator. Both states are first-class in the schema now,
so switching does not migrate anything — it starts populating a field that
already exists.

### 2. Playback resolves through a documented precedence

The PWA asks one question — "what can this browser actually play?" — and answers
it in a fixed order:

1. **`playbackUrl`**, if set. Works from anywhere. Populated by Blaze Storage
   later, and equally by a tunnel (Tailscale, ngrok) if that ever appeals: the
   field is just "a URL a browser can fetch", and Storage is only one supplier
   of one.
2. **The worker's local file server**, if reachable. The worker serves its
   workspace read-only on `127.0.0.1:8765`, so opening the PWA *on the machine*
   gives full video review through the same UI. Browsers exempt `localhost` from
   mixed-content blocking, so this works even with the PWA served over HTTPS.
3. **Poster frame only.** What the phone gets today.

One component, three sources, no branching in the product logic. Upgrading to
Blaze lights up branch 1 and changes nothing else.

### 3. Enough goes to Firestore that a phone review is still real

A **poster frame and a four-frame filmstrip** are stored as base64 in
`clips/{clipId}/preview/poster` — a subcollection document, so the review-queue
query does not drag image bytes on every read. At roughly 40-60 KB each this sits
comfortably inside both the 1 MiB document limit and the 1 GiB free tier
(~20,000 clips), and it means the phone shows the hook, the score breakdown, the
transcript excerpt and *what the clip actually looks like* — not a placeholder.

**Transcripts move out of Firestore** and onto the worker. A word-level
transcript of a 60-minute video approaches the 1 MiB document limit, and the PWA
never needs one in full — `Candidate.transcriptExcerpt` already carries the part
a reviewer reads. Firestore keeps a `TranscriptRef`: language, duration, counts
and a local path.

## Consequences

- **The v0.1.0 milestone is restated honestly.** Approve/reject still happens
  from the phone, over Firestore, from anywhere — that half is unaffected. What
  changes is that the phone reviews against a poster frame and metadata, and the
  video itself is watched on the machine. `docs/PLAN.md` §1.1 is updated to say
  so rather than quietly under-delivering.
- **Publishing is entirely unaffected**, which is the happiest consequence.
  Decision D7 already kept the OAuth token on the worker, and the worker has the
  file locally — so Phase 8 needs nothing from Storage. v0.2.0 is not blocked.
- **`storage.rules` stays in the repository and stays tested** against the
  emulator, which does not care about billing. It is simply not deployed. When
  Blaze arrives it deploys as-is.
- Analytics (Phase 9) is unaffected: it is API reads into Firestore.
- Hosting is free, so the PWA can still be deployed publicly for the portfolio.
- The local file server must bind to `127.0.0.1` only, serve read-only, and
  confine every path to the workspace root. It is a convenience, not an
  authenticated surface, and must not become one by accident.
- A `backfill-storage` worker command becomes possible the day Blaze is enabled:
  walk the clips, upload, populate `playbackUrl`. It is not written now, but the
  contract already supports its result.

## Alternatives considered

- **Wait for Blaze.** Stalls the project on something outside our control, for a
  capability only one phase actually needs.
- **Store clip bytes in Firestore, chunked across documents.** Technically
  possible under the 1 MiB document limit and genuinely terrible: it burns the
  read quota, consumes the 1 GiB allowance in a few dozen clips, and abuses a
  document database as a blob store.
- **Deploy clips to Firebase Hosting.** Free and superficially clever, but it
  means a site deploy per clip, makes every clip world-readable, and 360 MB/day
  of transfer is a handful of views.
- **Expose the worker on the LAN or through a tunnel.** Would give full video on
  the phone at home. Deliberately *not* required, but explicitly accommodated:
  it populates `playbackUrl` through the same precedence above, so it can be
  added later as configuration rather than as code.
- **A different object store (Cloudflare R2, S3).** The `BlobStore` port makes
  this a third adapter, and it remains the fallback if Blaze never happens and
  remote playback becomes necessary. Not adopted now, because it adds an account,
  a credential and a bill to a project whose current constraint is precisely that
  it must not have any.
