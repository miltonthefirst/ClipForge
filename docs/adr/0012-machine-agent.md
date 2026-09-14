# ADR-0012 — An agent on the machine, so the worker can be started from a phone

- **Status:** Accepted
- **Date:** 2026-09-12
- **Related:** [ADR-0006](0006-lease-based-job-claiming.md) (why there is no server-side executor), [ADR-0011](0011-local-control-api.md) (the loopback API this uses and deliberately does not expose), [ADR-0009](0009-spark-tier-local-artefacts.md) (the free-tier posture this stays inside)

## Context

ClipForge has no server-side executor. The claim loop and the reaper both live
inside the worker process ([ADR-0006](0006-lease-based-job-claiming.md)), so a
job stays `QUEUED` until something on the worker machine picks it up. Two things
could do that: a terminal, and the desktop shell.

Both require being at the machine. That left the PWA half-useful in a specific
and annoying way: a phone could submit work, watch the queue and review the
result, but the moment between submitting and watching needed somebody to walk to
the PC and press Start. The operator's words were "I need to be able to start the
worker from my web" — and the honest answer at the time was that a browser tab
cannot spawn a process and no amount of API design changes that.

What changes it is putting something on the machine that is already listening.

## Decision

**A small supervisor process — the agent — starts with Windows, watches one
Firestore document, and starts or stops the worker according to what it says.**

- `agents/{machineId}` holds a **wish** (`desired: RUNNING | STOPPED`, plus who
  asked and when) and a **report** (what is actually happening: run state, pid,
  exit code, the worker's last output lines).
- The PWA writes the wish. `firestore.rules` lets an approved member write those
  three fields and nothing else, so the rule this project is built on holds: the
  client may create work and review results, and may never write pipeline state.
- The agent writes the report, through the Admin SDK, and never writes the wish.
  A `merge` that omits those three fields means a Start pressed during a
  heartbeat cannot be undone by a report assembled a second earlier.
- The agent runs as a **Scheduled Task at logon**, installed by
  `tools/agent.ps1 -Install -Live`.

It lives in the worker's package (`clipforge/agent.py`, `clipforge-worker
agent`), reusing its settings, its Firestore adapter and its virtual environment.
No new language, no new dependency, no second deployment story.

## Why a wish in Firestore rather than a port on this machine

The direct design is to let the phone call the worker's control API. It cannot,
and should not be able to: that API binds loopback and refuses everything else,
with a threat model written down in [ADR-0011](0011-local-control-api.md).
Reaching it from a phone means exposing a token-authenticated *write* surface to
the LAN, or a tunnel, or dynamic DNS — real infrastructure standing behind a
button, and a standing invitation to get the guards subtly wrong.

Firestore is already there, already authenticated, already knows who is approved,
and already works everywhere the PWA works. The agent holds the connection
**outbound**, so nothing on this machine listens for the internet and the attack
surface does not grow at all.

It also answers a question the existing signals cannot. A worker heartbeat says
what a worker believes it is doing and stops entirely when no worker is running —
so "the PC is on and waiting" and "the PC is off" look identical from a phone.
The agent's own heartbeat separates them, which turns out to be the thing
somebody staring at a stalled queue most wants to know.

## Why the agent supervises rather than owns

Three rules, each with a failure behind it:

**It asks the worker to stop; it kills only what it started, only after the grace
period, and only when asking did not work.** A killed worker skips the shutdown
that returns its in-flight jobs to the queue and flips its heartbeat to OFFLINE,
leaving a job `RUNNING` behind a lease nobody is renewing — recoverable only by
the reaper, which lives in the worker that is no longer running. The difference
is "requeued now" versus "requeued whenever somebody next remembers".

**A worker it did not start is reported, never duplicated.** Two workers on one
machine are *safe* — that is what the lease protocol is for — and would still
fight over one GPU lane. The agent probes the control port before spawning and
adopts what it finds as `FOREIGN`.

**A standing `STOPPED` does not stop a worker somebody else started.** Only a
*fresh* request does. Without this, an agent restarting beside a worker started
at the machine ten minutes earlier would shut it down on the strength of a wish
from yesterday. The rules require `requestedAt` on every write for the same
reason, and the same freshness is what lets pressing Start clear an agent that
has given up — so recovery is a button somebody is already pressing rather than a
second control to explain.

**It does not stop the worker when it exits.** The agent supervises the worker;
it does not own it. Killing a render because the supervisor was restarted is a
worse outcome than the one being avoided, and the next agent to start adopts what
it finds.

## Why a logon task rather than a Windows service

A service runs as SYSTEM, in a different session, with a different `HOME` — so
the uv installation, the service-account key, and the user-scoped GPU context all
have to be re-provisioned before it works at all. What that buys is running
before anyone logs in, which a machine that logs in automatically does not need.
Every one of those is a real cost against a benefit this deployment does not
have.

## Consequences

- **The phone becomes sufficient.** Submit, start the worker, watch, review.
  That is the whole loop, from anywhere, with nothing new exposed.
- **A second heartbeat.** One write a minute per machine, plus a read a minute as
  a backstop against a listener that has silently died. Comfortably inside the
  free daily quota, and deliberately slower than the worker's own 30 seconds —
  this one says "the PC is on", it does not hold a lease.
- **A third thing that can be running.** Worker, desktop shell, agent. The panel
  reconciles all three rather than showing whichever it asked last, and a
  disagreement between them is itself informative.
- **The desktop's Start now also writes the wish.** Not redundancy: the agent
  reconciles what is running against what was asked for, so a worker started
  behind its back under a standing `STOPPED` would be adopted as foreign rather
  than owned. Writing both keeps them agreeing about what the person asked.
- **A start that fails is diagnosable from the phone.** The worker's last output
  lines travel with the report, which is the difference between "it did not
  start" and a reason.
- **Windows only, for now.** The supervisor is portable Python; the installer is
  Task Scheduler. A `launchd` plist or a systemd user unit would be a small
  addition to `tools/`, not a redesign.

## Alternatives considered

**Expose the loopback control API to the LAN.** Rejected: it reverses
[ADR-0011](0011-local-control-api.md) for convenience, only works on the same
network, and mixed content blocks an HTTPS-served PWA from calling it anyway.

**A Cloud Function that sends a push the machine acts on.** Rejected as more
moving parts for the same result. The project is on Blaze now, so it is possible;
it would mean a deployed function, a push channel, and a second place where
"start the worker" is implemented.

**Poll Firestore on an interval instead of a listener.** Rejected as the primary
mechanism and kept as the backstop. A listener bills a read per *delivered*
document and delivers nothing while nothing changes, so it is both cheaper and
instant. The once-a-minute read that runs beside it is insurance against the
listener dying quietly, which is the one failure that would make the whole thing
unreliable.

**Keep the worker running permanently and pause it instead.** Rejected: the
worker holds a GPU and loads models, and "stop the worker" is usually a wish
about electricity and VRAM rather than about the queue.

**A tray icon.** Rejected for this purpose — it solves being at the machine,
which was never the problem.
