"""Firing research on a schedule: when, and exactly once.

The arithmetic is tested against a clock and the firing against fakes that
answer the way the store's transaction does — fired, or somebody else got
there first. The transaction itself is the integration tier's business.
"""

# The fakes below stand in for the stores by shape, not by type.
# mypy: disable-error-code="arg-type"

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from clipforge.scheduler.schedule import ResearchScheduler, is_due, next_due
from clipforge_contracts import (
    Job,
    JobStatus,
    JobType,
    ResearchOptions,
    ResearchSchedule,
    ScheduleCadence,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 19, 9, 0, tzinfo=UTC)  # 10:00 in London, 05:00 in New York


def schedule(**over: Any) -> ResearchSchedule:
    defaults: dict[str, Any] = {
        "id": "sched-1",
        "uid": "user-1",
        "name": "Mornings",
        "enabled": True,
        "cadence": ScheduleCadence.INTERVAL,
        "every_hours": 12,
        "options": ResearchOptions(topics=["premier league"]),
        "next_due_at": NOW - timedelta(minutes=1),
        "created_at": NOW,
        "updated_at": NOW,
    }
    return ResearchSchedule(**{**defaults, **over})


# ── When ─────────────────────────────────────────────────────────────────────


def test_an_interval_counts_from_now_not_from_the_last_run() -> None:
    """A worker that was off for a week fires once, not seven times."""
    when, warning = next_due(schedule(every_hours=12), now=NOW)
    assert when == NOW + timedelta(hours=12)
    assert warning is None


def test_daily_is_the_next_occurrence_in_the_schedule_s_zone() -> None:
    later_today = schedule(cadence=ScheduleCadence.DAILY, at="07:30", timezone="America/New_York")
    when, _ = next_due(later_today, now=NOW)
    # 05:00 in New York now; 07:30 is still today, and 07:30 EDT is 11:30 UTC.
    assert when == datetime(2026, 9, 19, 11, 30, tzinfo=UTC)

    already_passed = schedule(cadence=ScheduleCadence.DAILY, at="07:30", timezone="Europe/London")
    when, _ = next_due(already_passed, now=NOW)
    # 10:00 in London now; 07:30 has gone, so tomorrow's, which is 06:30 UTC.
    assert when == datetime(2026, 9, 20, 6, 30, tzinfo=UTC)


def test_an_unknown_zone_falls_back_to_utc_and_says_so() -> None:
    when, warning = next_due(
        schedule(cadence=ScheduleCadence.DAILY, at="12:00", timezone="Mars/Olympus"), now=NOW
    )
    assert when == datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
    assert warning is not None and "Mars/Olympus" in warning and "UTC" in warning


def test_due_means_enabled_and_the_time_has_come_or_was_never_set() -> None:
    assert is_due(schedule(), now=NOW)
    assert is_due(schedule(next_due_at=None), now=NOW)
    assert not is_due(schedule(next_due_at=NOW + timedelta(minutes=1)), now=NOW)
    assert not is_due(schedule(enabled=False), now=NOW)


# ── Whether ──────────────────────────────────────────────────────────────────


class FakeScheduleStore:
    def __init__(self, rows: list[ResearchSchedule], *, lose_race: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self._rows = rows
        self._lose = set(lose_race)
        self.fired: list[tuple[str, Job, datetime, str]] = []
        self.recorded: list[tuple[str, str, datetime | None]] = []

    def enabled(self) -> list[ResearchSchedule]:
        return [row for row in self._rows if row.enabled]

    def fire(
        self, sched: ResearchSchedule, job: Job, *, next_due: datetime, now: datetime, outcome: str
    ) -> bool:
        if sched.id in self._lose:
            return False
        self.fired.append((sched.id, job, next_due, outcome))
        return True

    def record(
        self, schedule_id: str, *, outcome: str, next_due: datetime | None, now: datetime
    ) -> None:
        self.recorded.append((schedule_id, outcome, next_due))


class FakeJobStore:
    def __init__(self, jobs: dict[str, JobStatus]) -> None:
        self._jobs = jobs

    def get(self, job_id: str) -> Job | None:
        status = self._jobs.get(job_id)
        if status is None:
            return None
        return Job(
            id=job_id,
            uid="user-1",
            type=JobType.RESEARCH,
            status=status,
            stages=[{"name": "RESEARCH", "lane": "CPU", "status": "PENDING"}],
            attempts=0,
            max_attempts=2,
            created_at=NOW,
            updated_at=NOW,
        )


def scheduler(store: FakeScheduleStore, jobs: FakeJobStore | None = None) -> ResearchScheduler:
    return ResearchScheduler(
        schedules=store,
        jobs=jobs or FakeJobStore({}),
        clock=lambda: NOW,
    )


def test_a_due_schedule_fires_an_ordinary_research_job_that_names_it() -> None:
    store = FakeScheduleStore([schedule()])
    assert scheduler(store).tick() == ["sched-1"]
    _, job, when, outcome = store.fired[0]
    assert job.type is JobType.RESEARCH
    assert job.uid == "user-1"
    assert job.schedule_id == "sched-1"
    assert job.research_options is not None
    assert [str(getattr(t, "root", t)) for t in job.research_options.topics or []] == [
        "premier league"
    ]
    assert [stage.name.value for stage in job.stages] == ["RESEARCH", "CURATE"]
    assert when == NOW + timedelta(hours=12)
    assert outcome == "fired"


def test_a_schedule_that_is_not_due_yet_is_left_alone() -> None:
    store = FakeScheduleStore([schedule(next_due_at=NOW + timedelta(hours=1))])
    assert scheduler(store).tick() == []
    assert store.fired == [] and store.recorded == []


def test_a_run_still_going_defers_rather_than_piling_up() -> None:
    store = FakeScheduleStore([schedule(last_job_id="job-prev")])
    jobs = FakeJobStore({"job-prev": JobStatus.RUNNING})
    assert scheduler(store, jobs).tick() == []
    assert store.fired == []
    _, outcome, next_due_at = store.recorded[0]
    assert "still going" in outcome
    assert next_due_at == NOW + timedelta(minutes=15)


def test_a_finished_previous_run_does_not_block_the_next() -> None:
    store = FakeScheduleStore([schedule(last_job_id="job-prev")])
    jobs = FakeJobStore({"job-prev": JobStatus.COMPLETED})
    assert scheduler(store, jobs).tick() == ["sched-1"]


def test_losing_the_race_fires_nothing_and_records_nothing() -> None:
    store = FakeScheduleStore([schedule()], lose_race={"sched-1"})
    assert scheduler(store).tick() == []
    assert store.fired == [] and store.recorded == []


def test_a_timezone_warning_travels_with_the_outcome() -> None:
    store = FakeScheduleStore(
        [schedule(cadence=ScheduleCadence.DAILY, at="06:00", timezone="Nowhere/Land")]
    )
    scheduler(store).tick()
    assert "Nowhere/Land" in store.fired[0][3]
