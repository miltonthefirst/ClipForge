"""Firing a schedule is one transaction: the job and the advance, or neither.

Against the emulator, because the property under test — two workers reading
the same due schedule and exactly one creating the job — is a property of
Firestore's optimistic transactions and cannot be faked meaningfully.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from clipforge.config import Settings
from clipforge.stages.pipeline import new_research_job
from clipforge.store.firestore import JobStore, ScheduleStore
from clipforge_contracts import ResearchOptions, ResearchSchedule, ScheduleCadence
from google.cloud import firestore

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 19, 9, 0, tzinfo=UTC)


@pytest.fixture
def schedules(settings: Settings, client: firestore.Client) -> ScheduleStore:
    return ScheduleStore(client, settings)


@pytest.fixture
def jobs(settings: Settings, client: firestore.Client) -> JobStore:
    return JobStore(client, settings)


def seed(client: firestore.Client, **over: object) -> ResearchSchedule:
    defaults: dict[str, object] = {
        "id": "sched-1",
        "uid": "user-1",
        "name": "Mornings",
        "enabled": True,
        "cadence": ScheduleCadence.INTERVAL,
        "every_hours": 12,
        "options": ResearchOptions(topics=["f1"]),
        "next_due_at": NOW - timedelta(minutes=1),
        "created_at": NOW,
        "updated_at": NOW,
    }
    schedule = ResearchSchedule(**{**defaults, **over})
    client.collection("schedules").document(schedule.id).set(
        schedule.model_dump(by_alias=True, mode="python")
    )
    return schedule


def test_firing_creates_the_job_and_advances_the_schedule_together(
    schedules: ScheduleStore, jobs: JobStore, client: firestore.Client
) -> None:
    schedule = seed(client)
    job = new_research_job(uid="user-1", options=schedule.options, schedule_id=schedule.id)

    assert schedules.fire(
        schedule, job, next_due=NOW + timedelta(hours=12), now=NOW, outcome="fired"
    )

    assert jobs.get(job.id) is not None
    after = schedules.get("sched-1")
    assert after is not None
    assert after.last_job_id == job.id
    assert after.last_run_at == NOW
    assert after.next_due_at == NOW + timedelta(hours=12)
    assert after.last_outcome == "fired"


def test_only_one_of_two_workers_reading_the_same_schedule_fires_it(
    schedules: ScheduleStore, jobs: JobStore, client: firestore.Client
) -> None:
    schedule = seed(client)
    first = new_research_job(uid="user-1", options=schedule.options, schedule_id=schedule.id)
    second = new_research_job(uid="user-1", options=schedule.options, schedule_id=schedule.id)

    assert schedules.fire(schedule, first, next_due=NOW + timedelta(hours=12), now=NOW, outcome="a")
    # The second worker read the schedule before the first advanced it.
    assert not schedules.fire(
        schedule, second, next_due=NOW + timedelta(hours=12), now=NOW, outcome="b"
    )

    assert jobs.get(first.id) is not None
    assert jobs.get(second.id) is None
    after = schedules.get("sched-1")
    assert after is not None and after.last_job_id == first.id


def test_a_schedule_switched_off_in_the_meantime_does_not_fire(
    schedules: ScheduleStore, jobs: JobStore, client: firestore.Client
) -> None:
    schedule = seed(client)
    client.collection("schedules").document("sched-1").update({"enabled": False})
    job = new_research_job(uid="user-1", options=schedule.options, schedule_id=schedule.id)
    assert not schedules.fire(schedule, job, next_due=NOW, now=NOW, outcome="x")
    assert jobs.get(job.id) is None


def test_enabled_lists_only_what_is_on(schedules: ScheduleStore, client: firestore.Client) -> None:
    seed(client)
    seed(client, id="sched-2", enabled=False)
    assert [s.id for s in schedules.enabled()] == ["sched-1"]
