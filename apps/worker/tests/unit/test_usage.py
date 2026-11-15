"""Firebase usage for the Worker page: Google's counts, in Firebase's days."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import pytest
from clipforge.config import Settings
from clipforge.usage import FREE, UsageMeter, firebase_day, measure

pytestmark = pytest.mark.unit

# 2026-11-15 20:00 UTC is 12:00 Pacific (PST, UTC-8) on the 15th.
NOW = datetime(2026, 11, 15, 20, 0, tzinfo=UTC)


def point(at: str, value: int | float) -> dict[str, Any]:
    key = "int64Value" if isinstance(value, int) else "doubleValue"
    return {
        "interval": {"startTime": at},
        "value": {key: str(value) if key == "int64Value" else value},
    }


def fake_monitoring(params_seen: list[dict[str, str]]) -> Any:
    def get(_url: str, params: dict[str, str]) -> dict[str, Any]:
        params_seen.append(params)
        metric = params["filter"]
        if "read_count" in metric:
            # 07:00 UTC on the 15th is still the 14th in Pacific time.
            return {
                "timeSeries": [
                    {
                        "points": [
                            point("2026-11-15T07:00:00Z", 10),
                            point("2026-11-15T09:00:00Z", 5),
                        ]
                    }
                ]
            }
        if "write_count" in metric:
            return {"timeSeries": [{"points": [point("2026-11-15T18:00:00Z", 3)]}]}
        if "total_bytes" in metric:
            return {
                "timeSeries": [
                    {
                        "points": [
                            point("2026-11-13T00:00:00Z", 900.0),
                            point("2026-11-14T00:00:00Z", 1200.0),
                        ]
                    }
                ]
            }
        return {}

    return get


def test_hours_are_counted_in_firebases_day() -> None:
    assert firebase_day(datetime(2026, 11, 15, 7, 0, tzinfo=UTC)) == "2026-11-14"
    assert firebase_day(datetime(2026, 11, 15, 9, 0, tzinfo=UTC)) == "2026-11-15"


def test_a_measure_sums_by_day_and_takes_the_newest_stored_size() -> None:
    seen: list[dict[str, str]] = []
    document = measure(Settings(firebase_project_id="p"), get=fake_monitoring(seen), now=NOW)

    days = {row["day"]: row for row in document["days"]}
    assert days["2026-11-15"]["reads"] == 5
    assert days["2026-11-14"]["reads"] == 10
    assert days["2026-11-15"]["writes"] == 3
    assert document["storedBytes"] == 1200
    assert document["free"] == FREE
    assert len(document["days"]) == 7 and document["days"][0]["day"] == "2026-11-15"


class FakeDb:
    def __init__(self) -> None:
        self.written: list[dict[str, Any]] = []

    def collection(self, _name: str) -> Any:
        db = self

        class Doc:
            def set(self, data: dict[str, Any], merge: bool = False) -> None:
                db.written.append(data)

        class Col:
            def document(self, _id: str) -> Doc:
                return Doc()

        return Col()


def wait_for(db: FakeDb, count: int) -> None:
    deadline = time.monotonic() + 5
    while len(db.written) < count and time.monotonic() < deadline:
        time.sleep(0.01)


def test_the_meter_measures_on_start_then_at_most_hourly() -> None:
    db, calls = FakeDb(), list[int]()

    def count() -> dict[str, Any]:
        calls.append(1)
        return {"measuredAt": NOW}

    meter = UsageMeter(Settings(), db, measure_now=count)

    assert meter.maybe_refresh(force=True)
    wait_for(db, 1)
    assert not meter.maybe_refresh()
    assert len(calls) == 1


def test_a_failed_measure_is_written_as_its_reason() -> None:
    db = FakeDb()

    def refuse() -> dict[str, Any]:
        raise RuntimeError("Give the Firebase service account the Monitoring Viewer role.")

    UsageMeter(Settings(), db, measure_now=refuse).maybe_refresh(force=True)
    wait_for(db, 1)

    assert "Monitoring Viewer" in db.written[0]["error"]
