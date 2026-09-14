"""Phase 9 — bucketing, gap-filling, and reading someone else's report format.

The API half is tested against a stub transport rather than the network, which
means these run in CI with no account and no published video. What they cannot
prove is that YouTube's response looks like the one stubbed here; what they can
prove is everything that happens to it afterwards, which is where the bugs that
silently produce wrong numbers live.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from clipforge.analytics.cohorts import ClipFacts, bucket_for, hook_type, retention_at, summarise
from clipforge.analytics.poller import poll_publication, snapshots_for
from clipforge.analytics.youtube import (
    AnalyticsClient,
    AnalyticsScopeError,
    DailyRow,
    is_partial,
)
from clipforge.publish.youtube import ANALYTICS_SCOPE, QuotaExceededError, QuotaLedger
from clipforge_contracts import (
    CohortKind,
    Publication,
    PublicationState,
    PublishPlatform,
)

TODAY = date(2026, 9, 14)


def _publication(published: datetime | None = None) -> Publication:
    return Publication(
        id="pub1",
        clip_id="clip1",
        uid="user1",
        platform=PublishPlatform.YOUTUBE,
        state=PublicationState.PUBLISHED,
        external_id="vid123",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        published_at=published or datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
    )


def _transport(payload: Mapping[str, Any], status: int = 200) -> httpx.MockTransport:
    return httpx.MockTransport(lambda _request: httpx.Response(status, json=payload))


# ── Hook classification ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("hook", "expected"),
    [
        ("Why is this the best goal ever?", "QUESTION"),
        ("How did he miss that", "QUESTION"),
        ('"I have never seen anything like it"', "QUOTE"),
        ("The best finish of the season", "SUPERLATIVE"),
        ("3 goals in 4 minutes", "NUMBER"),
        ("He does not even look up", "NEGATION"),
        ("Pavlovitch breaks the first line", "STATEMENT"),
        ("", "NONE"),
        (None, "NONE"),
    ],
)
def test_hooks_are_classified_by_shape(hook: str | None, expected: str) -> None:
    assert hook_type(hook) == expected


def test_a_question_beats_a_superlative_when_a_hook_is_both() -> None:
    """Buckets must not overlap or the counts stop summing."""
    assert hook_type("Why is this the best goal ever?") == "QUESTION"


# ── Buckets ──────────────────────────────────────────────────────────────────


def test_an_unknowable_bucket_is_absent_rather_than_named_unknown() -> None:
    """A clip with no duration is not a cohort of clips-with-no-duration."""
    facts = ClipFacts(clip_id="c", publication_id="p", duration_sec=None)
    assert bucket_for(CohortKind.DURATION_BUCKET, facts) is None


def test_posting_time_is_banded_and_labelled_utc() -> None:
    facts = ClipFacts(
        clip_id="c",
        publication_id="p",
        published_at=datetime(2026, 9, 10, 14, 30, tzinfo=UTC),
    )
    assert bucket_for(CohortKind.POSTING_HOUR, facts) == "12-16 UTC"


def test_a_mean_is_withheld_for_a_bucket_too_small_to_mean_anything() -> None:
    """The bucket still appears with its count; only the mean is withheld."""
    facts = [
        ClipFacts(clip_id="c1", publication_id="p1", duration_sec=20.0, views=100),
        ClipFacts(clip_id="c2", publication_id="p2", duration_sec=21.0, views=900),
    ]
    stats = [s for s in summarise(facts) if s.kind is CohortKind.DURATION_BUCKET]
    assert len(stats) == 1
    assert stats[0].n == 2
    assert stats[0].mean_views is None


def test_a_bucket_at_the_threshold_reports_its_mean() -> None:
    facts = [
        ClipFacts(clip_id=f"c{i}", publication_id=f"p{i}", duration_sec=20.0, views=100)
        for i in range(3)
    ]
    stats = [s for s in summarise(facts) if s.kind is CohortKind.DURATION_BUCKET]
    assert stats[0].mean_views == pytest.approx(100.0)


# ── Retention ────────────────────────────────────────────────────────────────


def test_retention_is_interpolated_rather_than_nearest_sampled() -> None:
    assert retention_at([(0.0, 1.0), (1.0, 0.0)], 0.5) == pytest.approx(0.5)


def test_retention_beyond_the_curve_clamps_to_its_ends() -> None:
    curve = [(0.2, 0.8), (0.8, 0.4)]
    assert retention_at(curve, 0.0) == pytest.approx(0.8)
    assert retention_at(curve, 1.0) == pytest.approx(0.4)


def test_no_curve_is_none_rather_than_zero() -> None:
    assert retention_at([], 0.5) is None


# ── The API client ───────────────────────────────────────────────────────────


def test_a_token_without_the_analytics_scope_is_refused_by_name() -> None:
    """And the message names the command, not the HTTP status."""
    client = AnalyticsClient(
        access_token=lambda: "t",
        scopes=("https://www.googleapis.com/auth/youtube.upload",),
    )
    with pytest.raises(AnalyticsScopeError) as caught:
        client.ensure_scope()
    assert "youtube-auth" in str(caught.value)
    assert caught.value.retryable is False


def test_a_token_with_no_recorded_scopes_is_allowed_through() -> None:
    """Tokens predate the scope list. Refusing them would break a working setup."""
    AnalyticsClient(access_token=lambda: "t", scopes=()).ensure_scope()


def test_columns_are_read_by_name_not_by_position() -> None:
    """The metrics are requested in an order; the response need not honour it.

    Here the API returns views and likes swapped relative to the request. Decoded
    positionally, this clip has 7 views and 900 likes.
    """
    payload = {
        "columnHeaders": [
            {"name": "day"},
            {"name": "likes"},
            {"name": "views"},
        ],
        "rows": [["2026-09-12", 7, 900]],
    }
    client = AnalyticsClient(
        access_token=lambda: "t", scopes=(ANALYTICS_SCOPE,), transport=_transport(payload)
    )
    rows = client.daily("vid123", start=date(2026, 9, 12), end=TODAY)
    assert rows[0].views == 900
    assert rows[0].likes == 7


def test_a_withheld_retention_curve_comes_back_empty_rather_than_raising() -> None:
    payload: dict[str, Any] = {"columnHeaders": [], "rows": []}
    client = AnalyticsClient(
        access_token=lambda: "t", scopes=(ANALYTICS_SCOPE,), transport=_transport(payload)
    )
    assert client.retention("vid123", start=date(2026, 9, 1), end=TODAY) == []


def test_a_rewatched_segment_is_not_clamped_to_one() -> None:
    """audienceWatchRatio above 1 means people scrubbed back. That is the signal."""
    payload = {
        "columnHeaders": [{"name": "elapsedVideoTimeRatio"}, {"name": "audienceWatchRatio"}],
        "rows": [[0.0, 1.0], [0.5, 1.8], [1.0, 0.3]],
    }
    client = AnalyticsClient(
        access_token=lambda: "t", scopes=(ANALYTICS_SCOPE,), transport=_transport(payload)
    )
    points = client.retention("vid123", start=date(2026, 9, 1), end=TODAY)
    assert points[1].audience_watch_ratio == pytest.approx(1.8)


def test_a_query_spends_quota_even_when_it_fails() -> None:
    """A rejected request has already cost the allowance."""
    ledger = QuotaLedger.today()
    client = AnalyticsClient(
        access_token=lambda: "t",
        scopes=(ANALYTICS_SCOPE,),
        ledger=ledger,
        transport=_transport({"error": "boom"}, status=500),
    )
    with pytest.raises(Exception, match="analytics query failed"):
        client.daily("vid123", start=date(2026, 9, 12), end=TODAY)
    assert ledger.used_units > 0


def test_the_poller_shares_the_publishing_ledger() -> None:
    """A poll must not be able to spend the units an upload needed."""
    ledger = QuotaLedger.today()
    ledger.spend(9_999)
    client = AnalyticsClient(
        access_token=lambda: "t",
        scopes=(ANALYTICS_SCOPE,),
        ledger=ledger,
        transport=_transport({"columnHeaders": [], "rows": []}),
    )
    # 9,999 of 10,000 spent leaves room for exactly one more unit, then none.
    client.daily("vid123", start=TODAY, end=TODAY)
    with pytest.raises(Exception, match="quota"):
        client.daily("vid123", start=TODAY, end=TODAY)


# ── Gap filling ──────────────────────────────────────────────────────────────


def test_a_quiet_day_becomes_a_zero_not_a_gap() -> None:
    """The distinction exit criterion 1 depends on.

    YouTube reports nothing for a day with no views. If that became a missing
    snapshot, a quiet clip would be indistinguishable from a poller that never
    ran — and the gap check could not tell anyone anything.
    """
    publication = _publication()
    rows = [DailyRow(day=date(2026, 9, 12), views=40)]
    snapshots = snapshots_for(
        publication,
        rows,
        [],
        start=date(2026, 9, 10),
        end=date(2026, 9, 13),
        today=TODAY,
    )
    assert [s.date for s in snapshots] == [
        date(2026, 9, 10),
        date(2026, 9, 11),
        date(2026, 9, 12),
        date(2026, 9, 13),
    ]
    assert [s.views for s in snapshots] == [0, 0, 40, 0]


def test_days_inside_the_revision_window_are_marked_partial() -> None:
    """YouTube restates recent days, so recent days stay rewritable.

    Every day is *reported* here, so the only thing deciding `partial` is the
    distance from today — which is what this is about. The other reason a day
    can be partial, that the API never mentioned it, has its own test.
    """
    publication = _publication()
    span = [date(2026, 9, 8) + timedelta(days=offset) for offset in range(7)]
    rows = [DailyRow(day=day, views=5) for day in span]
    snapshots = snapshots_for(publication, rows, [], start=span[0], end=TODAY, today=TODAY)
    settled = {s.date: s.partial for s in snapshots}
    assert settled[date(2026, 9, 8)] is False
    assert settled[date(2026, 9, 10)] is False
    assert settled[date(2026, 9, 13)] is True
    assert settled[TODAY] is True


def test_the_future_is_never_written() -> None:
    publication = _publication()
    snapshots = snapshots_for(
        publication,
        [],
        [],
        start=date(2026, 9, 13),
        end=date(2026, 9, 20),
        today=TODAY,
    )
    assert max(s.date for s in snapshots) == TODAY


def test_days_before_publication_are_not_invented() -> None:
    """A window wider than the clip's life must not manufacture silent days."""
    publication = _publication(published=datetime(2026, 9, 12, tzinfo=UTC))
    client = AnalyticsClient(
        access_token=lambda: "t",
        scopes=(ANALYTICS_SCOPE,),
        transport=_transport({"columnHeaders": [], "rows": []}),
    )
    snapshots, outcome = poll_publication(client, publication, window_days=30, today=TODAY)
    assert outcome.error is None
    assert min(s.date for s in snapshots) == date(2026, 9, 12)


