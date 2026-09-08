"""Scoring, merging, filtering and ranking — the arithmetic half of selection.

Decision **D5**: the model returns sub-scores, Python computes the total. That
split makes the score auditable, removes LLM arithmetic error, and — the part
that actually pays off — lets the weighting change and every historical candidate
be re-ranked **without re-running inference**.

## Reconciling the two statements of the rubric

`docs/PLAN.md` states the rubric twice and the two look contradictory. The sub-score
maxima sum to 100 (hook 25, curiosity 20, standalone 20, emotion 15, pacing 10,
shareability 10), while Phase 5's formula is a weighted sum
(`0.25*hook + 0.20*curiosity + ...`), which would top out at 18.5.

They are the same thing. Normalising each sub-score by its own maximum and
weighting by the rubric proportions gives::

    100 * (0.25*(hook/25) + 0.20*(curiosity/20) + ...) == hook + curiosity + ...

So the default weights reproduce the plain rubric sum exactly, and a *changed*
weight is what re-ranks. Nothing here is a compromise between the two readings;
they coincide.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from clipforge_contracts import SubScores

__all__ = [
    "DEFAULT_WEIGHTS",
    "RUBRIC_MAXIMA",
    "ScoreWeights",
    "ScoredWindow",
    "iou",
    "merge_overlapping",
    "rank",
    "total_score",
]

# The rubric's per-dimension ceilings, from docs/PLAN.md. The schema enforces
# these too, so a model cannot return a hook of 40 and dominate the ranking.
RUBRIC_MAXIMA: dict[str, int] = {
    "hook": 25,
    "curiosity": 20,
    "standalone": 20,
    "emotion": 15,
    "pacing": 10,
    "shareability": 10,
}


@dataclass(frozen=True)
class ScoreWeights:
    """How much each dimension contributes. Defaults reproduce the plain rubric.

    Changing one of these re-ranks every candidate ever produced, with no
    inference — which is Phase 5's exit criterion 6, and the reason the model is
    not allowed to compute the total itself.
    """

    hook: float = 0.25
    curiosity: float = 0.20
    standalone: float = 0.20
    emotion: float = 0.15
    pacing: float = 0.10
    shareability: float = 0.10

    def as_mapping(self) -> dict[str, float]:
        return {
            "hook": self.hook,
            "curiosity": self.curiosity,
            "standalone": self.standalone,
            "emotion": self.emotion,
            "pacing": self.pacing,
            "shareability": self.shareability,
        }


DEFAULT_WEIGHTS = ScoreWeights()


def total_score(sub_scores: SubScores, weights: ScoreWeights = DEFAULT_WEIGHTS) -> int:
    """Combine sub-scores into a 0-100 total.

    Each dimension is normalised by its own maximum before weighting, so a
    dimension worth 25 points and one worth 10 contribute in proportion to their
    weight rather than to their scale.
    """
    values = sub_scores.model_dump()
    weighted = sum(
        weight * (float(values[name]) / RUBRIC_MAXIMA[name])
        for name, weight in weights.as_mapping().items()
    )
    # Clamped because a caller may supply weights that do not sum to 1, and a
    # total outside 0-100 would violate the contract's own bound.
    return max(0, min(100, round(100 * weighted)))


@dataclass(frozen=True)
class ScoredWindow:
    """One candidate window, before boundary snapping.

    Kept separate from the `Candidate` contract type because these are still
    provisional: they get merged, snapped, filtered and ranked, and only what
    survives becomes a document.
    """

    start_sec: float
    end_sec: float
    sub_scores: SubScores
    hook: str
    reason: str
    total: int

    @property
    def duration_sec(self) -> float:
        return self.end_sec - self.start_sec


def iou(a: ScoredWindow, b: ScoredWindow) -> float:
    """Intersection over union of two time ranges.

    The natural measure here: two windows proposing "the same moment" overlap
    heavily regardless of which is longer, whereas raw overlap seconds would call
    a 10-second overlap significant for two 15-second clips and insignificant for
    two 75-second ones.
    """
    intersection = min(a.end_sec, b.end_sec) - max(a.start_sec, b.start_sec)
    if intersection <= 0:
        return 0.0
    union = (a.end_sec - a.start_sec) + (b.end_sec - b.start_sec) - intersection
    return intersection / union if union > 0 else 0.0


def merge_overlapping(
    windows: Iterable[ScoredWindow], *, threshold: float = 0.5
) -> list[ScoredWindow]:
    """Collapse windows that describe the same moment, keeping the best.

    Overlapping windows are inevitable and intentional: the map step uses a 120s
    window on a 30s stride, so any given moment is seen four times. Without this,
    the top five candidates would routinely be five views of one joke.

    Highest total first, so the survivor of each cluster is the best-scoring
    description of that moment rather than whichever happened to come first.
    """
    ordered = sorted(windows, key=lambda w: (-w.total, w.start_sec))
    kept: list[ScoredWindow] = []
    for window in ordered:
        if any(iou(window, existing) > threshold for existing in kept):
            continue
        kept.append(window)
    return kept


def rank(
    windows: Sequence[ScoredWindow],
    *,
    weights: ScoreWeights = DEFAULT_WEIGHTS,
    min_duration_sec: float = 15.0,
    max_duration_sec: float = 75.0,
    score_floor: int = 0,
    limit: int = 5,
) -> list[ScoredWindow]:
    """Rescore, filter and take the best.

    Rescoring here rather than trusting the stored total is what makes exit
    criterion 6 true: change the weights, call this again, get a different order
    out of the same candidates with no model involved.
    """
    rescored = [replace(w, total=total_score(w.sub_scores, weights)) for w in windows]

    eligible = [
        w
        for w in rescored
        if min_duration_sec <= w.duration_sec <= max_duration_sec and w.total >= score_floor
    ]

    # Ties broken by start time so the order is deterministic — a golden test
    # over a fixed transcript is worthless if equal scores shuffle.
    eligible.sort(key=lambda w: (-w.total, w.start_sec))
    return eligible[:limit]
