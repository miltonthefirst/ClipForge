# ADR-0023 — A research schedule is a document the worker fires, and it still decides nothing

- **Status:** Accepted
- **Date:** 2026-09-19
- **Phase:** 10
- **Extends:** [ADR-0021](0021-trend-research-as-a-job.md) (research as a job),
  [ADR-0006](0006-lease-based-job-claiming.md) (periodic work is a worker task),
  [ADR-0012](0012-machine-agent.md) (what the agent is for)

## Context

ADR-0021 built trend research as something a person asks for, and the plan's
Phase 10 forbids *autonomous execution*. The first day of use produced the
obvious request: the same question, every morning, without walking to the
button. Asking whether that crosses the line the plan draws is the right
question, and the answer is that it does not, as long as the line is drawn
where the plan drew it.

The plan's concern is **an uncalibrated scorer producing bad clips faster**.
A research run produces no clips. It produces a list a person reads, and the
worst a scheduled list can do is be ignored. The autonomy the plan holds back —
promoting, compiling, publishing unattended — stays held back.

## Decision

**A schedule is a `ResearchSchedule` document, owned by the client, fired by
the worker, and it creates an ordinary `RESEARCH` job.** Three consequences
follow from each of those clauses.

### The worker fires it

For the reason the reaper lives in the worker rather than in a Cloud Function:
the project deploys no server-side executor, and the agent deliberately does one
thing. A schedule therefore fires only while a worker is running — which is
also the only time the job it creates could run, so nothing is lost by the
restriction and nothing is gained by lifting it. The Trends page says so under
the list of schedules.

A worker that was off for a week fires each due schedule **once** when it comes
back, not once per missed slot. `next_due` counts an interval from now rather
than from the last run for exactly this reason.

### The client owns the request, the worker owns the record

`name`, `enabled`, `cadence`, `everyHours`, `at`, `timezone`, `options` and
`nextDueAt` are the request; `lastRunAt`, `lastJobId` and `lastOutcome` are
what the worker did with it. The rules pin a client to the first set, and refuse
a schedule that arrives claiming to have already run. Any approved member may
edit any schedule, as with jobs: one workspace, one channel's worth of curiosity.

`nextDueAt` is set by the client on creation so the first run is predictable —
an interval schedule fires on the worker's next look, a daily one at its next
time of day — and rewritten by the worker after each firing. The worker
recomputes daily occurrences in the schedule's own zone, which is the zone of
the browser that set it up; an unknown zone falls back to UTC and says so in
`lastOutcome` rather than silently.

### Firing is a transaction

Two workers can read the same due schedule in the same minute. `ScheduleStore.fire`
re-reads the schedule inside a transaction, checks it is still enabled and still
due at the instant it was read at, and writes the job and the advance together.
The loser writes nothing. This is the claim protocol again, applied to a
document that creates a job rather than to the job itself, and it is tested
against the emulator because the property belongs to Firestore's transactions
and not to any fake.

A run still going is not fired over. The tick checks `lastJobId` and, if that
job is queued or running, moves `nextDueAt` fifteen minutes on and writes why.
Ranking the same feeds twice in a row helps nobody.

### The job is indistinguishable from a manual one, except that it says so

`Job.scheduleId` is the only difference. The Trends page marks such runs
*auto* in the run picker. Everything downstream — the list, the videos, *Clip
it*, *Compile this* — is the same code path with the same person at the end of
it.

## Consequences

**Two cadences, deliberately.** `INTERVAL` (every N hours, six to a week) and
`DAILY` (a wall-clock time in a named zone). Weekly-on-Tuesdays and cron
expressions were considered and refused: the feeds this reads are daily, and a
schedule language richer than the data it schedules is complexity with nothing
behind it.

**Six hours is the floor.** Google Trends and a subreddit's top-of-day move on
the scale of a day; a run every hour would rank the same signals twelve times.

**`tzdata` is a core dependency.** Python's `zoneinfo` reads the system
database on Linux and macOS and finds nothing on Windows — including no `UTC` —
and the reference machine is Windows. Without it every daily schedule would
fall back to UTC and every `lastOutcome` would say so.

**Off by a setting, per worker.** `CLIPFORGE_RESEARCH_SCHEDULES_ENABLED=false`
makes a worker ignore every schedule, for a second worker that should only run
jobs or a machine that must never start work unasked. The default is on,
because a schedule is something a person set up on purpose and a worker that
ignored it would be the surprise.

## Alternatives considered

**Let the agent fire it.** The agent runs even when no worker does, so a
schedule could fire on time and the job wait for a worker. It would also mean
the agent — a supervisor that starts and stops one process — growing a second
job and a Firestore query, for a gain of nothing: the job could not run until
the worker was up anyway.

**A Cloud Function on a cron.** Genuinely the right tool for a schedule, and the
one dependency the project has refused since ADR-0006. It would also be the
first server-side executor, and everything about the lease protocol assumes
there is none.

**Fire on the reaper's cadence.** One fewer thread. But the reaper's interval
is a lease-protocol number, and a person switching a schedule off wants the
next tick to notice within a minute, not within whatever that number is.

**Schedule the compilation too.** The next request, and the one the plan holds
back. A compilation is a rendered clip and a rendered clip is the thing the
scorer has not yet earned the right to make unattended. Phase 17's question.