def test_one_publication_failing_does_not_raise() -> None:
    """A poll over thirty clips must not be abandoned because one was deleted."""
    publication = _publication()
    client = AnalyticsClient(
        access_token=lambda: "t",
        scopes=(ANALYTICS_SCOPE,),
        transport=_transport({"error": "gone"}, status=404),
    )
    snapshots, outcome = poll_publication(client, publication, today=TODAY)
    assert snapshots == []
    assert outcome.error is not None


def test_a_publication_with_no_external_id_is_reported_not_polled() -> None:
    publication = _publication().model_copy(update={"external_id": None})
    client = AnalyticsClient(access_token=lambda: "t", scopes=(ANALYTICS_SCOPE,))
    snapshots, outcome = poll_publication(client, publication, today=TODAY)
    assert snapshots == []
    assert "externalId" in (outcome.error or "")


def test_partial_is_decided_by_distance_not_by_fetch_time() -> None:
    assert is_partial(date(2026, 9, 13), today=TODAY) is True
    assert is_partial(date(2026, 9, 10), today=TODAY) is False


# ── What the review caught ───────────────────────────────────────────────────


def test_a_day_the_api_never_mentioned_stays_repairable() -> None:
    """A zero-filled day is an inference and must not harden into a fact.

    `save_all` never rewrites a settled day, so marking an unreported day
    settled would make a fabricated zero permanent — and `missing_days` would
    report no gap, because a fabricated zero is not a gap. The clip would carry
    a wrong number for ever and every check would call the history healthy.
    """
    publication = _publication()
    rows = [DailyRow(day=date(2026, 9, 5), views=40)]
    snapshots = snapshots_for(
        publication, rows, [], start=date(2026, 9, 4), end=date(2026, 9, 6), today=TODAY
    )
    by_day = {s.date: s for s in snapshots}

    # Reported and long past: settled, and never to be touched again.
    assert by_day[date(2026, 9, 5)].partial is False
    # Never reported: zero-filled, and still correctable by a later poll.
    assert by_day[date(2026, 9, 4)].partial is True
    assert by_day[date(2026, 9, 4)].views == 0
    assert by_day[date(2026, 9, 6)].partial is True


