# ADR-0011 — A loopback control API, so credentials can be typed into the app without leaving the machine

- **Status:** Accepted
- **Date:** 2026-09-10
- **Extends:** [ADR-0010](0010-worker-held-publishing-credentials.md), which put publishing credentials on the worker
- **Related:** [ADR-0009](0009-spark-tier-local-artefacts.md) (the read-only file server this is deliberately *not* part of)

## Context

[ADR-0010](0010-worker-held-publishing-credentials.md) keeps the YouTube client
secret and refresh token on the worker and out of Firestore. That decision stands
and the reasoning has not changed: a refresh token can upload to and delete from
a real channel indefinitely, and Firestore is a shared, replicated, remotely
readable store whose rules the Admin SDK bypasses anyway.

Taken literally, though, it forces a bad experience. Connecting a channel means
editing `.env` and running a terminal command — for something an operator does
once per channel, from a machine where they are otherwise using a GUI. The
operator asked, reasonably, to type it into the app instead.

The obvious way to grant that is to put the secret in Firestore so the app can
write it. That reverses ADR-0010, and no amount of encryption fully rescues it:

- **Encrypted with a key the app holds** — the key ships in a public JS bundle.
- **Encrypted with the user's password** — then the worker needs that password to
  refresh a token at 04:00, which is exactly the unattended case ADR-0010 exists
  to protect.
- **Envelope-encrypted with a worker-held public key** — this genuinely works,
  and is the right answer *eventually*, when the secret must be settable from a
  phone. It is also real cryptography with real key-rotation obligations.

There is a simpler observation available first. **The desktop shell runs on the
same machine as the worker.** A secret typed there does not need to travel over
the internet at all; it needs to travel about two inches.

## Decision

**The worker exposes a small HTTP control API on `127.0.0.1`, authenticated with
a bearer token, which the desktop app uses to hand it credentials.**

The secret goes from a form in the Tauri webview, over loopback, to a file on
that machine. It never reaches Firestore, and ADR-0010 holds unchanged.

Three properties are load-bearing:

**It is a separate server from `localserver.py`.** That one is deliberately
read-only and its own docstring says it is "a convenience, not an authenticated
surface". Accepting credentials on the same port would have quietly made that
sentence false for anyone who read it later. Two servers, two obvious sets of
properties.

**It binds `127.0.0.1` unconditionally.** The file server's host is configurable
because a port might clash. This one is not, because there is no version of
exposing it that is a supported mode, and a configurable host is an invitation.

**Routes are injected, not defined there.** `localapi.py` knows about transport
and trust; `publish/channels.py` knows about YouTube. Either can be read, and
tested, without the other.

## The threat model

Loopback is not a security boundary, and writing this down is most of the point
of the ADR.

| Threat | Answer |
| --- | --- |
| Any other process running as this user | Bearer token, generated once, owner-only file. A process that could read it could read the token store beside it anyway, so this adds no new trust — it just removes the *free* access that binding a port would otherwise grant |
| A malicious web page issuing cross-origin requests to 127.0.0.1 | The token, which a page cannot read, **and** an `Origin` allowlist containing only the desktop shell. Both, because CORS does not constrain a non-browser client and a token alone does not stop a page that has somehow obtained one |
| DNS rebinding — a hostile name resolved to 127.0.0.1, so the page's origin looks local | The `Host` header must be a loopback literal. A rebound request still carries the attacker's hostname |
| A large or malformed body used to make the worker allocate | 64 KB cap and strict JSON parsing before any handler runs |

**What this does not defend against is malware already running as this user.** It
can read the token file, and it could read the credential store without going
near this API. That is out of reach here, as it was in ADR-0010, and claiming
otherwise would be worse than saying so.

`apps/worker/tests/unit/test_localapi.py` exercises each row above against a real
socket rather than by calling the handler directly — what is under test is header
handling, and a test that built the headers itself would only be testing its own
assumptions.

## Consequences

**Good.**

- Connecting a channel is a form, and the secret still never leaves the machine.
- ADR-0010 is unchanged rather than eroded; the credential boundary is where it
  was.
- The worker gains a place for future local-only operations — trigger a re-auth,
  disconnect a channel — that have the same "must be on this machine" shape.
- The split between *secret* and *describable* is now explicit in code: secrets
  to disk, connection state and defaults to Firestore, and a reviewer can see
  which is which.

**Bad, and accepted.**

- **Connecting a channel is a desktop task.** From a phone the settings page can
  show state and edit defaults, but not set the secret. Stated in the UI rather
  than left to be discovered.
- **A new listening port**, with the threat model above. Small, but not nothing,
  and it is why the guards are tested rather than assumed.
- **A second way to do the same thing.** `youtube-auth` and this API both write
  the same client file. They converge immediately — the file is identical either
  way — but two paths is still two paths.

## Alternatives considered

**Secret in Firestore, plaintext.** Rejected: reverses ADR-0010 for convenience.

**Secret in Firestore, envelope-encrypted with a worker-held public key.**
Deferred, not rejected. It is the correct answer to "set it from my phone", and
the operator has said that will be wanted later. It is also asymmetric crypto,
key rotation, and a migration — worth doing when the requirement is real rather
than anticipated, and the `channels` collection is already shaped to receive it.

**Tauri-only, no HTTP: a Rust command writing the file directly.** Tempting, and
simpler. Rejected because the worker owns that file's format and location, and a
second writer in another language is a duplication that drifts. It would also
give the *app* the ability to write credentials whether or not a worker is
running, which is a worse boundary than asking the worker to do its own job.
