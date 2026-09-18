"""The trend providers: what each feed says, read without the internet.

Every fixture here is the shape of a real response captured in September 2026,
trimmed. The parsers are the fragile part of research — a feed that changes
shape stops the whole provider — so each is pinned against the shape it was
written for, and a change that breaks one fails here rather than at 6am.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from clipforge.research.providers import (
    GoogleTrendsProvider,
    ProviderError,
    RedditProvider,
    YouTubeSearchProvider,
    parse_google_trends,
    parse_reddit_feed,
    search_params,
    strength_from_position,
    strength_from_traffic,
    strength_from_velocity,
)
from clipforge.research.signals import ResearchRequest
from clipforge_contracts import TrendSource

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def request(**over: Any) -> ResearchRequest:
    defaults: dict[str, Any] = {
        "topics": (),
        "region": "US",
        "lookback_hours": 48,
        "videos_per_topic": 3,
        "subreddits": (),
        "now": NOW,
    }
    return ResearchRequest(**{**defaults, **over})


GOOGLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0">
  <channel>
    <title>Daily Search Trends</title>
    <item>
      <title>brewers vs orioles</title>
      <ht:approx_traffic>200K+</ht:approx_traffic>
      <pubDate>Fri, 18 Sep 2026 15:10:00 -0700</pubDate>
      <ht:news_item>
        <ht:news_item_title>Orioles and Brewers lineups</ht:news_item_title>
        <ht:news_item_url>https://www.masnsports.com/lineups</ht:news_item_url>
      </ht:news_item>
    </item>
    <item>
      <title>stale thing</title>
      <ht:approx_traffic>2M+</ht:approx_traffic>
      <pubDate>Mon, 01 Sep 2026 15:10:00 -0700</pubDate>
    </item>
    <item>
      <title></title>
      <ht:approx_traffic>5000+</ht:approx_traffic>
    </item>
  </channel>
</rss>
"""

REDDIT_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">
  <entry>
    <content type="html">&lt;table&gt;&lt;a href=&quot;https://youtu.be/Zx9ICWApA0c?si=abc&quot;&gt;[link]&lt;/a&gt;&lt;/table&gt;</content>
    <id>t3_one</id>
    <media:thumbnail url="https://external-preview.redd.it/one.jpeg" />
    <link href="https://www.reddit.com/r/videos/comments/one/" />
    <published>2026-09-18T15:59:21+00:00</published>
    <title>Nominees refuse to answer a simple question</title>
  </entry>
  <entry>
    <content type="html">&lt;a href=&quot;https://v.redd.it/abc&quot;&gt;[link]&lt;/a&gt;</content>
    <id>t3_two</id>
    <link href="https://www.reddit.com/r/videos/comments/two/" />
    <published>2026-09-18T10:00:00+00:00</published>
    <title>A cat does a thing</title>
  </entry>
  <entry>
    <content type="html">old</content>
    <id>t3_three</id>
    <link href="https://www.reddit.com/r/videos/comments/three/" />
    <published>2026-08-01T10:00:00+00:00</published>
    <title>Something from last month</title>
  </entry>
