"""Turning signals into a ranked list, with no model in the loop.

Everything here is a pure function of what the providers returned and the
clock. That is what makes the ranking checkable: the same signals rank the same
way every time, and a row that ranked oddly can be explained by reading the
components rather than by asking a model why.

## What a trend is

Nothing any one provider says. A trending search is a phrase; a Reddit post is
a link; a YouTube search result is a video. A *trend* is what those agree on —
so signals are clustered by topic first, and the cluster is scored on how many
of them agree, how strongly, how recently, and whether there is anything to cut.

## Why clustering is by tokens and not by a model

Because it has to run on nothing, quickly, on sixty-odd short strings, and
because the mistakes it makes are visible: two rows that should have been one
are two rows, which a person reading the list will notice and can merge in
their head. A model that merged them for you would also merge the two Arsenal
stories that are not the same story, invisibly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from clipforge_contracts import TrendSource

from clipforge.research.signals import Signal, VideoHit

__all__ = [
    "Cluster",
    "ScoredTrend",
    "blend_rank",
    "cluster_signals",
    "dedupe_videos",
    "matched_interests",
    "rank_trends",
    "score_trend",
    "score_video",
    "topic_tokens",
]

# Words that carry no topic. Short and deliberately English: the feeds this
# reads are English-language by region, and a list that tried to be everything
# would be nothing.
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "by",
        "with",
        "from",
        "vs",
        "v",
        "versus",
        "is",
        "are",
        "was",
        "be",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "as",
        "into",
        "over",
        "after",
        "before",
        "new",
        "live",
        "full",
        "video",
        "watch",
        "highlights",
        "official",
        "trailer",
        "reaction",
        "reacts",
        "explained",
        "why",
        "how",
        "what",
    ]
)
_WORD = re.compile(r"[a-z0-9]+")


def topic_tokens(text: str) -> frozenset[str]:
    """The words that identify a topic: lowercased, punctuation gone, filler gone."""
    return frozenset(
        token for token in _WORD.findall(text.lower()) if len(token) > 1 and token not in _STOPWORDS
    )


@dataclass
class Cluster:
    """Signals that appear to be about one thing."""

    label: str
    tokens: set[str]
    signals: list[Signal] = field(default_factory=list)
    #: Videos looked up for the topic afterwards, by the finder. Kept apart
    #: from the signals because a search always finds *something*, and letting
    #: it count as a provider agreeing would make every row look corroborated.
    found: list[VideoHit] = field(default_factory=list)

    @property
    def sources(self) -> set[TrendSource]:
        return {signal.source for signal in self.signals}

    @property
    def videos(self) -> list[VideoHit]:
        from_signals = (hit for signal in self.signals for hit in signal.videos)
        return dedupe_videos([*from_signals, *self.found])

    @property
    def newest(self) -> datetime | None:
        seen = [signal.observed_at for signal in self.signals if signal.observed_at is not None]
        return max(seen) if seen else None

    @property
    def strength(self) -> float:
        return max((signal.strength for signal in self.signals), default=0.0)


def _same_topic(a: frozenset[str], b: frozenset[str]) -> bool:
    """Whether two token sets name one thing.

    Jaccard for the general case. Containment for the case the feeds actually
    produce — a two-word trending phrase inside a twelve-word post title — but
    only when the contained set has at least two words, because a single word
    contained in a title is how every story about the same person becomes one
    row.
    """
    if not a or not b:
        return False
    if a == b:
        return True
    shared = len(a & b)
    if shared == 0:
        return False
    if shared / len(a | b) >= 0.5:
        return True
    smaller = min(len(a), len(b))
    return smaller >= 2 and shared / smaller >= 0.75


def _prefer_label(signals: Sequence[Signal]) -> str:
    """The shortest way of saying what the cluster is about.

    A trending-search phrase over a search query over a post title, and the
    shortest within each: the label is a row heading, and a Reddit title is a
    sentence.
    """
    for source in (TrendSource.GOOGLE_TRENDS, TrendSource.YOUTUBE, TrendSource.REDDIT):
        candidates = [signal.topic for signal in signals if signal.source is source]
        if candidates:
            return min(candidates, key=len)[:80]
    return signals[0].topic[:80]


def cluster_signals(signals: Iterable[Signal]) -> list[Cluster]:
    """Group signals by topic, strongest first so a strong signal seeds its cluster."""
    ordered = sorted(signals, key=lambda signal: -signal.strength)
    clusters: list[Cluster] = []
    for signal in ordered:
        tokens = topic_tokens(signal.topic)
        if not tokens:
            continue
        home: Cluster | None = None
        for cluster in clusters:
            if _same_topic(frozenset(cluster.tokens), tokens):
                home = cluster
                break
        if home is None:
            clusters.append(Cluster(label=signal.topic[:80], tokens=set(tokens), signals=[signal]))
        else:
            home.signals.append(signal)
            home.tokens |= tokens
    for cluster in clusters:
        cluster.label = _prefer_label(cluster.signals)
    return clusters


def dedupe_videos(hits: Iterable[VideoHit]) -> list[VideoHit]:
    """One entry per video, keeping whatever each sighting knew.

    Two providers can surface the same video — a search result that Reddit also
    linked — and the merged record should know both the view count the search
    saw and that somebody chose to share it. `via` prefers the sharer, because
    that is the more meaningful of the two facts about it.
    """
    merged: dict[str, VideoHit] = {}
    for hit in hits:
        known = merged.get(hit.external_id)
        if known is None:
            merged[hit.external_id] = hit
            continue
        merged[hit.external_id] = VideoHit(
            url=known.url,
            external_id=known.external_id,
            title=known.title if known.via is TrendSource.YOUTUBE else hit.title,
            via=TrendSource.REDDIT if TrendSource.REDDIT in (known.via, hit.via) else known.via,
            channel=known.channel or hit.channel,
            duration_sec=known.duration_sec or hit.duration_sec,
            view_count=known.view_count or hit.view_count,
            uploaded_at=known.uploaded_at or hit.uploaded_at,
            thumbnail_url=known.thumbnail_url or hit.thumbnail_url,
            channel_followers=known.channel_followers or hit.channel_followers,
        )
    return list(merged.values())


# ── Scores ───────────────────────────────────────────────────────────────────


def score_video(
    hit: VideoHit,
    *,
    now: datetime,
    max_duration_sec: float,
) -> int:
    """How well one video would feed the pipeline, 0-100.

    Four parts: how fast it is being watched (40), how recent it is (20),
    whether its length is something the pipeline will accept at all (25), and
    how much it has been watched in total (15). Unknowns score in the middle,
    never at zero — a provider that knows less about a video must not make
    the video look worse.
    """
    velocity = _velocity(hit, now)
    if velocity is None:
        velocity_part = 15.0
    else:
        from clipforge.research.providers import strength_from_velocity

        velocity_part = 40.0 * strength_from_velocity(velocity)

    if hit.uploaded_at is None:
        recency_part = 8.0
    else:
        age_hours = max((now - hit.uploaded_at).total_seconds() / 3600.0, 0.0)
        if age_hours <= 24:
            recency_part = 20.0
        elif age_hours <= 72:
            recency_part = 14.0
        elif age_hours <= 168:
            recency_part = 8.0
        else:
            recency_part = 2.0

    duration = hit.duration_sec
    if duration is None:
        length_part = 12.0
    elif duration > max_duration_sec:
        length_part = 0.0
    elif duration >= 60:
        length_part = 25.0
    elif duration >= 20:
        length_part = 15.0
    else:
        length_part = 5.0

    views = hit.view_count or 0
    views_part = 15.0 * min(1.0, (len(str(views)) - 1) / 7.0) if views > 0 else 0.0

    return round(min(100.0, velocity_part + recency_part + length_part + views_part))


def _velocity(hit: VideoHit, now: datetime) -> float | None:
    if hit.view_count is None or hit.uploaded_at is None:
        return None
    hours = max((now - hit.uploaded_at).total_seconds() / 3600.0, 1.0)
    return hit.view_count / hours


def matched_interests(cluster: Cluster, interests: Sequence[str]) -> list[str]:
    """Which of the run's own topics this cluster is about.

    An interest matches when every one of its words is somewhere in the
    cluster — "premier league" matches a cluster that has both words across
    its signals — or when a provider was asked about it by name, which is what
    a YOUTUBE signal's topic records.
    """
    asked = {signal.topic for signal in cluster.signals if signal.source is TrendSource.YOUTUBE}
    matched: list[str] = []
    for interest in interests:
        tokens = topic_tokens(interest)
        if interest in asked or (tokens and tokens <= cluster.tokens):
            matched.append(interest)
    return matched


@dataclass(frozen=True)
class ScoredTrend:
    cluster: Cluster
    score: int
    matched: tuple[str, ...]
    videos: tuple[tuple[VideoHit, int], ...]


def score_trend(
    cluster: Cluster,
    *,
    now: datetime,
    lookback_hours: int,
    interests: Sequence[str],
    max_duration_sec: float,
) -> ScoredTrend:
    """The opportunity score, 0-100, with its parts fixed by weight.

    Agreement between providers is worth the most (30): one feed saying a thing
    is a feed; two saying it is news. Then how loudly the loudest one said it
    (25), how recently (15), whether there is any video at all (10) and how
    good the best one looks (10), and whether it is about something this
    channel actually covers (10).
    """
    sources = len(cluster.sources)
    corroboration = {1: 0.3, 2: 0.7}.get(sources, 1.0 if sources >= 3 else 0.0)

    newest = cluster.newest
    if newest is None:
        recency = 0.5
    else:
        age_hours = max((now - newest).total_seconds() / 3600.0, 0.0)
        recency = max(0.0, 1.0 - age_hours / max(lookback_hours, 1))

    scored_videos = sorted(
        (
            (hit, score_video(hit, now=now, max_duration_sec=max_duration_sec))
            for hit in cluster.videos
        ),
        key=lambda pair: -pair[1],
    )
    best_video = scored_videos[0][1] / 100.0 if scored_videos else 0.0
    matched = matched_interests(cluster, interests)

    total = (
        30.0 * corroboration
        + 25.0 * cluster.strength
        + 15.0 * recency
        + (10.0 if scored_videos else 0.0)
        + 10.0 * best_video
        + (10.0 if matched else 0.0)
    )
    return ScoredTrend(
        cluster=cluster,
        score=round(min(100.0, total)),
        matched=tuple(matched),
        videos=tuple(scored_videos),
    )


def rank_trends(scored: Iterable[ScoredTrend], *, limit: int) -> list[ScoredTrend]:
    """Best first. Ties go to the stronger signal, then the shorter label —
    both deterministic, so a re-run over the same feeds lists the same order."""
    ordered = sorted(
        scored,
        key=lambda trend: (-trend.score, -trend.cluster.strength, len(trend.cluster.label)),
    )
    return ordered[:limit]


def blend_rank(score: int, relevance: int | None, *, worth_clipping: bool) -> int:
    """Fold the model's opinion into the deterministic score for re-ranking.

    The score keeps most of the weight: the model is asked whether a topic
    belongs on this channel, not whether it is trending, and it should not be
    able to promote a dead topic on enthusiasm alone. A topic the model says
    is not worth clipping is pushed down, not removed — the person may know
    better, and the row still says why it sank.
    """
    blended = float(score) if relevance is None else 0.6 * score + 4.0 * relevance
    if not worth_clipping:
        blended *= 0.6
    return round(min(100.0, max(0.0, blended)))
