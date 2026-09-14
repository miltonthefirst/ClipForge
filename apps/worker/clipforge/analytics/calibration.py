"""Did the predicted score predict anything? The arithmetic that answers it.

Pure and numpy-only. Everything here runs with no account, no network and no
published video, which is what lets the phase be proven before the one thing it
cannot fake has happened.

**Rank statistics throughout.** Views are violently skewed: one clip that gets
picked up does ten times the traffic of the rest put together, and a Pearson
correlation over that sample reports the outlier as the finding. Spearman asks
the question actually being asked — *did the clips we scored highly do better
than the clips we scored poorly* — and one runaway success moves it by one rank.

**The thresholds below are judgement, and are stated rather than buried.** There
is no n at which a correlation becomes true. What there is, is an n below which
a confidence interval spans so much of the range that reporting the point
estimate would be misleading, and these constants are where that line was drawn.
Moving them is a legitimate decision; moving them *after seeing the result* is
not, which is why they are module constants and not parameters.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from clipforge_contracts import (
    CalibrationCorrelation,
    CalibrationMethod,
    ScoreWeights,
)

from clipforge.analytics.cohorts import ClipFacts

__all__ = [
    "MIN_N_FOR_CONCLUSION",
    "MIN_N_FOR_FIT",
    "Outcome",
    "correlate",
    "fisher_interval",
    "fit_weights",
    "pearson",
    "ranks",
    "spearman",
]

#: Below this, every correlation is reported with an explicit refusal to
#: conclude. Chosen because at n=20 a 95% Fisher interval around r=0.4 still
#: comfortably contains zero, so the honest summary of any result is "we cannot
#: tell yet" — which is the summary the report should print.
MIN_N_FOR_CONCLUSION = 20

#: Below this, no weights are fitted at all. Six predictors want considerably
#: more than six observations before a fit describes anything but this
#: particular sample, and a rubric re-weighted from 20 videos would be a
#: superstition with a decimal point on it.
MIN_N_FOR_FIT = 40

#: Ridge penalty for the fit. Small, and present because the six rubric
#: dimensions are correlated with one another — a model rating a moment highly
#: for "hook" rarely rates it poorly for "curiosity" — and unpenalised least
#: squares over collinear predictors produces large offsetting weights that
#: swing wildly on one more data point.
RIDGE_LAMBDA = 1.0

_DIMENSIONS = ("hook", "curiosity", "standalone", "emotion", "pacing", "shareability")

#: The rubric's per-dimension ceilings, mirrored from analysis.ranking so that a
#: fit works in the same normalised space the scorer does.
_MAXIMA = {
    "hook": 25,
    "curiosity": 20,
    "standalone": 20,
    "emotion": 15,
    "pacing": 10,
    "shareability": 10,
}


@dataclass(frozen=True)
class Outcome:
    """One realised measure, and how to pull it off a :class:`ClipFacts`."""

    key: str
    label: str

    def value(self, facts: ClipFacts) -> float | None:
        if self.key == "views":
            return float(facts.views)
        if self.key == "viewPercentage":
            return facts.view_percentage
        if self.key == "retentionAtHalf":
            return facts.retention_at_half
        return None


OUTCOMES = (
    Outcome("views", "views"),
    Outcome("viewPercentage", "average percentage viewed"),
    Outcome("retentionAtHalf", "audience remaining at the midpoint"),
)


def ranks(values: Sequence[float]) -> list[float]:
    """Ranks, averaging ties.

    Ties must be averaged rather than broken arbitrarily. Predicted scores are
    integers on a 0-100 scale over a few dozen clips, so ties are common rather
    than exotic, and breaking them by position would encode the order the clips
    happened to be fetched in as though it were signal.
    """
    n = len(values)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: values[i])
    out = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = shared
        i = j + 1
    return out


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Linear correlation, or None when it is undefined."""
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    a = np.asarray(xs, dtype=float)
    b = np.asarray(ys, dtype=float)
    # A constant series has zero variance, and the correlation is genuinely
    # undefined rather than zero — every clip scoring 80 tells you nothing about
    # whether 80 was a good score to give.
    if float(a.std()) == 0.0 or float(b.std()) == 0.0:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Rank correlation. The default, for the reason in the module docstring."""
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    return pearson(ranks(xs), ranks(ys))


def fisher_interval(
    r: float, n: int, *, method: CalibrationMethod = CalibrationMethod.SPEARMAN
) -> tuple[float, float] | None:
    """A 95% interval for a correlation, via the Fisher z transform.

    Spearman's standard error carries the usual 1.06 inflation factor: ranking
    discards information, and an interval that ignored that would be narrower
    than the estimate deserves — overstating confidence in exactly the direction
    this phase must not overstate it.
    """
    if n < 4 or not -1.0 < r < 1.0:
        return None
    z = math.atanh(r)
    se = 1.0 / math.sqrt(n - 3)
    if method == CalibrationMethod.SPEARMAN:
        se *= 1.06
    delta = 1.96 * se
    return math.tanh(z - delta), math.tanh(z + delta)


def _interpretation(r: float, n: int, interval: tuple[float, float] | None) -> str:
    """Plain words, decided by the interval and the sample — never by r alone."""
    if n < MIN_N_FOR_CONCLUSION:
        return (
            f"n={n} is below the {MIN_N_FOR_CONCLUSION} this report will draw a "
            f"conclusion from. The coefficient is shown for completeness and "
            f"should not be acted on."
        )
    if interval is None:
        return f"n={n}, but no interval could be computed, so no conclusion is drawn."
    low, high = interval
    if low <= 0.0 <= high:
        return (
            f"The 95% interval [{low:.2f}, {high:.2f}] contains zero, so this is "
            f"consistent with the score predicting nothing at all."
        )
    direction = "higher" if r > 0 else "lower"
    strength = "weak" if abs(r) < 0.3 else "moderate" if abs(r) < 0.6 else "strong"
    return (
        f"A {strength} relationship: clips scored higher did measurably "
        f"{direction} on this outcome, 95% interval [{low:.2f}, {high:.2f}]."
    )


def correlate(
    facts: Sequence[ClipFacts],
    outcome: Outcome,
    *,
    method: CalibrationMethod = CalibrationMethod.SPEARMAN,
) -> CalibrationCorrelation:
    """Correlate the predicted score against one realised outcome.

    Pairs where either side is missing are dropped rather than zero-filled, and
    ``n`` reports what actually survived — so a report over 40 clips of which 12
    had a retention curve says 12 against retention and 40 against views, rather
    than quietly implying 40 of each.
    """
    pairs = [
        (float(f.predicted_score), value)
        for f in facts
        if f.predicted_score is not None
        for value in (outcome.value(f),)
        if value is not None
    ]
    n = len(pairs)
    if n < 2:
        return CalibrationCorrelation(
            outcome=outcome.key,
            method=method,
            n=n,
            coefficient=0.0,
            interpretation=f"n={n}: nothing to correlate.",
        )

    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    r = spearman(xs, ys) if method == CalibrationMethod.SPEARMAN else pearson(xs, ys)
    if r is None:
        return CalibrationCorrelation(
            outcome=outcome.key,
            method=method,
            n=n,
            coefficient=0.0,
            interpretation=(
                "Undefined: one of the two series has no variation, so there is "
                "no relationship to measure."
            ),
        )

    interval = fisher_interval(r, n, method=method)
    return CalibrationCorrelation(
        outcome=outcome.key,
        method=method,
        n=n,
        coefficient=round(r, 4),
        ci_low=round(interval[0], 4) if interval else None,
        ci_high=round(interval[1], 4) if interval else None,
        interpretation=_interpretation(r, n, interval),
    )


def fit_weights(
    facts: Sequence[ClipFacts],
    outcome: Outcome,
    *,
    minimum_n: int = MIN_N_FOR_FIT,
) -> ScoreWeights | None:
    """Fit rubric weights against a realised outcome, or refuse to.

    Returns None below ``minimum_n``, and that refusal is the expected answer
    for as long as this project publishes a handful of clips a week. Refusing is
    not a failure mode here; it is the deliverable, because the alternative is a
    set of six decimals that look like knowledge.

    The fit works on **ranks of the outcome**, not the outcome itself, for the
    same reason the correlations do. Sub-scores are normalised by their own
    maxima first so the fitted numbers live in the same space
    ``analysis.ranking`` already uses, and the result is clipped at zero and
    renormalised to sum to 1 — a negative weight would mean the rubric should
    *penalise* a dimension it was written to reward, which is a finding big
    enough that it should be read off the correlations by a person rather than
    silently encoded in config.
    """
    usable = [
        (f.sub_scores, value)
        for f in facts
        if f.sub_scores is not None
        for value in (outcome.value(f),)
        if value is not None
    ]
    if len(usable) < minimum_n:
        return None

    matrix = np.array(
        [[float(getattr(sub, dim)) / _MAXIMA[dim] for dim in _DIMENSIONS] for sub, _ in usable],
        dtype=float,
    )
    target = np.asarray(ranks([value for _, value in usable]), dtype=float)
    target = (target - target.mean()) / (target.std() or 1.0)

    # Centre the predictors so the ridge penalty is not fighting the intercept,
    # which is not a weight and should not be shrunk towards zero.
    centred = matrix - matrix.mean(axis=0)
    gram = centred.T @ centred + RIDGE_LAMBDA * np.eye(len(_DIMENSIONS))
    coefficients = np.linalg.solve(gram, centred.T @ target)

    positive = np.clip(coefficients, 0.0, None)
    total = float(positive.sum())
    if total <= 0.0:
        # Every dimension fitted zero or negative. That is a real result and it
        # is not a set of weights; it says the rubric as written has no positive
        # relationship with this outcome at all.
        return None

    normalised = positive / total
    return ScoreWeights(
        **{dim: round(float(w), 4) for dim, w in zip(_DIMENSIONS, normalised, strict=True)}
    )
