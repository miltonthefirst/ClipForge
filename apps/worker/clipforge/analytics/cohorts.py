"""Bucketing published clips by the dimensions the calibration breaks down by.

Pure, and deliberately fed by a flat :class:`ClipFacts` rather than by the
documents themselves. Joining a clip to its candidate, its publication, its
render profile and its metrics is genuinely fiddly; doing arithmetic on the
result is not. Keeping the two apart means every rule in this module is testable
with a literal, and the fiddly half happens once, in the poller.

**The buckets are coarser than the data.** With tens of published clips, an
hour-of-day breakdown has twenty-four buckets and one clip in most of them, which
produces a table that looks like findings and is noise. Every dimension here is
therefore banded, and every bucket carries its ``n`` so that a mean over two
videos can be seen for what it is.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from statistics import fmean
from typing import assert_never

from clipforge_contracts import CohortKind, CohortStat, SubScores

__all__ = [
    "ClipFacts",
    "bucket_for",
    "hook_type",
    "retention_at",
    "summarise",
]

#: Fewer than this in a bucket and no mean is reported for it. The bucket still
#: appears, carrying its count: "we have two of these" is information, and
#: hiding the row would misrepresent the shape of the sample.
MIN_BUCKET_N = 3

_QUESTION_WORDS = (
    "who",
    "what",
    "when",
    "where",
    "why",
    "how",
    "which",
    "is",
    "are",
    "can",
    "did",
    "does",
    "do",
)
# "first" and "last" are deliberately absent. They read as superlatives in
# isolation and almost never are in practice: "breaks the first line", "the last
# man", "the first half" are all ordinary positional language, and football
# commentary is full of them. Including them put a plain description of a pass
# into the SUPERLATIVE bucket, which is the kind of error that does not announce
# itself — it just makes a breakdown quietly wrong.
_SUPERLATIVES = (
    "best",
    "worst",
    "most",
    "least",
    "only",
    "greatest",
    "biggest",
    "fastest",
    "never",
    "always",
    "ever",
    "perfect",
    "incredible",
    "unbelievable",
)
_NEGATIONS = ("not", "no", "never", "without", "nobody", "nothing", "cannot")


def hook_type(hook: str | None) -> str:
    """Classify a hook line into one of a handful of shapes.

    Heuristic and English-only, which is a real limitation rather than a
    simplification: Phase 8b made clips that are narrated in Spanish and French,
    and their hooks land in ``STATEMENT`` regardless of what shape they are. The
    report says so where it prints this breakdown, because a dimension that
    quietly means "English clips, plus everything else in one pile" would
    otherwise read as a finding about hooks.

    Order is significant. A hook can be several of these at once — *"Why is this
    the best goal ever?"* is a question, a superlative and arguably a statement —
    and a clip must land in exactly one bucket or the counts stop summing.
    Questions win because the question mark is the least ambiguous signal
    available.
    """
    if not hook or not hook.strip():
        return "NONE"
    text = hook.strip()
    lowered = text.lower()
    words = re.findall(r"[\w']+", lowered)

    if text.endswith("?"):
        return "QUESTION"
    if words and words[0] in _QUESTION_WORDS:
        return "QUESTION"
    if (text[0], text[-1]) in (('"', '"'), ("'", "'"), ("“", "”")):
        return "QUOTE"
    if any(word in _SUPERLATIVES for word in words):
        return "SUPERLATIVE"
    if re.search(r"\d", text):
        return "NUMBER"
    if any(word in _NEGATIONS for word in words):
        return "NEGATION"
    return "STATEMENT"


def _duration_bucket(seconds: float | None) -> str | None:
    if seconds is None or seconds <= 0:
        return None
    if seconds < 15:
        return "<15s"
    if seconds < 30:
        return "15-30s"
    if seconds < 45:
        return "30-45s"
    if seconds <= 60:
        return "45-60s"
    return ">60s"


def _score_band(total: int | None) -> str | None:
    if total is None:
        return None
    if total < 60:
        return "<60"
    if total < 70:
        return "60-69"
    if total < 80:
        return "70-79"
    if total < 90:
        return "80-89"
    return "90+"


def _posting_band(published_at: datetime | None) -> str | None:
    """A four-hour band, in UTC, and labelled as UTC.

    Posting time only means anything relative to an audience's waking hours, and
    this project has no idea where its audience is. Reporting UTC and saying so
    is honest; silently converting to the operator's local timezone would make
    the breakdown look more meaningful than it is and would change retroactively
    the first time they travelled.
    """
    if published_at is None:
        return None
    # Converted, not assumed. The label says UTC, and a naive or differently
    # offset datetime would land in a band the label then misnames — which is
    # worse than no breakdown, because it looks like a finding about timing.
    moment = (
        published_at.astimezone(UTC) if published_at.tzinfo else published_at.replace(tzinfo=UTC)
    )
    start = (moment.hour // 4) * 4
    return f"{start:02d}-{start + 4:02d} UTC"


def _topic(tags: Sequence[str]) -> str | None:
    """The clip's first tag, which Phase 8e grounds in the actual material."""
    for tag in tags:
        cleaned = tag.strip().lower()
        if cleaned:
            return cleaned
    return None


