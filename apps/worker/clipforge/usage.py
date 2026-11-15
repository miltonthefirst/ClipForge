"""What ClipForge uses of Firebase, from Google's own counts (Cloud Monitoring).

Copied in idea from Sarungano's app/cloud/usage.py, at the owner's request
(2026-11-15). ClipForge is on Blaze, so what crosses the no-cost allowances is
money: Firestore reads, writes and deletes per day, and Cloud Storage's bytes
stored and bytes downloaded. These are the project's numbers, every phone's
reads included, which is what the bill is measured against; Sarungano also
counts each of its own processes' operations, which this does not.

Days are Firebase's: the Firestore allowance resets at midnight Pacific time.

The agent measures (clipforge/agent.py): when it starts, and when someone opens
the app (a ping) and the last measure is over an hour old. No timer: nobody
looking means nothing to measure for (docs/adr/0030-status-on-events-and-pings.md).
The result is one document, ``usage/current``, which the Worker page shows.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from clipforge.config import Settings
from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = ["FREE", "UsageMeter", "firebase_day", "measure"]

# Firestore's no-cost allowance per day, the same on Spark and Blaze.
FREE = {"reads": 50_000, "writes": 20_000, "deletes": 20_000}
DAYS = 7
REFRESH_AFTER_SECONDS = 3600.0
MONITORING = "https://monitoring.googleapis.com/v3/projects/{project}/timeSeries"
FIRESTORE = {kind: f"firestore.googleapis.com/document/{kind[:-1]}_count" for kind in FREE}
STORED = "storage.googleapis.com/storage/v2/total_bytes"
DOWNLOADED = "storage.googleapis.com/network/sent_bytes_count"
_PACIFIC = ZoneInfo("America/Los_Angeles")


def firebase_day(at: datetime) -> str:
    """The Pacific-time date `at` falls on: the day Firebase counts in."""
    return at.astimezone(_PACIFIC).date().isoformat()


def measure(
    settings: Settings,
    *,
    get: Callable[[str, dict[str, str]], dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The project's last seven Firebase days, as the `usage/current` document.

    `get(url, params) -> json` is the test seam; by default an authorised
    session with the service account's credentials.
    """
    now = now or datetime.now(UTC)
    fetch = get or _session_get(settings)
    end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    start = end - timedelta(days=DAYS + 1)
    days = [firebase_day(now - timedelta(days=n)) for n in range(DAYS)]
    table: dict[str, dict[str, float]] = {
        day: {"reads": 0, "writes": 0, "deletes": 0, "downloadedBytes": 0} for day in days
    }

    def series(metric: str, aligner: str, period: str) -> list[tuple[datetime, float]]:
        answer = fetch(
            MONITORING.format(project=settings.firebase_project_id),
            {
                "filter": f'metric.type="{metric}"',
                "interval.startTime": start.isoformat().replace("+00:00", "Z"),
                "interval.endTime": end.isoformat().replace("+00:00", "Z"),
                "aggregation.alignmentPeriod": period,
                "aggregation.perSeriesAligner": aligner,
                "aggregation.crossSeriesReducer": "REDUCE_SUM",
            },
        )
        points: list[tuple[datetime, float]] = []
        for one in answer.get("timeSeries") or []:
            for point in one.get("points") or []:
                at = datetime.fromisoformat(point["interval"]["startTime"].replace("Z", "+00:00"))
                value = point["value"]
                number = float(value.get("int64Value", value.get("doubleValue", 0)))
                points.append((at, number))
        return points

    for kind, metric in FIRESTORE.items():
        for at, value in series(metric, "ALIGN_SUM", "3600s"):
            day = firebase_day(at)
            if day in table:
                table[day][kind] += value
    for at, value in series(DOWNLOADED, "ALIGN_SUM", "3600s"):
        day = firebase_day(at)
        if day in table:
            table[day]["downloadedBytes"] += value
    # Sampled about once a day, and a while behind: the newest sample is the
    # best answer to "how much is stored".
    stored = sorted(series(STORED, "ALIGN_MEAN", "86400s"))
    return {
        "measuredAt": now,
        "error": None,
        "free": dict(FREE),
        "storedBytes": int(stored[-1][1]) if stored else 0,
        "storedAsOf": stored[-1][0] if stored else None,
        "days": [{"day": day, **{k: int(v) for k, v in table[day].items()}} for day in days],
    }


def _session_get(settings: Settings) -> Callable[[str, dict[str, str]], dict[str, Any]]:
    from google.auth.transport.requests import AuthorizedSession

    scopes = ["https://www.googleapis.com/auth/monitoring.read"]
    if settings.google_application_credentials:
        from google.oauth2 import service_account

        credentials: Any = service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
            settings.google_application_credentials, scopes=scopes
        )
    else:
        import google.auth

        credentials, _ = google.auth.default(scopes=scopes)
    session = AuthorizedSession(credentials)  # type: ignore[no-untyped-call]

    def get(url: str, params: dict[str, str]) -> dict[str, Any]:
        response = session.get(url, params=params, timeout=30)
        body: dict[str, Any] = response.json() if response.content else {}
        if response.status_code != 200:
            said = str((body.get("error") or {}).get("message") or "")
            if response.status_code == 403 and ("disabled" in said or "has not been used" in said):
                advice = (
                    "Turn on the Cloud Monitoring API for the project in the Google Cloud console."
                )
            elif response.status_code == 403:
                advice = "Give the Firebase service account the Monitoring Viewer role."
            else:
                advice = f"Google answered {response.status_code} when asked for the usage."
            raise RuntimeError(f"{advice} Google said: {said}" if said else advice)
        return body

    return get


class UsageMeter:
    """The agent's half: measure on start, and on a ping when an hour old."""

    def __init__(
        self,
        settings: Settings,
        client: Any,
        *,
        measure_now: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._db = client
        self._measure = measure_now or (lambda: measure(settings))
        self._measured = 0.0
        self._lock = threading.Lock()
        self._running = False

    def maybe_refresh(self, *, force: bool = False) -> bool:
        """Start a measure in the background if one is due. True if started."""
        with self._lock:
            due = force or time.monotonic() - self._measured > REFRESH_AFTER_SECONDS
            if self._running or not due:
                return False
            self._running = True
            self._measured = time.monotonic()
        threading.Thread(target=self._run, name="usage-meter", daemon=True).start()
        return True

    def _run(self) -> None:
        try:
            try:
                document = self._measure()
            except Exception as exc:  # noqa: BLE001 - said on the page, measured again later
                document = {
                    "measuredAt": datetime.now(UTC),
                    "error": str(exc) or type(exc).__name__,
                }
                log.warning("usage.measure_failed", error=document["error"])
            self._db.collection("usage").document("current").set(document, merge=True)
        except Exception as exc:  # noqa: BLE001 - the agent carries on
            log.warning("usage.write_failed", error=str(exc))
        finally:
            with self._lock:
                self._running = False
