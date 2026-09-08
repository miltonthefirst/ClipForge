# ADR-0004 — Give ClipForge its own Firebase project, and engineer for near-zero spend

- **Status:** Accepted
- **Date:** 2026-09-08
- **Phase:** 1

## Context

`docs/PLAN.md`'s Phase 1 addendum recorded that the intended `ClipForge` Firebase
project was "blocked on upgrade issues", and directed Phase 1 to borrow the
existing **`miltongore`** project as a temporary gateway. It left the Spark vs.
Blaze question open, to be resolved when Phase 1 began.

Two things were discovered when Phase 1 actually began.

**`miltongore` is not a scratch project.** It hosts a live site: two hosting
targets (`miltongore.web.app` and `admin-miltongore.web.app`), a registered web
app, and a `(default)` Firestore database holding that site's data. Any ClipForge
mistake there is a production incident on someone's live site.

**A `ClipForge` project already exists** — `bytepic-clipforge` — and is empty:
Firestore API not enabled, Functions API not enabled, no registered apps. The
"blocked on upgrade" note was stale.

That prompted the real question: could ClipForge have safely shared `miltongore`
by namespacing its collections? Three facts say no.

1. **Firestore IAM is database-level, never per-collection**, and the worker uses
   the Admin SDK, which **bypasses security rules entirely**. No combination of
   rules and IAM could have stopped a ClipForge bug from writing over the
   neighbouring app's data. Rules would have constrained only the PWA.
2. **Firestore's no-cost tier applies to only one database per project** — the
   first one created. On `miltongore` that is already `(default)`, so a dedicated
   `clipforge` database there would bill from its first operation, while the
   first database created in a fresh project gets the free tier.
3. **A shared database shares the daily quota, not just the namespace.** A
   badly-scoped `onSnapshot` burning the 50K free daily reads would have taken
   the neighbouring app down with it — a failure mode namespacing does nothing
   about.

There is also a mundane hazard that no amount of application-level care fixes:
Firebase deploy commands **replace rather than merge**. `firebase deploy --only
firestore:rules` overwrites a project's entire rules file, `--only functions`
deletes deployed functions absent from local source, and `--only hosting`
overwrites the live site.

## Decision

ClipForge uses its own Firebase project, **`bytepic-clipforge`**. Nothing in this
repository targets `miltongore`.

The stated goal is Blaze-class capability at **as close to zero spend as
possible**. Blaze is pay-as-you-go *on top of* the same no-cost quotas Spark has,
so this is not about avoiding features — it is about staying inside the free
quotas and guarding the things that can run away. The following are therefore
requirements, not optimisations:

- The Emulator Suite is the development default (`CLIPFORGE_USE_EMULATORS=true`).
  The real project is touched deliberately, never incidentally.
- `onSnapshot` queries stay narrow. Every delivered document bills a read.
- Job-progress writes are throttled and event-log entries batched.
- Source video is never uploaded — only rendered clips and thumbnails (D3).
- Storage carries a lifecycle rule for rejected and stale clips.
- Where the lease reaper can run either as a worker task or a Cloud Function,
  the worker task is preferred. It is cheaper, and it was already the better
  design.
- A Cloud Billing budget with a Pub/Sub-driven kill switch. GCP budgets *notify*;
  they do not cap.

Every project id, bucket name and emulator host remains configuration only, in
`.env`. That rule — adopted in Phase 0 before any Firebase work — is what made
switching projects a one-line change rather than a refactor.

## Consequences

- The data model in `docs/PLAN.md` §3.2 stands unchanged. Generic top-level
  collections (`users/`, `jobs/`, `sources/`, `clips/`) are safe in a project we
  own outright; they would have been collision-prone inside `miltongore`.
- No uid allowlist is needed. A separate project means a separate Auth user pool,
  so the neighbouring app's users cannot sign in to ClipForge. Had we shared a
  project, rules would have had to gate on an explicit allowlist rather than
  `request.auth != null`.
- Isolation is structural rather than a discipline to maintain forever, but it is
  only as good as the project id in use. Every Firebase invocation passes an
  explicit `--project` sourced from `.env`, and **no bare `firebase deploy` is
  ever run**. This is documented in `CONTRIBUTING.md`.
- The Phase 1 addendum in `docs/PLAN.md` is obsolete and has been replaced by a
  pointer to this ADR. §2.2's claim that Blaze is enabled referred to
  `miltongore` and says nothing about `bytepic-clipforge`, whose billing tier is
  still unverified.
- Blaze remains unverified on the new project. This blocks neither Phase 1 nor
  Phase 2, because every exit criterion in both is emulator-backed. It decides
  only whether the reaper's Cloud Function binding is deployable and whether
  clips land in Firebase Storage or in the `BlobStore` port's alternative.
- The Firestore location must be chosen deliberately when the database is first
  created: it is **permanent**, and Cloud Storage's always-free tier is
  US-region-only.

## Alternatives considered

- **Namespace inside `miltongore`'s default database.** Free, and the option
  originally requested. Rejected on the three facts above: the Admin SDK bypasses
  the only mechanism that could have enforced the namespace, the quota is shared,
  and our `firestore.rules` would have had to contain the neighbouring app's
  rules too, since deploying replaces the whole file.
- **A second, named Firestore database inside `miltongore`.** Genuinely strong
  isolation — its own rules file, its own IAM scope. Rejected because it forfeits
  the no-cost tier for no benefit over a separate project, and still shares the
  Auth user pool.
- **Stay on Spark.** Moot once a dedicated project removed the pressure, and
  Blaze costs nothing until the free quotas are exceeded.
