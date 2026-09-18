"""The three places ClipForge asks what is moving, each behind the same port.

None of them needs an API key, and that is a design constraint rather than a
convenience: an open-source project has to be usable from a clean clone, and a
trend feature that needs a Google Cloud project and a Reddit app registration
before it says anything is a feature most people will never turn on.

What each one knows, and what it cannot:

- **Google Trends** publishes a daily RSS feed of trending searches per region,
  with an approximate search volume. It says what people are looking for and
  nothing whatsoever about video.
- **Reddit** publishes an Atom feed of the top of any subreddit. Its JSON API
  refuses unauthenticated scripts with a 403 (measured, September 2026) and the
  feed does not; the feed carries no upvote counts, so position in the list is
  the only strength it can offer. It very often links the video itself.
- **YouTube search**, through yt-dlp, is the only one that returns things the
  pipeline can cut. Searched with the view-count sort and an upload-date filter,
  which is the closest thing to "trending" YouTube still exposes since it
  removed its Trending page in 2025. A flat search returns titles, durations and
  view counts but not upload times, so the top few hits are looked up in full
  to get one — bounded by ``videos_per_topic``, because each is a request.

Every network call is behind an injectable transport or callable so the unit
tier can feed these fixtures and never reach the internet.
"""

from __future__ import annotations

import html
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote_plus
from xml.etree import ElementTree

import httpx
from clipforge_contracts import TrendSource

from clipforge.media.sources import parse_youtube_id
from clipforge.observability import get_logger
from clipforge.research.signals import ResearchRequest, Signal, VideoHit

log = get_logger(__name__)

__all__ = [
    "GoogleTrendsProvider",
    "ProviderError",
    "RedditProvider",
    "YouTubeSearchProvider",
    "parse_google_trends",
    "parse_reddit_feed",
    "search_params",
    "strength_from_position",
    "strength_from_traffic",
    "strength_from_velocity",
]


class ProviderError(RuntimeError):
    """A provider could not answer. Recorded by the stage, never fatal on its own."""


# ── Strength scales ──────────────────────────────────────────────────────────
#
# Each provider measures something different, and the scorer needs them on one
# axis. These are log scales with the knee placed where the number stops being
# noise: 20K searches, a post in the top few, a thousand views an hour.


def strength_from_traffic(approx_searches: int) -> float:
    """Google's '200K+' → 0-1. 20K is barely news; 1M is the top of the page."""
    return _clamp((math.log10(max(approx_searches, 1)) - 3.5) / 3.0)


def strength_from_position(position: int, total: int) -> float:
    """Where a post sits in a top-of-the-day list → 0.15-0.7.

    A floor rather than zero for the last one: it is still in the top fifty of
    the day, which is more than most things can say. And a ceiling well short
    of 1.0: the top of r/videos is the top of one subreddit on one day, however
    quiet the day was, and the first real run ranked eight of its posts above
    every trending search because a position of 1 read as the loudest thing
    any provider can say. It is not; a million searches is.
    """
    if total <= 1:
        return 0.7
    return 0.15 + 0.55 * (1.0 - (position - 1) / (total - 1))


def strength_from_velocity(views_per_hour: float) -> float:
    """Views per hour → 0-1. Ten an hour is nothing; ten thousand is a moment."""
    return _clamp((math.log10(max(views_per_hour, 1.0)) - 1.0) / 3.5)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


# ── Google Trends ────────────────────────────────────────────────────────────

_TRAFFIC = re.compile(r"(\d+(?:[.,]\d+)?)\s*([KM]?)\+?", re.IGNORECASE)


def _approx_traffic(text: str | None) -> int:
    """'200K+' → 200000. '2000+' → 2000. Anything unreadable → 0."""
    if not text:
        return 0
    match = _TRAFFIC.search(text.replace(",", ""))
    if not match:
        return 0
    number = float(match.group(1))
    unit = match.group(2).upper()
    return int(number * {"": 1, "K": 1_000, "M": 1_000_000}[unit])


