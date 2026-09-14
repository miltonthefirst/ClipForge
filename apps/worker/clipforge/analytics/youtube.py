"""YouTube Analytics API v2: daily metrics and the retention curve.

Separate from :mod:`clipforge.publish.youtube` because it is a separate API with
a separate scope, and joined to it by exactly two things — the access token, and
the quota ledger.

**The scope is new, and that means re-authorising.** Publishing asked for
``youtube.upload`` and ``youtube.readonly``; neither grants analytics. A token
minted before this phase existed will be refused, so the failure is checked for
by name and answered with the command to run rather than with a 403 body.

**Quota is shared with publishing, deliberately.** A report query costs a
handful of units against an upload's 1,600, so the poller is not what will
exhaust an allowance — but a poller that ignored the budget entirely could still
be the thing that takes the last units on the day a clip needed publishing, and
publishing is the side that cannot simply run again in an hour. Note that Google
meters the Analytics API separately from the Data API in the console; sharing one
ledger here is conservatism rather than a claim about their billing, and if the
two are wholly independent the only cost of this choice is polling slightly less
aggressively than strictly necessary.

**Two to three days of any report are provisional.** YouTube restates recent
days as spam filtering and cross-device attribution settle. Snapshots inside
that window are marked ``partial`` and are rewritten by later polls; snapshots
outside it are written once and never touched again, which is what makes the
calibration read a stable history rather than a shifting one.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from clipforge.observability import get_logger
from clipforge.publish.youtube import (
    ANALYTICS_SCOPE,
    QuotaExceededError,
    QuotaLedger,
    YouTubeError,
)

log = get_logger(__name__)

__all__ = [
    "ANALYTICS_SCOPE",
    "REPORT_QUOTA_UNITS",
    "REVISION_WINDOW_DAYS",
    "AnalyticsClient",
    "AnalyticsScopeError",
    "DailyRow",
    "RetentionRow",
]

REPORTS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"

#: Charged against the shared ledger. See the module docstring on why this is
#: counted at all when it is this cheap.
REPORT_QUOTA_UNITS = 1

#: How many trailing days YouTube may still restate. Snapshots at or inside this
#: distance from today are written as ``partial``.
REVISION_WINDOW_DAYS = 3

#: The daily metrics, in the order the API returns them for this request.
DAILY_METRICS = (
    "views",
    "likes",
    "comments",
    "shares",
    "subscribersGained",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "averageViewPercentage",
)


class AnalyticsScopeError(YouTubeError):
    """The stored token predates analytics and cannot read reports."""

    def __init__(self, scopes: Sequence[str]) -> None:
        held = ", ".join(scopes) if scopes else "none recorded"
        super().__init__(
            "the stored YouTube token does not grant analytics access. It holds: "
            f"{held}. Re-authorise with `clipforge-worker youtube-auth` — the "
            "consent screen will now also ask for read-only analytics. Publishing "
            "keeps working in the meantime; only metrics polling is blocked.",
            # Re-authorising is a human action, so retrying the job achieves
            # nothing until somebody performs it.
            retryable=False,
            code="ANALYTICS_SCOPE_MISSING",
        )


@dataclass(frozen=True)
class DailyRow:
    """One day of metrics for one video, as the API reported it."""

    day: date
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    subscribers_gained: int = 0
    estimated_minutes_watched: float = 0.0
    average_view_duration_sec: float | None = None
    average_view_percentage: float | None = None


@dataclass(frozen=True)
class RetentionRow:
    """One point of the audience-retention curve."""

    elapsed_ratio: float
    audience_watch_ratio: float


def _cell(columns: dict[str, int], row: Sequence[Any], name: str) -> Any:
    """One named cell of a report row, or None when the column is absent.

    Named rather than positional. The request asks for metrics in an order, and
    decoding the response in that same order works right up until the API returns
    them in another one — at which point views are silently recorded as likes and
    nothing anywhere reports an error.
    """
    index = columns.get(name)
    if index is None or index >= len(row):
        return None
    return row[index]


def _as_int(value: Any) -> int:
    """Coerce a metric cell to an int, treating absent and null as zero.

    The API omits a metric entirely for a video with no activity rather than
    returning a zero, and returns floats for counters often enough that int()
    on a raw cell raises on perfectly ordinary responses.
    """
    if value is None:
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float | None:
    """Coerce a metric cell to a float, preserving the difference from zero.

    Unlike the counters, an absent ``averageViewPercentage`` is genuinely not
    the same as zero: YouTube withholds it below its privacy threshold, and a
    clip whose watch percentage is unknown must not be averaged in as though
    nobody watched any of it.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class AnalyticsClient:
    """Reads reports for the operator's own channel.

    Takes a callable for the access token rather than a ``YouTubeClient``, so
    the refresh machinery is reused without this class inheriting the ability to
    upload. Tests supply a transport and a token callable and need neither
    credentials nor network.
    """

    def __init__(
        self,
        *,
        access_token: Callable[[], str],
        scopes: Sequence[str] = (),
        ledger: QuotaLedger | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self._access_token = access_token
        self._scopes = tuple(scopes)
        self._ledger = ledger or QuotaLedger.today()
        self._transport = transport
        self._timeout_s = timeout_s

    @property
    def quota(self) -> QuotaLedger:
        return self._ledger

    def ensure_scope(self) -> None:
        """Refuse early if the token cannot possibly work.

        Checked against the recorded scopes rather than by making a request and
        reading the error, because the useful message here is about the operator's
        consent screen and the API's own 403 says nothing about which command to
        run.

        An empty scope list is *not* treated as failure: tokens written before
        scopes were recorded have none, and refusing those would break a working
        setup to guard against a hypothetical one.
        """
        if self._scopes and ANALYTICS_SCOPE not in self._scopes:
            raise AnalyticsScopeError(self._scopes)

    def _query(self, params: dict[str, str]) -> dict[str, Any]:
        """One report query, charged to the ledger before it is made."""
        self.ensure_scope()
        if not self._ledger.can_afford(REPORT_QUOTA_UNITS):
            raise QuotaExceededError(self._ledger.used_units, REPORT_QUOTA_UNITS)

        headers = {"Authorization": f"Bearer {self._access_token()}"}
        with httpx.Client(timeout=self._timeout_s, transport=self._transport) as client:
            response = client.get(REPORTS_URL, params=params, headers=headers)

        # Spent whatever the outcome: a rejected request has already cost the
        # allowance, and a ledger that only counts successes drifts optimistic
        # in exactly the circumstances where being accurate matters.
        self._ledger.spend(REPORT_QUOTA_UNITS)

        if response.status_code == 403:
            # Google returns 403 for quota and rate limiting as well as for a
            # missing scope, and the three want opposite responses: re-authorise
            # (a human, now) versus wait (nobody, later). `ensure_scope` has
            # already passed by this point, so a scope problem here is the less
            # likely reading — telling the operator to re-authorise over a quota
            # blip would send them to a consent screen that fixes nothing.
            reason = response.text.lower()
            if "quota" in reason or "ratelimit" in reason or "rate limit" in reason:
                raise QuotaExceededError(self._ledger.used_units, REPORT_QUOTA_UNITS)
            raise AnalyticsScopeError(self._scopes)
        if response.status_code == 429:
            raise QuotaExceededError(self._ledger.used_units, REPORT_QUOTA_UNITS)
        if response.status_code >= 400:
            raise YouTubeError(
                f"analytics query failed with {response.status_code}: {response.text[:400]}",
                retryable=response.status_code >= 500,
                code="ANALYTICS_ERROR",
            )
        payload: dict[str, Any] = response.json()
        return payload

    @staticmethod
    def _columns(payload: dict[str, Any]) -> dict[str, int]:
        """Map column name to index.

        Read from ``columnHeaders`` rather than assumed from the request, because
        positional decoding of someone else's response is how a request that
        silently reorders turns into metrics attributed to the wrong field.
        """
        headers = payload.get("columnHeaders") or []
        return {str(h.get("name")): i for i, h in enumerate(headers) if h.get("name")}

    def daily(self, video_id: str, *, start: date, end: date) -> list[DailyRow]:
        """Daily metrics for one video, oldest first."""
        payload = self._query(
            {
                "ids": "channel==MINE",
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "metrics": ",".join(DAILY_METRICS),
                "dimensions": "day",
                "filters": f"video=={video_id}",
                "sort": "day",
            }
        )
        columns = self._columns(payload)
        rows: list[DailyRow] = []
        for raw in payload.get("rows") or []:
            day_cell = _cell(columns, raw, "day")
            if not day_cell:
                continue
            try:
                day = date.fromisoformat(str(day_cell))
            except ValueError:
                log.warning("analytics.unparseable_day", day=str(day_cell))
                continue
            rows.append(
                DailyRow(
                    day=day,
                    views=_as_int(_cell(columns, raw, "views")),
                    likes=_as_int(_cell(columns, raw, "likes")),
                    comments=_as_int(_cell(columns, raw, "comments")),
                    shares=_as_int(_cell(columns, raw, "shares")),
                    subscribers_gained=_as_int(_cell(columns, raw, "subscribersGained")),
                    estimated_minutes_watched=(
                        _as_float(_cell(columns, raw, "estimatedMinutesWatched")) or 0.0
                    ),
                    average_view_duration_sec=_as_float(_cell(columns, raw, "averageViewDuration")),
                    average_view_percentage=_as_float(_cell(columns, raw, "averageViewPercentage")),
                )
            )
        rows.sort(key=lambda r: r.day)
        return rows

    def retention(self, video_id: str, *, start: date, end: date) -> list[RetentionRow]:
        """The audience-retention curve, or an empty list if withheld.

        An empty curve is the normal state for a video below YouTube's privacy
        threshold, so it is returned as emptiness rather than raised. Callers
        exclude such clips from curve-based aggregation; treating a withheld
        curve as a flat zero would report every small clip as nobody watching.
        """
        payload = self._query(
            {
                "ids": "channel==MINE",
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "metrics": "audienceWatchRatio",
                "dimensions": "elapsedVideoTimeRatio",
                "filters": f"video=={video_id}",
                "sort": "elapsedVideoTimeRatio",
            }
        )
        columns = self._columns(payload)
        elapsed_index = columns.get("elapsedVideoTimeRatio")
        ratio_index = columns.get("audienceWatchRatio")
        if elapsed_index is None or ratio_index is None:
            return []

        points: list[RetentionRow] = []
        for raw in payload.get("rows") or []:
            if max(elapsed_index, ratio_index) >= len(raw):
                continue
            elapsed = _as_float(raw[elapsed_index])
            watched = _as_float(raw[ratio_index])
            if elapsed is None or watched is None:
                continue
            # Clamped on elapsed only. audienceWatchRatio above 1 is real —
            # it means people scrubbed back — and flattening it would erase
            # the single most interesting thing a curve can say.
            points.append(
                RetentionRow(
                    elapsed_ratio=min(1.0, max(0.0, elapsed)),
                    audience_watch_ratio=max(0.0, watched),
                )
            )
        points.sort(key=lambda p: p.elapsed_ratio)
        return points


def is_partial(day: date, *, today: date | None = None) -> bool:
    """Whether ``day`` is recent enough that YouTube may still restate it."""
    reference = today or datetime.now(UTC).date()
    return (reference - day) <= timedelta(days=REVISION_WINDOW_DAYS)
