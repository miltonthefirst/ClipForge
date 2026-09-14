"""Joining publications to their realised performance, and back to their scores.

This is the only module in the package that touches both the network and the
store, and it is deliberately the only one. Everything interesting — the
bucketing, the correlations, the interval arithmetic — lives in pure modules
that this one feeds.

**What "without gaps" means.** Exit criterion 1 asks that a published clip
accrue daily snapshots without gaps, and a gap has two quite different causes: a
poll that did not run, and a day YouTube reported nothing for. The second is not
a gap — a video with no views on a Tuesday has a Tuesday with zeroes, not a
missing Tuesday — so the poller writes a zero-filled snapshot for every day in
the requested window that the API did not mention. Without that, a quiet clip
would look like a broken poller, and the check that is supposed to detect a
broken poller would be useless.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from clipforge_contracts import (
    Candidate,
    Clip,
    MetricSnapshot,
    Publication,
    RetentionPoint,
)

from clipforge.analytics.cohorts import ClipFacts, retention_at
from clipforge.analytics.youtube import AnalyticsClient, DailyRow, is_partial
from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = [
    "PollOutcome",
    "build_facts",
    "poll_publication",
    "snapshots_for",
]

#: How far back a routine poll looks. Long enough to repair a week of downtime,
#: short enough that a daily run is a handful of queries rather than a backfill.
DEFAULT_WINDOW_DAYS = 14


@dataclass(frozen=True)
class PollOutcome:
    """What one publication's poll produced."""

    publication_id: str
    external_id: str
    fetched_days: int
    written: int
    retention_points: int
    error: str | None = None


def snapshots_for(
    publication: Publication,
    rows: Sequence[DailyRow],
    retention: Sequence[tuple[float, float]],
    *,
    start: date,
    end: date,
    today: date | None = None,
) -> list[MetricSnapshot]:
    """Turn one publication's API rows into snapshots, one per day in range.

    Days the API did not mention are filled with zeroes and marked partial, so a
    later poll can replace them with real numbers. A genuinely quiet day is
    re-filled identically on every poll, which costs a write and keeps the
    history correctable; that trade is the right way round.

    The retention curve is attached to **every** day rather than only the last.
    It is a lifetime-to-date measure, not a daily one, so a snapshot carrying the
    curve as it stood on that date is the honest record — and it means a clip
    whose curve was withheld in week one and released in week three shows exactly
    when that happened, instead of retroactively appearing to have always had it.
    """
    reference = today or datetime.now(UTC).date()
    by_day = {row.day: row for row in rows}
    curve = [
        RetentionPoint(elapsed_ratio=elapsed, audience_watch_ratio=watched)
        for elapsed, watched in retention
    ]
    published_day = publication.published_at.date() if publication.published_at else start

    snapshots: list[MetricSnapshot] = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        if day > reference:
            break
        row = by_day.get(day)
        snapshots.append(
            MetricSnapshot(
                id=f"{publication.id}_{day.isoformat()}",
                uid=publication.uid,
                clip_id=publication.clip_id,
                publication_id=publication.id,
                platform=publication.platform,
                # Checked by the caller before we get here; the contract requires
                # it and a publication without one is not pollable.
                external_id=publication.external_id or "",
                channel_id=publication.channel_id,
                date=day,
                days_since_publish=max(0, (day - published_day).days),
                views=row.views if row else 0,
                likes=row.likes if row else 0,
                comments=row.comments if row else 0,
                shares=row.shares if row else 0,
                subscribers_gained=row.subscribers_gained if row else 0,
                estimated_minutes_watched=row.estimated_minutes_watched if row else 0.0,
                average_view_duration_sec=row.average_view_duration_sec if row else None,
                average_view_percentage=row.average_view_percentage if row else None,
                retention=curve,
                # Partial if the platform may still restate it, *or* if the
                # platform never mentioned it. The second half matters more than
                # it looks: a zero-filled day is an inference — "the API said
                # nothing, so presumably nothing happened" — and marking an
                # inference settled makes it permanent, because save_all never
                # rewrites a settled day. One dropped row, one response that came
                # back empty for a reason other than silence, and that clip
                # carries a fabricated zero for ever, with missing_days reporting
                # no gap because a fabricated zero is not a gap.
                partial=is_partial(day, today=reference) or row is None,
                fetched_at=datetime.now(UTC),
            )
        )
    return snapshots