@dataclass(frozen=True)
class ClipFacts:
    """Everything one published clip contributes, already joined and flattened."""

    clip_id: str
    publication_id: str
    predicted_score: int | None = None
    sub_scores: SubScores | None = None
    hook: str | None = None
    duration_sec: float | None = None
    tags: tuple[str, ...] = ()
    published_at: datetime | None = None
    render_profile: str | None = None
    caption_style: str | None = None
    views: int = 0
    view_percentage: float | None = None
    retention_at_half: float | None = None


def bucket_for(kind: CohortKind, facts: ClipFacts) -> str | None:
    """Which bucket of ``kind`` this clip belongs to, or None if unknowable.

    None is a real answer and is not the same as a bucket named "unknown": a clip
    whose duration was never recorded is absent from the duration breakdown
    rather than forming a cohort of clips-with-no-duration, which would be a
    cohort about a bug rather than about content.
    """
    if kind == CohortKind.HOOK_TYPE:
        bucket = hook_type(facts.hook)
        return None if bucket == "NONE" else bucket
    if kind == CohortKind.DURATION_BUCKET:
        return _duration_bucket(facts.duration_sec)
    if kind == CohortKind.SCORE_BAND:
        return _score_band(facts.predicted_score)
    if kind == CohortKind.TOPIC:
        return _topic(facts.tags)
    if kind == CohortKind.POSTING_HOUR:
        return _posting_band(facts.published_at)
    if kind == CohortKind.CAPTION_STYLE:
        return facts.caption_style or None
    if kind == CohortKind.RENDER_PROFILE:
        return facts.render_profile or None
    # Not a fallback: `assert_never` makes adding a CohortKind without giving it
    # a bucket a type error where the enum grows. An earlier version let
    # RENDER_PROFILE be the catch-all and claimed in a comment that mypy proved
    # the chain exhaustive — it did not, and a new dimension would have been
    # silently bucketed by render profile.
    assert_never(kind)


def retention_at(curve: Sequence[tuple[float, float]], point: float = 0.5) -> float | None:
    """Audience still watching at ``point`` through the clip, interpolated.

    Interpolated rather than nearest-sampled because YouTube's curve resolution
    varies with video length, so "the sample closest to the middle" is a
    different distance from the middle for a 20-second clip than for a 60-second
    one, and comparing those across clips is comparing two different questions.
    """
    if not curve:
        return None
    ordered = sorted(curve)
    if point <= ordered[0][0]:
        return ordered[0][1]
    if point >= ordered[-1][0]:
        return ordered[-1][1]
    for (x0, y0), (x1, y1) in pairwise(ordered):
        if x0 <= point <= x1:
            if x1 == x0:
                return y0
            weight = (point - x0) / (x1 - x0)
            return y0 + weight * (y1 - y0)
    return None


def _mean_or_none(values: list[float]) -> float | None:
    """A mean, withheld unless enough clips actually contributed a value.

    The threshold is checked against **how many values were averaged**, not how
    many clips are in the bucket. Those differ constantly: YouTube withholds a
    retention curve below its privacy threshold, so a bucket of five clips may
    hold one retention number — and reporting that single figure next to `n=5`
    would be precisely the misrepresentation this threshold exists to prevent,
    dressed up as five clips' worth of evidence.
    """
    if len(values) < MIN_BUCKET_N:
        return None
    return round(fmean(values), 4)


def summarise(facts: Iterable[ClipFacts]) -> list[CohortStat]:
    """Every breakdown, as a flat list ordered by kind then by bucket.

    A flat list rather than a nested mapping because that is what the contract
    carries and what the dashboard iterates; nesting here would be un-nested at
    both ends.
    """
    collected = list(facts)
    stats: list[CohortStat] = []

    for kind in CohortKind:
        grouped: dict[str, list[ClipFacts]] = defaultdict(list)
        for item in collected:
            bucket = bucket_for(kind, item)
            if bucket is not None:
                grouped[bucket].append(item)

        for bucket in sorted(grouped):
            members = grouped[bucket]
            n = len(members)
            stats.append(
                CohortStat(
                    kind=kind,
                    bucket=bucket,
                    n=n,
                    mean_predicted_score=_mean_or_none(
                        [
                            float(m.predicted_score)
                            for m in members
                            if m.predicted_score is not None
                        ],
                    ),
                    mean_views=_mean_or_none([float(m.views) for m in members]),
                    mean_view_percentage=_mean_or_none(
                        [m.view_percentage for m in members if m.view_percentage is not None]
                    ),
                    mean_retention_at_half=_mean_or_none(
                        [m.retention_at_half for m in members if m.retention_at_half is not None]
                    ),
                )
            )
    return stats
