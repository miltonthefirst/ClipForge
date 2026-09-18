"""What a trend provider hands back, and the port it hands it through.

A *signal* is one provider's evidence that a topic is moving, in that provider's
own terms: a trending search with an approximate volume, a post near the top of
a subreddit, a video being watched faster than its age explains. A signal may
carry the videos it found along the way — a Reddit post that links YouTube is
already a video, not merely a topic — and most do not.

The provider port exists so that the scorer is not a YouTube-search scorer with
extras. docs/PLAN.md §8 asks for exactly this shape, because the ranked list a
person promotes from is the same signal the synthesis track's PLAN stage will
later consume, and a scorer welded to one search API would have to be rewritten
for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from clipforge_contracts import TrendSource

__all__ = [
    "ResearchRequest",
    "Signal",
    "TrendProvider",
    "VideoFinder",
    "VideoHit",
]


@dataclass(frozen=True)
class ResearchRequest:
    """What one run is looking for. Every provider gets the same one."""

    topics: tuple[str, ...]
    region: str
    lookback_hours: int
    videos_per_topic: int
    subreddits: tuple[str, ...]
    now: datetime


@dataclass(frozen=True)
class VideoHit:
    """A video a provider found, with whatever it learned about it.

    Every field but the first three is optional because every provider knows a
    different subset: a search result knows the view count and not the upload
    time, a Reddit link knows neither. The scorer treats an unknown as neutral
    rather than as zero, so a provider that knows less does not rank last.
    """

    url: str
    external_id: str
    title: str
    via: TrendSource
    channel: str | None = None
    duration_sec: float | None = None
    view_count: int | None = None
    uploaded_at: datetime | None = None
    thumbnail_url: str | None = None
    channel_followers: int | None = None


@dataclass(frozen=True)
class Signal:
    """One provider's word that a topic is moving."""

    source: TrendSource
    topic: str
    #: 0-1, on the provider's own scale normalised in the provider. Comparable
    #: across providers only loosely, which is why the scorer uses the maximum
    #: and the count rather than a sum.
    strength: float
    detail: str | None = None
    url: str | None = None
    observed_at: datetime | None = None
    videos: tuple[VideoHit, ...] = field(default_factory=tuple)


class TrendProvider(Protocol):
    """One place to ask what is moving."""

    source: TrendSource

    def fetch(self, request: ResearchRequest) -> list[Signal]:
        """Ask once. May raise; the stage records the failure and carries on."""
        ...


class VideoFinder(Protocol):
    """Find videos for a topic that arrived without any.

    A trending search knows nothing about video. Something has to turn "brewers
    vs orioles" into a list of things the pipeline could actually cut, and that
    something is a YouTube search — which is a provider in its own right and is
    also this.
    """

    def find_videos(
        self, topic: str, *, limit: int, request: ResearchRequest
    ) -> list[VideoHit]: ...

    def enrich(self, hit: VideoHit) -> VideoHit:
        """Look up what a sighting did not know — views, upload time. May raise."""
        ...