def test_a_quota_403_is_not_reported_as_a_missing_scope() -> None:
    """The two want opposite responses: wait, versus go and re-authorise.

    `ensure_scope` has already passed by the time a request is made, so a 403
    here is more likely quota than scope — and sending the operator to a consent
    screen over a limit that clears on its own fixes nothing.
    """
    client = AnalyticsClient(
        access_token=lambda: "t",
        scopes=(ANALYTICS_SCOPE,),
        transport=_transport({"error": {"message": "quotaExceeded"}}, status=403),
    )
    with pytest.raises(QuotaExceededError):
        client.daily("vid123", start=TODAY, end=TODAY)


def test_a_genuine_403_still_names_the_scope() -> None:
    client = AnalyticsClient(
        access_token=lambda: "t",
        scopes=(ANALYTICS_SCOPE,),
        transport=_transport({"error": {"message": "insufficientPermissions"}}, status=403),
    )
    with pytest.raises(AnalyticsScopeError):
        client.daily("vid123", start=TODAY, end=TODAY)


def test_a_posting_band_converts_rather_than_assuming_utc() -> None:
    """The label says UTC, so the number had better be UTC.

    22:30 in UTC+4 is 18:30 UTC. Reading `.hour` off the original would file it
    under "20-24 UTC" — a bucket whose own label says it is not.
    """
    east = timezone(timedelta(hours=4))
    facts = ClipFacts(
        clip_id="c",
        publication_id="p",
        published_at=datetime(2026, 9, 10, 22, 30, tzinfo=east),
    )
    assert bucket_for(CohortKind.POSTING_HOUR, facts) == "16-20 UTC"


