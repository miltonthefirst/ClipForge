# ADR-0010 — Publishing credentials live on the worker, never in Firestore

- **Status:** Accepted
- **Date:** 2026-09-09
- **Phase:** 8
- **Implements:** decision **D7** from [docs/PLAN.md](../PLAN.md) §3.1
- **Related:** [ADR-0004](0004-dedicated-firebase-project.md) (the worker bypasses security rules),
  [ADR-0009](0009-spark-tier-local-artefacts.md) (artefacts already stay local)

## Context

Phase 8 gives ClipForge the ability to upload a finished clip to YouTube. That
needs an OAuth refresh token for the operator's Google account, and it needs it
available **unattended** — the worker publishes on a schedule, possibly at 04:00,
with nobody present to type anything.

A refresh token for the `youtube.upload` scope is the most dangerous secret in
the project by a wide margin. It does not expire in normal use, and it authorises
uploading to — and deleting from — a real channel with a real audience.

The obvious place to put it, given everything else about the system, is
Firestore: the PWA and the worker already share state there, it is already
per-user, and it is already protected by security rules. That is the option this
ADR rejects.

Three things make Firestore the wrong home for it:

1. **Security rules do not apply to the worker.** The worker authenticates with
   the Admin SDK, which bypasses `firestore.rules` entirely
   ([ADR-0004](0004-dedicated-firebase-project.md)). The rules that would
   "protect" the token are not enforced on the one component that reads it.
2. **Firestore is replicated, remote and backed up.** A credential there is a
   credential in a datacentre, in whatever exports exist, and in whatever the
   emulator wrote to disk during a test run.
3. **The PWA has no need for it.** The phone approves clips; it does not upload
   them. Putting the token where the client can reach it grants access nothing
   in the design requires.

## Decision

**The OAuth refresh token is stored in a file on the worker machine, encrypted at
rest, and is never written to Firestore, never sent to the PWA, and never
logged.**

What crosses Firestore is a publish **intent** — a `PUBLISH` job naming a clip —
and an audit record of what happened. The worker holds the credential and decides
whether to act.

Concretely:

- `clipforge/publish/credentials.py` reads and writes the token file. The key is
  derived from machine-local material (hostname, architecture, username), not
  from a passphrase, because the worker must run unattended.
- `Publication` documents record the video id, the privacy setting, the quota
  spent and the rights attestation. They carry no token, and no upload session
  URL — a session URL is a bearer capability for one upload, so it is
  checkpointed on the job document (owner-readable) rather than on the audit log.
- Phase 8 exit criterion 4 is a test, not a promise:
  `test_no_oauth_token_reaches_firestore` runs a full publish with a real token on
  disk and then walks *every document* the emulator holds — subcollections
  included — asserting the token does not appear. It is written as an explicit
  walk rather than a collection-group query, because a collection-group query
  would only search the subcollections someone remembered to name.

## On the encryption, precisely

The encryption is a keystream cipher over SHA-256 counter blocks, with a key
derived from machine and user identity. It is dependency-free by choice.

**What it protects against:** the token being read out of a backup, a synced
folder (OneDrive, Dropbox), a copied disk image, or over someone's shoulder in a
terminal. Also against a file accidentally committed to the repository being
usable by anyone else.

**What it does not protect against:** an attacker who can already execute code as
this user on this machine. They can derive the same key, because the worker can.

That distinction is stated here, in the module docstring, and in the README,
because the failure mode of over-claiming is worse than the failure mode of not
encrypting at all: a credential store trusted further than it deserves invites
decisions — syncing the folder, checking it into a private repo — that the real
guarantee does not support.

The honest alternative would be an OS keychain (DPAPI on Windows, Keychain on
macOS, Secret Service on Linux). That is the right upgrade if ClipForge ever runs
somewhere shared. It was not done now because it is three platform-specific
implementations for a single-user machine, and because the meaningful boundary on
this deployment is "can you run code as this user", which a keychain does not move.

## Consequences

**Good.**

- The blast radius of a Firestore compromise excludes the channel entirely.
- The PWA needs no publishing scope, so a stolen phone session cannot upload.
- Revocation is local and instant: delete one file, or revoke the grant at
  `myaccount.google.com/permissions`.
- The audit trail can be world-readable-to-its-owner without leaking anything,
  which is what makes exit criterion 5 achievable.

**Bad, and accepted.**

- **Publishing only works when the worker machine is on.** On the free tier the
  clips are already local ([ADR-0009](0009-spark-tier-local-artefacts.md)), so
  this adds no new limitation — but it would become one under Blaze, and the
  answer then is still the worker, not a Cloud Function holding the token.
- **The token file does not survive a machine change.** The key is bound to the
  machine and user, so a restored backup on a new laptop will not decrypt. This
  is deliberate, and the error message says so and names the command to re-run.
- **Re-authorisation every 7 days while the OAuth consent screen is in
  "Testing" mode.** The upload scope is *sensitive*, so leaving Testing requires
  Google verification. `clipforge-worker youtube-auth` is built for that path
  first, and a rejected refresh token produces a message naming the command
  rather than an opaque `invalid_grant`.

## Alternatives considered

**Token in Firestore, protected by rules.** Rejected: the Admin SDK bypasses
rules, so the protection does not apply where it matters.

**Token in an environment variable.** Rejected: environment variables leak into
crash dumps, process listings and CI logs, and there is nowhere durable to put one
that is not itself a file with worse properties.

**A passphrase-derived key.** Rejected: it makes unattended scheduled publishing
impossible, which is a stated Phase 8 requirement. A passphrase cached in memory
across restarts would be the same guarantee with more machinery.

**OS keychain.** Deferred, not rejected — see above. Worth revisiting if
ClipForge is ever run on a shared or multi-user machine.