def _local(tag: str) -> str:
    """Strip an XML namespace: '{https://...}approx_traffic' → 'approx_traffic'."""
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ElementTree.Element, name: str) -> str | None:
    for child in element:
        if _local(child.tag) == name:
            return (child.text or "").strip() or None
    return None


def parse_google_trends(body: str, *, now: datetime, lookback_hours: int) -> list[Signal]:
    """Read the daily trending-searches feed into signals.

    Matched on local tag names rather than the namespace URI: the URI has moved
    once already, and a feed that stops parsing because a constant changed
    would fail the whole provider over nothing.
    """
    try:
        root = ElementTree.fromstring(body)  # noqa: S314 - a fixed, trusted feed URL
    except ElementTree.ParseError as exc:
        raise ProviderError(f"Google Trends returned something that is not RSS: {exc}") from exc

    cutoff = now - timedelta(hours=lookback_hours)
    signals: list[Signal] = []
    for item in root.iter():
        if _local(item.tag) != "item":
            continue
        title = _child_text(item, "title")
        if not title:
            continue
        traffic = _approx_traffic(_child_text(item, "approx_traffic"))
        observed = _rfc822(_child_text(item, "pubDate"))
        if observed is not None and observed < cutoff:
            continue
        news_url: str | None = None
        for child in item:
            if _local(child.tag) == "news_item":
                news_url = _child_text(child, "news_item_url")
                break
        signals.append(
            Signal(
                source=TrendSource.GOOGLE_TRENDS,
                topic=title[:200],
                strength=strength_from_traffic(traffic),
                detail=f"{_child_text(item, 'approx_traffic') or '?'} searches",
                url=news_url,
                observed_at=observed,
            )
        )
    return signals


