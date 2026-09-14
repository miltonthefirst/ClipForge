# ADR-0020 — Removing the rights attestation

- **Status:** Accepted
- **Date:** 2026-09-14
- **Supersedes:** the rights gate described in [PLAN.md Phase 8](../PLAN.md#phase-8--publishing-with-a-rights-gate)
- **Amends:** [ADR-0010](0010-worker-held-publishing-credentials.md) (what a `Publication` records)

## Context

Phase 8 shipped a rights gate. Before a clip could be published, the operator
had to record a basis — `OWN_CONTENT`, `LICENSED`, `PERMISSION_GRANTED`,
`FAIR_USE_ASSERTED` or `PUBLIC_DOMAIN` — attributed to them and timestamped,
re-confirmed after a year, with written reasoning required for fair use. It was
enforced in three places: Firestore rules, the worker, and advisorily in the
PWA. A `MUSIC` job carried a second attestation for the track, and a scored clip
had to answer for both halves of the file.

The operator asked for it to be removed. They are the only user of this system,
the only person who decides what goes on their channel, and being asked to
restate that decision per clip — and again per music track — added a click to
every publish without adding a fact.

**The honest reading of what the gate achieved.** Phase 8 recorded its own
counter-evidence: a clip published under `PERMISSION_GRANTED` was claimed by
Content ID anyway. That was never a contradiction. An attestation is not a
shield and was never described as one; it is an audit trail, and a rights basis
does not change what a matching algorithm does with the picture. Which leaves
the question of who the audit trail was for. On a multi-tenant product with a
compliance story it is for someone: a reviewer, a takedown response, a
colleague. On a single-operator system it is the operator writing themselves a
note about a decision they had just made and were about to act on.

## Decision

**Remove it entirely.** Not a setting, not a default — the field, the enum, the
rules, the UI and the tests all go.

What remains is the condition that was always doing the load-bearing work:

1. `CLIPFORGE_PUBLISHING_ENABLED` is false by default, so a fresh install cannot
   post to the internet by accident.
2. A clip must be `APPROVED` — a person watched it and said yes.

Both are still enforced in two independent places, and that duplication is still
the point: the worker holds Admin credentials and bypasses security rules
entirely, so rules alone would protect nothing on the path that uploads.
The worker's `publish/rights.py` became `publish/gate.py`, and the PWA's
`core/rights.ts` became `core/publishable.ts` — the same shape, minus the
attestation.

Responsibility for the underlying question is unchanged and is stated where it
belongs — in the README and the terms — rather than collected as a field.

## Living with documents written before the removal

Nothing was migrated, and nothing needs to be. Every clip, job and publication
already in Firestore still carries `rights`, and three separate mechanisms have
to tolerate that:

**The worker's reader.** `_read` drops fields the models do not declare, which
already existed for the opposite case — an older worker meeting a newer schema.
Fixing it for this case exposed a real bug: it read only the first segment of
the error path, so an unknown field *inside* a nested object stripped the whole
object. `musicOptions.rights` would have taken all of `musicOptions` with it,
and a queued MUSIC job would have failed claiming it had never been given any
options — a lie about a document that was merely older than the code. It now
prunes at the depth the field actually occurs.

**The security rules.** They no longer read `rights` at all, so a clip that
still has one publishes on the same terms as one that never did — including a
clip whose attestation would have failed the old gate, because nothing evaluates
it any more. What the rules do still refuse is a *new* write carrying the field:
`changed().hasOnly([...])` on clips and `keys().hasOnly([...])` on music options
both drop it. That asymmetry is deliberate. Old documents are history and must
keep working; a client still sending the field is running old code, and
accepting it silently is how a schema and its writers drift apart.

**The UI.** TypeScript is structural, so an extra property on a document is
inert at runtime. Nothing renders it, which is the whole intent.

## Consequences

**Publishing is one press from an approved clip.** That was the request.

**The `Publication` record is a smaller audit log.** It still answers *what went
out, to which channel, when, at what privacy, and at what quota cost* — it no
longer answers *on what basis*. ADR-0010's description of that record is amended
accordingly; nothing about credential handling changes.

**A scored clip is no longer gated twice.** Adding music needs a source, a mode
and a caption choice, and nothing else.

**The judgement did not go anywhere.** Publishing someone else's footage is
still the operator's call and still carries the same risk it did. What changed
is that the system stopped asking them to type it in.

## Alternatives considered

**Make it optional.** An attestation nobody is required to make is not an audit
trail; it is a field that is sometimes filled in, which is worse than either
alternative because it looks like evidence and is not.

**Keep it and default it.** A pre-filled basis is a lie with a timestamp on it.
If the answer can be defaulted, it was never being asked.

**Keep the enum, drop the enforcement.** This was tempting because it preserves
the *record* without the click. But an unenforced field on a single-operator
system is a column nobody reads, and the cost of a schema is not the storage —
it is every rule, model, form and test that has to keep knowing about it.
