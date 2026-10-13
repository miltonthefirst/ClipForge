# ADR-0030 — Status on events and pings, not on timers

- **Status:** Accepted
- **Date:** 2026-10-13
- **Supersedes in part:** [ADR-0012](0012-machine-agent.md) (the agent's once-a-minute heartbeat and
  backstop read), [ADR-0006](0006-lease-based-job-claiming.md) (the reaper "bound to a periodic
  worker task", and the heartbeat refreshing `workers/{id}` while idle)
- **Related:** [ADR-0011](0011-local-control-api.md) (the loopback API the ping is relayed over),
  [ADR-0023](0023-research-on-a-schedule.md) (schedules, now listened to rather than read)

## Context

Firestore bills every read and every write, and every write is billed again as a read on each
listener open on that document. Nothing the PC did while idle was free:

| What | How often | Cost per day, idle |
| --- | --- | --- |
| Worker `claim_next()` poll of the `queued()` query | every 1 s | ~86,400 reads, plus an `expired()` query each time the queue was empty |
| Worker `_announce()` of `workers/{id}` | every 30 s | 2,880 writes, plus a read per open PWA listener per write |
| Worker reaper: `expired()` and the tidy's clip queries | every 60 s | ~4,300 queries |
| Worker research-schedule tick, `schedules where enabled` | every 60 s | 1,440 queries |
| Agent report of `agents/{id}` | every 60 s | 1,440 writes, plus a read per open listener |
| Agent backstop re-read of its own document | every 60 s | 1,440 reads |

All of it existed to answer two questions: *is there work?* and *is the machine there?* — and to
answer them continuously, whether or not anybody was asking.

The owner's requirement: status updates only when something actually happens (a job is claimed,
progresses, finishes; a process starts or stops), or when a client asks and the PC answers.

## Decision

**The PC listens, reacts to events, and answers pings. Nothing touches Firestore on a timer while
idle.**

### Finding work: listeners and local alarms

- **The queue.** `JobStore.watch_queued` holds the same bounded query `queued()` ran, as a listener.
  Each delivery replaces the worker's local view of the waiting jobs, and the claim loop looks at
  that view when it changes, when a job ends, on a ping, and when a scheduled job's `notBefore`
  comes due — a local alarm, not a read. A claim that was refused (another worker won it) is not
  retried until the listener delivers a new version of that job.
- **The reaper** runs at startup, on a ping, after each job ends, and at the moment a lease it has
  seen is due to lapse. It lists the RUNNING jobs (`JobStore.running()`), reaps the lapsed ones
  that are not its own, and sets a local alarm for the earliest lease somebody else holds. With
  nobody else running a job, there is no alarm. The tidy runs beside it, as before.
- **Research schedules.** `ScheduleStore.watch_enabled` delivers the enabled schedules; the worker
  sets a local alarm for the earliest `nextDueAt` and fires from the list it holds. A due time that
  was tried and did not advance is retried after five minutes rather than on every pass.
- **Listener health.** A listener the client library recovers on its own is fine; one it closes
  for good goes quiet, and quiet is also what an idle listener sounds like. Both the worker and the
  agent check `Subscription.alive()` — the library's own state, no request — every
  `CLIPFORGE_LISTENER_CHECK_SECONDS` (30), and replace a dead one. The replacement's first delivery
  is the sweep that makes up for whatever was missed.

### Saying what is happening: writes on events

- `workers/{id}` is written when the worker starts (ONLINE), claims a job (BUSY), a running stage
  says something new (at most once per lease renewal, so once per 30 s), a job ends, a ping is
  answered, and on shutdown (OFFLINE). Never while idle.
- `agents/{id}` is written when the agent starts, when the supervisor's state changes, and when a
  ping is answered.
- **Lease renewal stays on its interval** (`CLIPFORGE_HEARTBEAT_SECONDS`, 30), **only while a job
  runs.** That is work happening, and the lease is what keeps it correct. Idle, a renewal round is
  a dictionary lookup. `Settings.heartbeat_fits_in_lease()` still guards the interval.

### Asking whether the machine is there: the ping

A client writes a fresh random token to the agent's own document:

| Field (`agents/{agentId}`) | Written by | Meaning |
| --- | --- | --- |
| `pingToken` | client | 8-64 characters, random per ping |
| `pingRequestedAt` | client, `serverTimestamp()` | rules pin it to `request.time` |
| `pingRequestedBy` | client | rules pin it to `request.auth.uid` |
| `answeredPing` | agent | the last token it answered, with a fresh `lastSeenAt` |

| Field (`workers/{workerId}`) | Written by | Meaning |
| --- | --- | --- |
| `answeredPing` | worker | the last token the agent relayed to it; carried on every later write |

1. The agent's listener delivers the token at once. The agent re-probes the worker over loopback,
   and if one is running (`RUNNING`, `FOREIGN`, `STOPPING`) relays the token to it with
   `POST /worker/ping {token}` on the control API.