def _rfc822(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class GoogleTrendsProvider:
    """The daily trending-searches feed, per region."""

    source = TrendSource.GOOGLE_TRENDS
    FEED = "https://trends.google.com/trending/rss"

    def __init__(
        self,
        *,
        user_agent: str,
        timeout_s: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._timeout_s = timeout_s
        self._transport = transport

    def fetch(self, request: ResearchRequest) -> list[Signal]:
        body = _get(
            f"{self.FEED}?geo={request.region}",
            user_agent=self._user_agent,
            timeout_s=self._timeout_s,
            transport=self._transport,
            what="Google Trends",
        )
        signals = parse_google_trends(body, now=request.now, lookback_hours=request.lookback_hours)
        log.info("research.google_trends", region=request.region, signals=len(signals))
        return signals


# ── Reddit ───────────────────────────────────────────────────────────────────

_HREF = re.compile(r'href="([^"]+)"')


def parse_reddit_feed(
    body: str, *, subreddit: str, now: datetime, lookback_hours: int
) -> list[Signal]:
    """Read a subreddit's top-of-the-day Atom feed into signals.

    The outbound link lives inside the HTML-escaped ``content`` block, as the
    ``[link]`` anchor. It is the only place the feed says what a post points at,
    so it is unescaped and searched for a YouTube URL; a post that links
    anywhere else is still a topic, just not a video the pipeline can fetch.
    """
    try:
        root = ElementTree.fromstring(body)  # noqa: S314 - a fixed, trusted feed URL
    except ElementTree.ParseError as exc:
        raise ProviderError(f"r/{subreddit} returned something that is not a feed: {exc}") from exc

    entries = [element for element in root.iter() if _local(element.tag) == "entry"]
    cutoff = now - timedelta(hours=lookback_hours)
    signals: list[Signal] = []
    for position, entry in enumerate(entries, start=1):
        title = _child_text(entry, "title")
        if not title:
            continue
        observed = _iso(_child_text(entry, "published") or _child_text(entry, "updated"))
        if observed is not None and observed < cutoff:
            continue
        permalink: str | None = None
        thumbnail: str | None = None
        for child in entry:
            name = _local(child.tag)
            if name == "link" and child.get("href"):
                permalink = child.get("href")
            elif name == "thumbnail" and child.get("url"):
                thumbnail = child.get("url")
        content = html.unescape(_child_text(entry, "content") or "")
        video = _youtube_link(content, title=title, thumbnail=thumbnail)
        signals.append(
            Signal(
                source=TrendSource.REDDIT,
                topic=title[:200],
                strength=strength_from_position(position, len(entries)),
                detail=f"r/{subreddit} · #{position} today",
                url=permalink,
                observed_at=observed,
                videos=(video,) if video else (),
            )
        )
    return signals


def _youtube_link(content: str, *, title: str, thumbnail: str | None) -> VideoHit | None:
    for href in _HREF.findall(content):
        video_id = parse_youtube_id(href)
        if video_id is not None:
            return VideoHit(
                url=f"https://www.youtube.com/watch?v={video_id}",
                external_id=video_id,
                title=title[:300],
                via=TrendSource.REDDIT,
                thumbnail_url=thumbnail,
            )
    return None


def _iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class RedditProvider:
    """The top of a few subreddits over the last day, from their feeds."""

    source = TrendSource.REDDIT
    FEED = "https://www.reddit.com/r/{subreddit}/top/.rss?t=day&limit=50"

    def __init__(
        self,
        *,
        user_agent: str,
        default_subreddits: tuple[str, ...] = ("videos", "popular"),
        timeout_s: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._defaults = default_subreddits
        self._timeout_s = timeout_s
        self._transport = transport

    def fetch(self, request: ResearchRequest) -> list[Signal]:
        subreddits = request.subreddits or self._defaults
        signals: list[Signal] = []
        failures: list[str] = []
        for subreddit in subreddits:
            try:
                body = _get(
                    self.FEED.format(subreddit=subreddit),
                    user_agent=self._user_agent,
                    timeout_s=self._timeout_s,
                    transport=self._transport,
                    what=f"r/{subreddit}",
                )
                signals.extend(
                    parse_reddit_feed(
                        body,
                        subreddit=subreddit,
                        now=request.now,
                        lookback_hours=request.lookback_hours,
                    )
                )
            except ProviderError as exc:
                # One subreddit down is not Reddit down. Carry on, and only
                # raise when nothing at all answered.
                failures.append(str(exc))
                log.warning("research.reddit_subreddit_failed", subreddit=subreddit, error=str(exc))
        if not signals and failures:
            raise ProviderError("; ".join(failures))
        log.info("research.reddit", subreddits=list(subreddits), signals=len(signals))
        return signals


# ── YouTube search ───────────────────────────────────────────────────────────

Search = Callable[[str, int], list[dict[str, Any]]]
Inspect = Callable[[str], dict[str, Any]]


def search_params(lookback_hours: int) -> str:
    """YouTube's ``sp`` filter: sort by view count, uploaded within the window.

    Opaque protobuf strings, read off the search page's own filter buttons.
    The upload-date filter only comes in day, week and month, so the lookback
    is rounded up to the one that contains it.
    """
    if lookback_hours <= 24:
        return "CAMSAggC"  # today
    if lookback_hours <= 24 * 7:
        return "CAMSAggD"  # this week
    return "CAMSAggE"  # this month


def _search_url(query: str, lookback_hours: int) -> str:
    return (
        "https://www.youtube.com/results?search_query="
        f"{quote_plus(query)}&sp={search_params(lookback_hours)}"
    )


def _ytdlp_search(query_url: str, limit: int) -> list[dict[str, Any]]:
    import yt_dlp  # the `media` extra; imported here so the core install works without it

    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "playlistend": limit,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(query_url, download=False)
    entries = info.get("entries") if isinstance(info, dict) else None
    return [entry for entry in (entries or []) if isinstance(entry, dict)]


def _ytdlp_inspect(url: str) -> dict[str, Any]:
    import yt_dlp

    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False, process=False)
    return info if isinstance(info, dict) else {}


@dataclass(frozen=True)
class _Flat:
    """What a flat search entry says, typed."""

    video_id: str
    title: str
    channel: str | None
    duration_sec: float | None
    view_count: int | None
    thumbnail_url: str | None


def _flat(entry: dict[str, Any]) -> _Flat | None:
    video_id = entry.get("id")
    title = entry.get("title")
    if not isinstance(video_id, str) or not isinstance(title, str):
        return None
    if entry.get("live_status") in {"is_live", "is_upcoming"}:
        return None
    thumbnails = entry.get("thumbnails")
    thumbnail = None
    if isinstance(thumbnails, list) and thumbnails:
        last = thumbnails[-1]
        if isinstance(last, dict) and isinstance(last.get("url"), str):
            thumbnail = str(last["url"])
    channel = entry.get("channel") or entry.get("uploader")
    return _Flat(
        video_id=video_id,
        title=title,
        channel=channel if isinstance(channel, str) else None,
        duration_sec=_as_float(entry.get("duration")),
        view_count=_as_int(entry.get("view_count")),
        thumbnail_url=thumbnail,
    )


class YouTubeSearchProvider:
    """Recency-filtered, view-sorted YouTube search, through yt-dlp.

    Two jobs. As a *provider* it searches each of the run's own topics and
    reports how fast the best result is being watched. As the *video finder*
    it does the same search for a topic some other provider surfaced, so a
    trending search phrase acquires videos the pipeline can cut.
    """

    source = TrendSource.YOUTUBE

    def __init__(self, *, search: Search | None = None, inspect: Inspect | None = None) -> None:
        self._search = search or _ytdlp_search
        self._inspect = inspect or _ytdlp_inspect

    def fetch(self, request: ResearchRequest) -> list[Signal]:
        signals: list[Signal] = []
        for topic in request.topics:
            hits = self.find_videos(topic, limit=request.videos_per_topic, request=request)
            if not hits:
                continue
            best = max(hits, key=lambda hit: _velocity(hit, request.now) or 0.0)
            velocity = _velocity(best, request.now)
            signals.append(
                Signal(
                    source=TrendSource.YOUTUBE,
                    topic=topic,
                    strength=strength_from_velocity(velocity) if velocity else 0.1,
                    detail=(
                        f"{_short(best.view_count)} views on the top result"
                        + (f", {velocity:,.0f}/hour" if velocity else "")
                    ),
                    url=best.url,
                    observed_at=best.uploaded_at,
                    videos=tuple(hits),
                )
            )
        log.info("research.youtube", topics=len(request.topics), signals=len(signals))
        return signals

    def enrich(self, hit: VideoHit) -> VideoHit:
        """Fill in what another provider's sighting did not know.

        A Reddit link is a video with no view count and no upload time, which
        the scorer treats as neutral — so a video somebody chose to share
        scored below one a search merely returned. One lookup fixes that, and
        the stage bounds how many it asks for.
        """
        if hit.view_count is not None and hit.uploaded_at is not None:
            return hit
        flat = _Flat(
            video_id=hit.external_id,
            title=hit.title,
            channel=hit.channel,
            duration_sec=hit.duration_sec,
            view_count=hit.view_count,
            thumbnail_url=hit.thumbnail_url,
        )
        looked_up = self._enrich(flat)
        return VideoHit(
            url=hit.url,
            external_id=hit.external_id,
            title=hit.title,
            via=hit.via,
            channel=hit.channel or looked_up.channel,
            duration_sec=hit.duration_sec or looked_up.duration_sec,
            view_count=hit.view_count or looked_up.view_count,
            uploaded_at=hit.uploaded_at or looked_up.uploaded_at,
            thumbnail_url=hit.thumbnail_url or looked_up.thumbnail_url,
            channel_followers=hit.channel_followers or looked_up.channel_followers,
        )

    def find_videos(self, topic: str, *, limit: int, request: ResearchRequest) -> list[VideoHit]:
        try:
            entries = self._search(_search_url(topic, request.lookback_hours), limit)
        except Exception as exc:
            raise ProviderError(f"YouTube search for {topic!r} failed: {str(exc)[:200]}") from exc

        hits: list[VideoHit] = []
        for entry in entries[:limit]:
            flat = _flat(entry)
            if flat is None:
                continue
            hits.append(self._enrich(flat))
        return hits

    def _enrich(self, flat: _Flat) -> VideoHit:
        """One full lookup per hit, for the upload time a flat entry lacks.

        A failure here is not a failure: the hit is still a hit, just one whose
        velocity the scorer will treat as unknown.
        """
        url = f"https://www.youtube.com/watch?v={flat.video_id}"
        uploaded: datetime | None = None
        followers: int | None = None
        view_count = flat.view_count
        duration = flat.duration_sec
        try:
            full = self._inspect(url)
        except Exception as exc:  # noqa: BLE001 - a missing upload time is not worth a stage
            log.debug("research.inspect_failed", video=flat.video_id, error=str(exc)[:120])
        else:
            timestamp = _as_int(full.get("timestamp"))
            if timestamp:
                uploaded = datetime.fromtimestamp(timestamp, tz=UTC)
            elif isinstance(full.get("upload_date"), str):
                uploaded = _yyyymmdd(str(full["upload_date"]))
            followers = _as_int(full.get("channel_follower_count"))
            view_count = _as_int(full.get("view_count")) or view_count
            duration = _as_float(full.get("duration")) or duration
        return VideoHit(
            url=url,
            external_id=flat.video_id,
            title=flat.title[:300],
            via=TrendSource.YOUTUBE,
            channel=flat.channel,
            duration_sec=duration,
            view_count=view_count,
            uploaded_at=uploaded,
            thumbnail_url=flat.thumbnail_url,
            channel_followers=followers,
        )


def _velocity(hit: VideoHit, now: datetime) -> float | None:
    if hit.view_count is None or hit.uploaded_at is None:
        return None
    hours = max((now - hit.uploaded_at).total_seconds() / 3600.0, 1.0)
    return hit.view_count / hours


def _yyyymmdd(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, "%Y%m%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def _short(count: int | None) -> str:
    if count is None:
        return "?"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.0f}K"
    return str(count)


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    return None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


# ── HTTP, once ───────────────────────────────────────────────────────────────


def _get(
    url: str,
    *,
    user_agent: str,
    timeout_s: float,
    transport: httpx.BaseTransport | None,
    what: str,
) -> str:
    try:
        with httpx.Client(timeout=timeout_s, transport=transport, follow_redirects=True) as client:
            response = client.get(url, headers={"User-Agent": user_agent})
    except httpx.HTTPError as exc:
        raise ProviderError(f"{what} could not be reached: {exc}") from exc
    if response.status_code != 200:
        raise ProviderError(f"{what} answered {response.status_code}")
    return response.text


def iter_default_providers(
    *,
    user_agent: str,
    subreddits: Iterable[str],
    timeout_s: float,
    google_trends: bool,
    reddit: bool,
    youtube: bool,
) -> list[GoogleTrendsProvider | RedditProvider | YouTubeSearchProvider]:
    """The configured providers, in the order they are asked."""
    providers: list[GoogleTrendsProvider | RedditProvider | YouTubeSearchProvider] = []
    if google_trends:
        providers.append(GoogleTrendsProvider(user_agent=user_agent, timeout_s=timeout_s))
    if reddit:
        providers.append(
            RedditProvider(
                user_agent=user_agent,
                default_subreddits=tuple(subreddits) or ("videos", "popular"),
                timeout_s=timeout_s,
            )
        )
    if youtube:
        providers.append(YouTubeSearchProvider())
    return providers