def poll_publication(
    client: AnalyticsClient,
    publication: Publication,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    today: date | None = None,
) -> tuple[list[MetricSnapshot], PollOutcome]:
    """Fetch and shape one publication's recent metrics.

    Never raises for one publication's sake. A poll covering thirty clips must
    not abandon twenty-nine of them because the thirtieth was deleted from the
    channel, so the failure is recorded on the outcome and the caller carries on.
    """
    reference = today or datetime.now(UTC).date()
    external_id = publication.external_id or ""
    if not external_id:
        return [], PollOutcome(
            publication_id=publication.id,
            external_id="",
            fetched_days=0,
            written=0,
            retention_points=0,
            error="no externalId on the publication",
        )

    published_day = (
        publication.published_at.date()
        if publication.published_at
        else reference - timedelta(days=window_days)
    )
    # Never ask for days before the video existed: the API accepts the range and
    # returns nothing, and the zero-fill would then invent a history of silence
    # for a clip that had not been published yet.
    start = max(published_day, reference - timedelta(days=window_days))
    if start > reference:
        start = reference

    try:
        rows = client.daily(external_id, start=start, end=reference)
        curve = [
            (point.elapsed_ratio, point.audience_watch_ratio)
            for point in client.retention(external_id, start=published_day, end=reference)
        ]
    except Exception as exc:  # noqa: BLE001 - one clip's failure is not the run's
        log.warning(
            "analytics.poll_failed",
            publication_id=publication.id,
            external_id=external_id,
            error=str(exc),
        )
        return [], PollOutcome(
            publication_id=publication.id,
            external_id=external_id,
            fetched_days=0,
            written=0,
            retention_points=0,
            error=str(exc),
        )

    snapshots = snapshots_for(publication, rows, curve, start=start, end=reference, today=reference)
    return snapshots, PollOutcome(
        publication_id=publication.id,
        external_id=external_id,
        fetched_days=len(rows),
        written=len(snapshots),
        retention_points=len(curve),
    )


def build_facts(
    *,
    publication: Publication,
    clip: Clip | None,
    candidate: Candidate | None,
    snapshots: Sequence[MetricSnapshot],
    caption_style: str | None = None,
) -> ClipFacts:
    """Flatten one published clip and its history into a single row.

    Views are taken as the **sum** across snapshots, because the API reports
    views per day and the question is lifetime. Average view percentage is taken
    from the **latest snapshot that has one**, because it is already an average
    over the video's life and averaging a sequence of running averages would
    weight the early, sparse days as heavily as the settled ones.
    """
    settled = sorted(snapshots, key=lambda s: s.date)
    # `or 0` throughout: the generator makes every defaulted field Optional,
    # so a contract that says `minimum: 0, default: 0` still types as int|None.
    views = sum(snapshot.views or 0 for snapshot in settled)

    percentage: float | None = None
    for snapshot in reversed(settled):
        if snapshot.average_view_percentage is not None:
            percentage = snapshot.average_view_percentage
            break

    curve: list[tuple[float, float]] = []
    for snapshot in reversed(settled):
        if snapshot.retention:
            curve = [
                (point.elapsed_ratio, point.audience_watch_ratio) for point in snapshot.retention
            ]
            break

    return ClipFacts(
        clip_id=publication.clip_id,
        publication_id=publication.id,
        predicted_score=candidate.total if candidate else None,
        sub_scores=candidate.sub_scores if candidate else None,
        hook=candidate.hook if candidate else None,
        duration_sec=clip.duration_sec if clip else None,
        # `str(getattr(tag, "root", tag))` for the same reason publish/metadata.py
        # does it: a maxLength on an array's items makes the generator emit a
        # RootModel, so a clip's own tags arrive as objects rather than strings.
        tags=tuple(str(getattr(tag, "root", tag)) for tag in (clip.tags or ())) if clip else (),
        published_at=publication.published_at,
        render_profile=clip.render_profile if clip else None,
        caption_style=caption_style,
        views=views,
        view_percentage=percentage,
        retention_at_half=retention_at(curve, 0.5),
    )