2. The agent writes its report with `answeredPing` and a fresh `lastSeenAt`.
3. The worker's claim loop writes `workers/{id}` with `answeredPing`, then reaps, tidies, and runs
   one real sweep of the queue (`claim_next()`) — the moment a listener that had stopped delivering
   without closing would otherwise go unnoticed.

The PWA (`core/presence.ts`) pings when the worker panel opens (Jobs and Worker pages), when
**Check now** is pressed, and before Start or Stop. It never re-pings on a timer. Each document is
`checking` until the token comes back — or the document's `lastSeenAt` moves, which is as good an
answer — and `offline` after 15 seconds without one. An agent that answers STOPPED or FAILED settles
the worker as `offline` at once, and an agent's "no worker" stamped later than the worker's last
write outranks it (both stamps come from the PC's clock): that is a worker that died without
writing OFFLINE. The panel shows "Checked 12s ago" instead of the 90 s / 180 s staleness windows.

**The throttle.** The rules accept a ping only if the document's previous `pingRequestedAt` is more
than 15 seconds old — one ping per machine per 15 s, whoever sends it. A client refused by the
throttle waits on the token already in the document, which answers the same question. This is what
stops a page reloaded in a loop from becoming a write-and-relay loop on the PC.

## Why the agent's document, and not a `pings/{agentId}` collection

The agent already holds a listener on `agents/{agentId}`, so a ping there costs no new listener on
the PC, and the PWA already listens to `agents` — so the answer arrives through a listener that is
open anyway. A separate collection would add a listener on each side, a second document to create
and secure, and a rule set to keep in step with this one, for no property the field-level rule does
not already give. The rules keep the ping its own narrow update branch (`changed().hasOnly` the three
ping fields), separate from the wish, so neither can ride on the other.

`pingRequestedBy` rather than reusing `requestedBy`: that field says who last asked for a start or a
stop, and the agent reads it as part of the wish. Somebody merely looking must not overwrite it.

## Why the agent relays to the worker, rather than the worker listening too

The worker could listen to `agents/{id}` itself. It would then hold a third listener, billed a read
for every write to that document — including every agent report — to learn about the one field it
cares about. The agent already talks to the worker's loopback API every ten seconds, which is free,
and already knows whether a worker is running. Relaying costs one loopback request per ping.

The price is that a worker started without an agent on the machine cannot be pinged. It still writes
its document on every event, and the panel shows that document as "last reported … — nothing on that
machine can be asked", rather than claiming it is or is not running.

`POST /worker/ping` is a second write route on the control API. It changes nothing about what the
worker does — it is asked to *say* something — so it does not widen the threat model in
[ADR-0011](0011-local-control-api.md); it is bearer-token authenticated like the rest, and the token is
validated to the same 64-character bound as the rules.

## Consequences

- **Idle costs nothing.** Measured against the emulator: two idle windows of 130 s each, zero RPCs
  from the worker and zero from the agent (see the change's verification notes).
- **A job is picked up faster**, not slower: the listener delivers in tens of milliseconds, where the
  poll waited up to a second.
- **"Is it on?" costs a ping.** One client write and roughly three listener deliveries on the PC side,
  one agent write, one worker write, one reaper/tidy pass and one queue sweep. Each panel opening
  asks at most once per 15 s per machine.
- **The answer is as of the ping.** A panel left open does not notice the PC switching off until
  somebody presses Check now or Start. That is the trade, and the panel says when it last checked.
- **A machine that crashes while nobody is looking is noticed later.** Jobs it held are recovered by
  the next worker to start (startup reap plus its lapse alarm), by a ping, or by another worker's
  lapse alarm — not by a reaper polling every minute.
- **Configuration.** `CLIPFORGE_AGENT_HEARTBEAT_SECONDS`, `CLIPFORGE_SCHEDULE_TICK_SECONDS` and
  `CLIPFORGE_REAPER_INTERVAL_SECONDS` are gone (an old `.env` that sets them is ignored).
  `CLIPFORGE_LISTENER_CHECK_SECONDS` is new and local-only. `CLIPFORGE_HEARTBEAT_SECONDS` keeps its
  name and now means lease renewal while a job runs.
- **Deploy order.** Rules first (the PWA's ping write needs them), then the agent and worker restarted
  on the new code, then hosting. An old agent ignores pings, so a new PWA against an old agent shows
  the machine as not answering.

## Alternatives considered

**Keep the heartbeats, slow them down.** Cheaper, still not free, and still the wrong shape: the cost
is paid all day so that a question asked a few times a day has a ready answer. And every slowdown
lengthens the staleness window the PWA has to wait out to call a machine off.

**A Cloud Function that answers "is it there?"** It cannot: only the PC knows, and the question has to
reach it. The PC already holds an outbound listener for exactly that.

**Presence via the Realtime Database's `onDisconnect`.** The classic answer to presence, and a second
database, SDK and rule set in a project that otherwise uses only Firestore. The ping answers the same
question on demand with what is already there.

**The worker listens to the agent's document as well.** Rejected above: a listener billed for every
agent write to learn one field, against a loopback request that is already being made.
