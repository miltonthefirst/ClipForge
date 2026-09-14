"""Metric snapshots against a real Firestore.

The pure shaping is covered in tests/unit/test_analytics_poller.py. What can
only be proven here is what the store does with a document that already exists —
which is where the two properties Phase 9 depends on actually live:

- **Polling twice must not double a clip's views.** The composite id is the
  mechanism; that it holds against a real write is the thing to check.
- **A settled day is never rewritten.** If it were, a calibration run today and
  the same run tomorrow could disagree about last month, and the report would
  stop being evidence.

Phase 9, exit criterion 1.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from clipforge.config import Settings
from clipforge.store.firestore import CalibrationStore, MetricStore
from clipforge_contracts import (
    CalibrationReport,
    MetricSnapshot,
    PublishPlatform,
    ScoreWeights,
)
from google.cloud import firestore

pytestmark = pytest.mark.integration

UID = "user-1"
PUB = "pub-1"


@pytest.fixture
def metrics(client: firestore.Client, settings: Settings) -> MetricStore:
    return MetricStore(client, settings)


@pytest.fixture
def calibrations(client: firestore.Client, settings: Settings) -> CalibrationStore:
    return CalibrationStore(client, settings)


def _snapshot(day: date, *, views: int = 10, partial: bool = False) -> MetricSnapshot:
    return MetricSnapshot(
        id=f"{PUB}_{day.isoformat()}",
        uid=UID,
        clip_id="clip-1",
        publication_id=PUB,
        platform=PublishPlatform.YOUTUBE,
        external_id="vid123",
        date=day,
        views=views,
        partial=partial,
        fetched_at=datetime(2026, 9, 14, tzinfo=UTC),
    )


def test_a_snapshot_round_trips(metrics: MetricStore) -> None:
    day = date(2026, 9, 10)
    metrics.save_all([_snapshot(day, views=42)])
    found = metrics.get(PUB, day)
    assert found is not None
    assert found.views == 42
    assert found.date == day


def test_polling_the_same_day_twice_does_not_double_the_views(metrics: MetricStore) -> None:
    """The bug the composite id exists to prevent, asserted directly."""
    day = date(2026, 9, 13)
    metrics.save_all([_snapshot(day, views=100, partial=True)])
    metrics.save_all([_snapshot(day, views=140, partial=True)])

    everything = metrics.for_publication(PUB)
    assert len(everything) == 1
    assert everything[0].views == 140


def test_a_settled_day_is_never_rewritten(metrics: MetricStore) -> None:
    """Outside the revision window, the first answer is the final answer."""
    day = date(2026, 9, 1)
    metrics.save_all([_snapshot(day, views=100, partial=False)])
    written = metrics.save_all([_snapshot(day, views=999, partial=False)])

    assert written == 0
    found = metrics.get(PUB, day)
    assert found is not None
    assert found.views == 100


def test_a_partial_day_is_replaced_when_it_settles(metrics: MetricStore) -> None:
    """YouTube restates recent days, so a partial one must still be updatable."""
    day = date(2026, 9, 13)
    metrics.save_all([_snapshot(day, views=100, partial=True)])
    written = metrics.save_all([_snapshot(day, views=180, partial=False)])

    assert written == 1
    found = metrics.get(PUB, day)
    assert found is not None
    assert found.views == 180
    assert found.partial is False


def test_snapshots_come_back_oldest_first(metrics: MetricStore) -> None:
    days = [date(2026, 9, 5), date(2026, 9, 3), date(2026, 9, 4)]
    metrics.save_all([_snapshot(day) for day in days])
    assert [s.date for s in metrics.for_publication(PUB)] == sorted(days)


def test_a_gap_is_named_rather_than_counted(metrics: MetricStore) -> None:
    """Exit criterion 1 made checkable.

    Four of six days present. A count would say "four snapshots" and look
    healthy; the question worth answering is *which two are missing*.
    """
    present = [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 5), date(2026, 9, 6)]
    metrics.save_all([_snapshot(day) for day in present])

    missing = metrics.missing_days(PUB, start=date(2026, 9, 1), end=date(2026, 9, 6))
    assert missing == [date(2026, 9, 3), date(2026, 9, 4)]


def test_no_gaps_is_an_empty_list(metrics: MetricStore) -> None:
    days = [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]
    metrics.save_all([_snapshot(day) for day in days])
    assert metrics.missing_days(PUB, start=days[0], end=days[-1]) == []


def test_a_publication_with_nothing_polled_is_all_gap(metrics: MetricStore) -> None:
    missing = metrics.missing_days(PUB, start=date(2026, 9, 1), end=date(2026, 9, 3))
    assert len(missing) == 3


def test_another_account_s_clip_is_measured_too(metrics: MetricStore) -> None:
    """The workspace is shared, and the metrics have to agree.

    This was a real bug rather than a hypothetical: the first two clips this
    project published went out under two different accounts, and the store
    originally filtered by uid — so a poll would have measured one video and
    silently skipped the other, and the dashboard would have shown half a
    channel while looking perfectly complete.
    """
    day = date(2026, 9, 10)
    metrics.save_all([_snapshot(day)])
    metrics.save_all(
        [
            _snapshot(day).model_copy(
                update={"id": f"other_{day}", "uid": "user-2", "publication_id": "other"}
            )
        ]
    )
    assert sorted(s.uid for s in metrics.all()) == [UID, "user-2"]


def test_a_calibration_report_round_trips(calibrations: CalibrationStore) -> None:
    weights = ScoreWeights(
        hook=0.25, curiosity=0.20, standalone=0.20, emotion=0.15, pacing=0.10, shareability=0.10
    )
    report = CalibrationReport(
        id="20260914T120000Z",
        uid=UID,
        generated_at=datetime(2026, 9, 14, 12, tzinfo=UTC),
        n=3,
        # Both are required by the contract even though they default to empty:
        # a report that omitted them would be a report that never ran the
        # analysis, which is different from one that ran it and found nothing.
        correlations=[],
        cohorts=[],
        baseline_weights=weights,
        underpowered=True,
        notes=["n=3 supports nothing."],
    )
    calibrations.save(report)

    found = calibrations.get(report.id)
    assert found is not None
    assert found.underpowered is True
    assert found.n == 3
    assert calibrations.latest() is not None
