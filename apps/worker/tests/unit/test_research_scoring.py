"""The ranking: pure, deterministic, and explainable by its parts.

What a row's score is made of matters more than the number, because the
number is what a person sees and the parts are what they will disagree with.
Each part is pinned on its own, and the combination is pinned as an order.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from clipforge.research.scoring import (
    blend_rank,
    cluster_signals,
    dedupe_videos,
    lookup_query,
    matched_interests,
    rank_trends,
    score_trend,
    score_video,
    topic_tokens,
)
from clipforge.research.signals import Signal, VideoHit
from clipforge_contracts import TrendSource

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
MAX = 4 * 60 * 60.0


def signal(
    topic: str,
    source: TrendSource = TrendSource.GOOGLE_TRENDS,
    *,
    strength: float = 0.5,
    hours_ago: float | None = 2.0,
    videos: tuple[VideoHit, ...] = (),
) -> Signal:
    return Signal(
        source=source,
        topic=topic,
        strength=strength,
        observed_at=None if hours_ago is None else NOW - timedelta(hours=hours_ago),
        videos=videos,
    )


def hit(
    video_id: str,
    *,
    via: TrendSource = TrendSource.YOUTUBE,
    views: int | None = 10_000,
    hours_ago: float | None = 5.0,
    duration: float | None = 300.0,
    channel: str | None = None,
) -> VideoHit:
    return VideoHit(
        url=f"https://www.youtube.com/watch?v={video_id}",
        external_id=video_id,
        title=f"Video {video_id}",
        via=via,
        channel=channel,
        duration_sec=duration,
        view_count=views,
        uploaded_at=None if hours_ago is None else NOW - timedelta(hours=hours_ago),
    )


# ── Tokens and clusters ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_topic_tokens_drop_filler_and_case() -> None:
    assert topic_tokens("Watch: The Brewers vs the Orioles — HIGHLIGHTS") == {"brewers", "orioles"}


@pytest.mark.unit
def test_a_trending_phrase_joins_the_post_title_that_contains_it() -> None:
    clusters = cluster_signals(
        [
            signal("brewers vs orioles", TrendSource.GOOGLE_TRENDS, strength=0.6),
            signal(
                "Brewers beat the Orioles 3-1 with a ninth-inning homer",
                TrendSource.REDDIT,
                strength=0.9,
            ),
            signal("arsenal", TrendSource.GOOGLE_TRENDS, strength=0.3),
        ]
    )
    assert len(clusters) == 2
    baseball = next(c for c in clusters if "brewers" in c.tokens)
    assert baseball.sources == {TrendSource.GOOGLE_TRENDS, TrendSource.REDDIT}
    # The short phrase is the heading, not the sentence.
    assert baseball.label == "brewers vs orioles"


@pytest.mark.unit
def test_one_shared_word_is_not_one_topic() -> None:
    """Every story about the same person would otherwise become one row."""
    clusters = cluster_signals(
        [
            signal("trump tariffs", TrendSource.GOOGLE_TRENDS),
            signal("trump nominees hearing", TrendSource.REDDIT),
        ]
    )
    assert len(clusters) == 2


@pytest.mark.unit
def test_an_identical_single_word_topic_does_merge() -> None:
    clusters = cluster_signals(
        [signal("arsenal", TrendSource.GOOGLE_TRENDS), signal("Arsenal!", TrendSource.REDDIT)]
    )
    assert len(clusters) == 1


@pytest.mark.unit
def test_a_signal_with_no_words_left_is_dropped() -> None:
    assert cluster_signals([signal("the and of")]) == []


# ── Videos ───────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_dedupe_keeps_what_each_sighting_knew_and_prefers_the_sharer() -> None:
    merged = dedupe_videos(
        [
            hit("aaaaaaaaaaa", via=TrendSource.YOUTUBE, views=5000, channel="C"),
            hit("aaaaaaaaaaa", via=TrendSource.REDDIT, views=None, hours_ago=None),
            hit("bbbbbbbbbbb"),
        ]
    )
    assert [m.external_id for m in merged] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    first = merged[0]
    assert first.via is TrendSource.REDDIT
    assert first.view_count == 5000
    assert first.channel == "C"


@pytest.mark.unit
def test_a_fresh_fast_video_outscores_an_old_slow_one() -> None:
    fast = score_video(hit("a", views=200_000, hours_ago=4), now=NOW, max_duration_sec=MAX)
    slow = score_video(hit("b", views=200, hours_ago=400), now=NOW, max_duration_sec=MAX)
    assert fast > slow


@pytest.mark.unit
def test_a_video_the_pipeline_would_refuse_gets_no_length_credit() -> None:
    fits = score_video(hit("a", duration=600), now=NOW, max_duration_sec=MAX)
    too_long = score_video(hit("a", duration=MAX + 1), now=NOW, max_duration_sec=MAX)
    assert fits - too_long == 25


@pytest.mark.unit
def test_unknowns_score_in_the_middle_rather_than_at_zero() -> None:
    unknown = score_video(
        hit("a", views=None, hours_ago=None, duration=None), now=NOW, max_duration_sec=MAX
    )
    assert 30 <= unknown <= 40


# ── Trends ───────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_agreement_between_providers_is_worth_the_most() -> None:
    alone = cluster_signals([signal("solo topic", strength=0.9)])[0]
    agreed = cluster_signals(
        [
            signal("shared topic", TrendSource.GOOGLE_TRENDS, strength=0.5),
            signal("shared topic", TrendSource.REDDIT, strength=0.5),
            signal("shared topic", TrendSource.YOUTUBE, strength=0.5),
        ]
    )[0]
    kwargs: dict[str, Any] = {
        "now": NOW,
        "lookback_hours": 48,
        "interests": [],
        "max_duration_sec": MAX,
    }
    assert score_trend(agreed, **kwargs).score > score_trend(alone, **kwargs).score


@pytest.mark.unit
def test_an_interest_match_is_recorded_and_rewarded() -> None:
    cluster = cluster_signals(
        [signal("premier league goals of the week", TrendSource.GOOGLE_TRENDS)]
    )[0]
    plain = score_trend(cluster, now=NOW, lookback_hours=48, interests=[], max_duration_sec=MAX)
    matched = score_trend(
        cluster, now=NOW, lookback_hours=48, interests=["premier league"], max_duration_sec=MAX
    )
    assert matched.matched == ("premier league",)
    assert matched.score - plain.score == 20


@pytest.mark.unit
def test_a_topic_the_run_searched_for_matches_by_name() -> None:
    cluster = cluster_signals([signal("F1", TrendSource.YOUTUBE)])[0]
    assert matched_interests(cluster, ["F1", "tennis"]) == ["F1"]


@pytest.mark.unit
def test_videos_raise_a_trend_and_the_best_one_is_scored() -> None:
    kwargs: dict[str, Any] = {
        "now": NOW,
        "lookback_hours": 48,
        "interests": [],
        "max_duration_sec": MAX,
    }
    without = cluster_signals([signal("topic")])[0]
    with_video = cluster_signals([signal("topic", videos=(hit("aaaaaaaaaaa"),))])[0]
    assert score_trend(with_video, **kwargs).score > score_trend(without, **kwargs).score
    assert score_trend(with_video, **kwargs).videos[0][1] == score_video(
        hit("aaaaaaaaaaa"), now=NOW, max_duration_sec=MAX
    )


@pytest.mark.unit
def test_ranking_is_best_first_bounded_and_stable() -> None:
    kwargs: dict[str, Any] = {
        "now": NOW,
        "lookback_hours": 48,
        "interests": [],
        "max_duration_sec": MAX,
    }
    clusters = cluster_signals(
        [
            signal("quiet", strength=0.1),
            signal("loud", strength=0.9),
            signal("medium", strength=0.5),
        ]
    )
    scored = [score_trend(c, **kwargs) for c in clusters]
    twice = [rank_trends(scored, limit=2) for _ in range(2)]
    assert [t.cluster.label for t in twice[0]] == ["loud", "medium"]
    assert [t.cluster.label for t in twice[1]] == ["loud", "medium"]


@pytest.mark.unit
def test_blending_keeps_most_of_the_weight_on_the_arithmetic() -> None:
    assert blend_rank(80, None, worth_clipping=True) == 80
    assert blend_rank(80, 10, worth_clipping=True) == 88
    assert blend_rank(80, 0, worth_clipping=True) == 48
    # Not worth clipping sinks the row without removing it.
    assert blend_rank(80, 10, worth_clipping=False) == 53


# ── Looking up a feed phrase ─────────────────────────────────────────────────


@pytest.mark.unit
def test_a_lookup_appends_the_category_hint_unless_the_phrase_already_says_it() -> None:
    assert lookup_query("portland st vs oregon", "nfl") == "portland st vs oregon nfl"
    assert lookup_query("f1 grand prix", "f1") == "f1 grand prix"
    assert lookup_query("Grand Prix", "F1") == "Grand Prix F1"
    assert lookup_query("starz", None) == "starz"
    assert lookup_query("starz", "") == "starz"