</feed>
"""


# ── Scales ───────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_traffic_strength_puts_the_knee_at_twenty_thousand_searches() -> None:
    assert strength_from_traffic(2_000) == 0.0
    assert 0.2 < strength_from_traffic(20_000) < 0.35
    assert 0.45 < strength_from_traffic(100_000) < 0.55
    assert strength_from_traffic(5_000_000) == 1.0


@pytest.mark.unit
def test_position_strength_keeps_a_floor_for_the_last_post() -> None:
    assert strength_from_position(1, 50) == 1.0
    assert strength_from_position(50, 50) == pytest.approx(0.2)
    assert strength_from_position(1, 1) == 1.0


@pytest.mark.unit
def test_velocity_strength_saturates_at_tens_of_thousands_an_hour() -> None:
    assert strength_from_velocity(5.0) == 0.0
    assert 0.5 < strength_from_velocity(1_000.0) < 0.6
    assert strength_from_velocity(50_000.0) == 1.0


# ── Google Trends ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_google_trends_reads_topic_traffic_and_the_first_news_link() -> None:
    signals = parse_google_trends(GOOGLE_RSS, now=NOW, lookback_hours=48)
    assert [s.topic for s in signals] == ["brewers vs orioles"]
    only = signals[0]
    assert only.source is TrendSource.GOOGLE_TRENDS
    assert only.strength == pytest.approx(strength_from_traffic(200_000))
    assert only.detail == "200K+ searches"
    assert only.url == "https://www.masnsports.com/lineups"
    assert only.observed_at == datetime(2026, 9, 18, 22, 10, tzinfo=UTC)
    assert only.videos == ()


@pytest.mark.unit
def test_google_trends_drops_items_outside_the_lookback_and_untitled_ones() -> None:
    """The 2M+ item is the loudest thing in the feed and three weeks old."""
    topics = [s.topic for s in parse_google_trends(GOOGLE_RSS, now=NOW, lookback_hours=48)]
    assert "stale thing" not in topics
    assert "" not in topics


@pytest.mark.unit
def test_google_trends_refuses_something_that_is_not_rss() -> None:
    with pytest.raises(ProviderError, match="not RSS"):
        parse_google_trends("<html>blocked</html", now=NOW, lookback_hours=48)


@pytest.mark.unit
def test_google_trends_provider_asks_for_the_region_and_identifies_itself() -> None:
    seen: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, text=GOOGLE_RSS)

    provider = GoogleTrendsProvider(
        user_agent="ClipForge/test", transport=httpx.MockTransport(handle)
    )
    signals = provider.fetch(request(region="GB"))

    assert len(signals) == 1
    assert seen[0].url.params["geo"] == "GB"
    assert seen[0].headers["user-agent"] == "ClipForge/test"


@pytest.mark.unit
def test_a_refusal_is_a_provider_error_not_a_crash() -> None:
    provider = GoogleTrendsProvider(
        user_agent="x", transport=httpx.MockTransport(lambda _r: httpx.Response(403))
    )
    with pytest.raises(ProviderError, match="403"):
        provider.fetch(request())


# ── Reddit ───────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_reddit_reads_titles_positions_and_the_youtube_link_inside_the_content() -> None:
    signals = parse_reddit_feed(REDDIT_ATOM, subreddit="videos", now=NOW, lookback_hours=48)
    assert [s.topic for s in signals] == [
        "Nominees refuse to answer a simple question",
        "A cat does a thing",
    ]
    first, second = signals
    assert first.source is TrendSource.REDDIT
    assert first.strength == 1.0
    assert first.detail == "r/videos · #1 today"
    assert first.url == "https://www.reddit.com/r/videos/comments/one/"
    assert len(first.videos) == 1
    video = first.videos[0]
    # The `?si=` tracking parameter is gone and the id is canonical.
    assert video.url == "https://www.youtube.com/watch?v=Zx9ICWApA0c"
    assert video.external_id == "Zx9ICWApA0c"
    assert video.via is TrendSource.REDDIT
    assert video.thumbnail_url == "https://external-preview.redd.it/one.jpeg"
    # A v.redd.it link is a topic the pipeline cannot fetch, so no video.
    assert second.videos == ()
    assert second.strength < first.strength


@pytest.mark.unit
def test_reddit_skips_posts_older_than_the_lookback() -> None:
    topics = [
        s.topic
        for s in parse_reddit_feed(REDDIT_ATOM, subreddit="videos", now=NOW, lookback_hours=48)
    ]
    assert "Something from last month" not in topics


@pytest.mark.unit
def test_reddit_provider_carries_on_when_one_subreddit_refuses() -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        if "/r/videos/" in str(req.url):
            return httpx.Response(200, text=REDDIT_ATOM)
        return httpx.Response(403)

    provider = RedditProvider(user_agent="x", transport=httpx.MockTransport(handle))
    signals = provider.fetch(request(subreddits=("videos", "blocked")))
    assert len(signals) == 2


@pytest.mark.unit
def test_reddit_provider_fails_only_when_nothing_answered() -> None:
    provider = RedditProvider(
        user_agent="x", transport=httpx.MockTransport(lambda _r: httpx.Response(403))
    )
    with pytest.raises(ProviderError, match="403"):
        provider.fetch(request(subreddits=("videos",)))


@pytest.mark.unit
def test_reddit_provider_uses_its_defaults_when_the_run_names_none() -> None:
    asked: list[str] = []

    def handle(req: httpx.Request) -> httpx.Response:
        asked.append(str(req.url))
        return httpx.Response(200, text=REDDIT_ATOM)

    provider = RedditProvider(
        user_agent="x",
        default_subreddits=("videos", "popular"),
        transport=httpx.MockTransport(handle),
    )
    provider.fetch(request())
    assert any("/r/videos/" in url for url in asked)
    assert any("/r/popular/" in url for url in asked)


# ── YouTube ──────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_search_params_round_the_lookback_up_to_youtube_s_own_filters() -> None:
    assert search_params(6) == "CAMSAggC"  # today
    assert search_params(48) == "CAMSAggD"  # this week
    assert search_params(168) == "CAMSAggD"
    assert search_params(169) == "CAMSAggE"  # this month


def flat(video_id: str, *, views: int, duration: int = 300) -> dict[str, Any]:
    return {
        "id": video_id,
        "title": f"Video {video_id}",
        "duration": duration,
        "view_count": views,
        "channel": "A channel",
        "thumbnails": [{"url": "small"}, {"url": f"https://i.ytimg.com/{video_id}.jpg"}],
    }


@pytest.mark.unit
def test_youtube_provider_searches_each_topic_and_reports_the_fastest_result() -> None:
    urls: list[str] = []

    def search(url: str, limit: int) -> list[dict[str, Any]]:
        urls.append(url)
        return [flat("aaaaaaaaaaa", views=100_000), flat("bbbbbbbbbbb", views=50_000)]

    def inspect(url: str) -> dict[str, Any]:
        uploaded = NOW - timedelta(hours=10 if "aaaa" in url else 100)
        return {"timestamp": int(uploaded.timestamp()), "channel_follower_count": 12_000}

    provider = YouTubeSearchProvider(search=search, inspect=inspect)
    signals = provider.fetch(request(topics=("premier league",), lookback_hours=48))

    assert len(signals) == 1
    signal = signals[0]
    assert signal.source is TrendSource.YOUTUBE
    assert signal.topic == "premier league"
    assert "search_query=premier+league" in urls[0]
    assert "sp=CAMSAggD" in urls[0]
    # 100K views in 10 hours is the faster of the two.
    assert signal.url == "https://www.youtube.com/watch?v=aaaaaaaaaaa"
    assert signal.strength == pytest.approx(strength_from_velocity(10_000.0))
    assert "100K views" in (signal.detail or "")
    assert len(signal.videos) == 2
    assert signal.videos[0].channel_followers == 12_000
    assert signal.videos[0].thumbnail_url == "https://i.ytimg.com/aaaaaaaaaaa.jpg"


@pytest.mark.unit
def test_youtube_provider_tolerates_a_failed_lookup_and_a_live_stream() -> None:
    def search(url: str, limit: int) -> list[dict[str, Any]]:
        return [
            flat("aaaaaaaaaaa", views=10),
            {**flat("bbbbbbbbbbb", views=10), "live_status": "is_live"},
            {"title": "no id"},
        ]

    def inspect(url: str) -> dict[str, Any]:
        raise RuntimeError("blocked")

    provider = YouTubeSearchProvider(search=search, inspect=inspect)
    hits = provider.find_videos("x", limit=5, request=request())
    assert [hit.external_id for hit in hits] == ["aaaaaaaaaaa"]
    assert hits[0].uploaded_at is None
    assert hits[0].view_count == 10


@pytest.mark.unit
def test_youtube_search_failure_is_a_provider_error() -> None:
    def search(url: str, limit: int) -> list[dict[str, Any]]:
        raise RuntimeError("Sign in to confirm you're not a bot")

    provider = YouTubeSearchProvider(search=search, inspect=lambda _u: {})
    with pytest.raises(ProviderError, match="not a bot"):
        provider.find_videos("x", limit=3, request=request())
