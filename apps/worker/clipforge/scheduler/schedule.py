"""Firing research on a schedule, without deciding anything else.

A `ResearchSchedule` is a standing request: what to look for, and how often. The
worker turns it into an ordinary `RESEARCH` job at the right moment and stops
there. Nothing a schedule produces goes further than the Trends page — promoting
a video is still a person's press — so this is the one place in ClipForge that
starts work unasked, and it starts the one kind of work that changes nothing.

## Why the worker, and not the agent or a Cloud Function

For the reason the reaper is a worker task
([ADR-0006](../../docs/adr/0006-lease-based-job-claiming.md)): the project
deploys no server-side executor, and the agent deliberately does one thing. A
schedule therefore only fires while a worker is running — which is also the
only time the job it creates could run. A worker that was off for three days
fires each due schedule once when it comes back, not once per missed slot.

## Why the "when" is arithmetic here and the "whether" is a transaction there

`next_due` and `is_due` are pure so they can be tested against a clock. Firing
is a transaction in the store, because two workers can look at the same
schedule in the same minute and exactly one of them may create the job.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clipforge_contracts import JobStatus, ResearchSchedule, ScheduleCadence

from clipforge.observability import get_logger
from clipforge.stages.pipeline import new_research_job
from clipforge.store.firestore import JobStore, ScheduleStore

log = get_logger(__name__)

__all__ = ["ResearchScheduler", "is_due", "next_due"]

#: When the previous run is still going, look again this much later rather
#: than on every tick. Research runs take about a minute.
DEFER = timedelta(minutes=15)


def next_due(schedule: ResearchSchedule, *, now: datetime) -> tuple[datetime, str | None]:
    """When this schedule should fire after ``now``, and a warning if any.

    DAILY is the next occurrence of ``at`` in the schedule's zone — tomorrow's,
    if today's has passed — so "every morning at 07:30" survives a clock change
    the way a person expects. INTERVAL is ``now`` plus the interval, counted
    from now rather than from the last run: a worker that was off for a week
    should fire once, not seven times.
    """
    if schedule.cadence is ScheduleCadence.DAILY and schedule.at:
        warning: str | None = None
        zone: tzinfo = UTC
        try:
            zone = ZoneInfo(schedule.timezone or "UTC")
        except (ZoneInfoNotFoundError, ValueError):
            warning = (
                f"timezone {schedule.timezone!r} is unknown here, so {schedule.at} was read as UTC"
            )
        hours, minutes = (int(part) for part in schedule.at.split(":"))
        local_now = now.astimezone(zone)
        candidate = local_now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
        if candidate <= local_now:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC), warning

    hours = schedule.every_hours or 24
    return now + timedelta(hours=hours), None


def is_due(schedule: ResearchSchedule, *, now: datetime) -> bool:
    """Enabled, and either never scheduled or scheduled for a time that has come."""
    if not schedule.enabled:
        return False
    return schedule.next_due_at is None or schedule.next_due_at <= now


class ResearchScheduler:
    """One tick: fire whatever is due, exactly once each."""

    def __init__(
        self,
        *,
        schedules: ScheduleStore,
        jobs: JobStore,
        clock: Callable[[], datetime] | None = None,
        defer: timedelta = DEFER,
    ) -> None:
        self._schedules = schedules
        self._jobs = jobs
        self._clock = clock or (lambda: datetime.now(UTC))
        self._defer = defer

    def tick(self) -> list[str]:
        """Fire every due schedule. Returns the ids of the ones that fired."""
        now = self._clock()
        fired: list[str] = []
        for schedule in self._schedules.enabled():
            if not is_due(schedule, now=now):
                continue

            if self._previous_still_going(schedule):
                # Piling a second run behind one that has not finished would
                # rank the same feeds twice. Look again in a while, and say so
                # where the person who set the schedule will see it.
                self._schedules.record(
                    schedule.id,
                    outcome="deferred: the previous run is still going",
                    next_due=now + self._defer,
                    now=now,
                )
                continue

            due_next, warning = next_due(schedule, now=now)
            job = new_research_job(
                uid=schedule.uid, options=schedule.options, schedule_id=schedule.id
            )
            outcome = "fired" + (f"; {warning}" if warning else "")
            if self._schedules.fire(schedule, job, next_due=due_next, now=now, outcome=outcome):
                fired.append(schedule.id)
                log.info(
                    "schedule.fired",
                    schedule_id=schedule.id,
                    job_id=job.id,
                    next_due=due_next.isoformat(),
                )
            else:
                # Another worker got there first, or the schedule was edited or
                # disabled between the read and the write. Either way, not ours.
                log.info("schedule.lost_race", schedule_id=schedule.id)
        return fired

    def _previous_still_going(self, schedule: ResearchSchedule) -> bool:
        if not schedule.last_job_id:
            return False
        job = self._jobs.get(schedule.last_job_id)
        return job is not None and job.status in (JobStatus.QUEUED, JobStatus.RUNNING)