def test_a_mean_needs_enough_values_not_enough_clips() -> None:
    """Five clips in a bucket and one retention figure is not five clips of evidence.

    YouTube withholds the curve below its privacy threshold, so this is the
    normal case rather than an odd one: the bucket is large and the measurement
    is rare. Publishing that single number beside n=5 is exactly the
    misrepresentation the threshold exists to prevent.
    """
    facts = [
        ClipFacts(
            clip_id=f"c{i}",
            publication_id=f"p{i}",
            duration_sec=20.0,
            views=100,
            retention_at_half=0.9 if i == 0 else None,
        )
        for i in range(5)
    ]
    stat = next(s for s in summarise(facts) if s.kind is CohortKind.DURATION_BUCKET)
    assert stat.n == 5
    assert stat.mean_views is not None
    assert stat.mean_retention_at_half is None


def test_every_cohort_kind_has_a_bucket_rule() -> None:
    """`assert_never` makes a new dimension a type error, not a silent one.

    An earlier version let RENDER_PROFILE be the catch-all, so any unhandled
    kind would have been bucketed by render profile while a comment claimed the
    chain was proven exhaustive.
    """
    facts = ClipFacts(
        clip_id="c",
        publication_id="p",
        render_profile="default",
        caption_style="karaoke",
    )
    for kind in CohortKind:
        bucket_for(kind, facts)
