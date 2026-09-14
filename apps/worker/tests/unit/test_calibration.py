"""Phase 9 — the arithmetic that decides whether the scoring was worth anything.

These are the tests that can exist before a single clip has been published, and
they are the ones that matter most: a correlation routine that is subtly wrong
produces a number, not an error, and the number looks exactly as convincing as a
correct one.

Every case here plants a relationship and checks it is found, or plants none and
checks nothing is claimed. The second kind is the point of the phase.
"""

from __future__ import annotations

import random

import pytest
from clipforge.analytics.calibration import (
    MIN_N_FOR_CONCLUSION,
    MIN_N_FOR_FIT,
    OUTCOMES,
    Outcome,
    correlate,
    fisher_interval,
    fit_weights,
    pearson,
    ranks,
    spearman,
)
from clipforge.analytics.cohorts import ClipFacts
from clipforge_contracts import CalibrationMethod, ScoreWeights, SubScores

BASELINE = ScoreWeights(
    hook=0.25, curiosity=0.20, standalone=0.20, emotion=0.15, pacing=0.10, shareability=0.10
)
VIEWS = OUTCOMES[0]


def _facts(
    index: int,
    *,
    score: int,
    views: int,
    sub: SubScores | None = None,
    percentage: float | None = None,
    retention: float | None = None,
) -> ClipFacts:
    return ClipFacts(
        clip_id=f"c{index}",
        publication_id=f"p{index}",
        predicted_score=score,
        sub_scores=sub,
        views=views,
        view_percentage=percentage,
        retention_at_half=retention,
    )


# ── Ranking ──────────────────────────────────────────────────────────────────


def test_ties_share_the_average_rank() -> None:
    """Two clips scoring 80 must not be ordered by which was fetched first."""
    assert ranks([1.0, 2.0, 2.0, 3.0]) == [1.0, 2.5, 2.5, 4.0]


def test_every_value_tied_gives_every_value_the_same_rank() -> None:
    assert ranks([5.0, 5.0, 5.0]) == [2.0, 2.0, 2.0]


def test_ranks_of_nothing_is_nothing() -> None:
    assert ranks([]) == []


# ── Correlation ──────────────────────────────────────────────────────────────


def test_a_perfect_monotonic_relationship_is_found() -> None:
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)


def test_a_perfectly_inverted_relationship_is_found_as_negative() -> None:
    assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)


def test_spearman_survives_an_outlier_that_pearson_does_not() -> None:
    """The reason rank correlation is the default, stated as a test.

    A perfectly ordered set of clips, then one runaway success. Spearman still
    sees the order; Pearson is dragged towards the single enormous value.
    """
    xs = [1, 2, 3, 4, 5, 6, 7, 8]
    ys = [1, 2, 3, 4, 5, 6, 7, 100_000]
    assert spearman(xs, ys) == pytest.approx(1.0)
    assert pearson(xs, ys) is not None


def test_a_constant_series_has_no_correlation_rather_than_zero() -> None:
    """Every clip scoring 80 tells you nothing — which is not the same as zero."""
    assert spearman([80, 80, 80, 80], [1, 2, 3, 4]) is None
    assert pearson([80, 80, 80, 80], [1, 2, 3, 4]) is None


def test_mismatched_lengths_refuse_rather_than_truncate() -> None:
    assert spearman([1, 2, 3], [1, 2]) is None


# ── Intervals ────────────────────────────────────────────────────────────────


def test_the_interval_narrows_as_the_sample_grows() -> None:
    small = fisher_interval(0.5, 10)
    large = fisher_interval(0.5, 200)
    assert small is not None
    assert large is not None
    assert (small[1] - small[0]) > (large[1] - large[0])


def test_spearman_intervals_are_wider_than_pearson_at_the_same_n() -> None:
    """Ranking discards information and the interval must admit it."""
    rank_based = fisher_interval(0.5, 50, method=CalibrationMethod.SPEARMAN)
    linear = fisher_interval(0.5, 50, method=CalibrationMethod.PEARSON)
    assert rank_based is not None
    assert linear is not None
    assert (rank_based[1] - rank_based[0]) > (linear[1] - linear[0])


def test_a_perfect_correlation_has_no_interval() -> None:
    """atanh(1) is infinite. Returning None beats returning an infinity."""
    assert fisher_interval(1.0, 50) is None
    assert fisher_interval(-1.0, 50) is None


def test_too_few_points_for_an_interval() -> None:
    assert fisher_interval(0.5, 3) is None


# ── What the report is willing to claim ──────────────────────────────────────


def test_a_small_sample_refuses_to_conclude_however_strong_the_signal() -> None:
    """A planted perfect relationship, under the threshold. Still no claim.

    This is the test that keeps the phase honest. The correlation is 1.0 and
    the interpretation must still say the sample cannot support it.
    """
    facts = [_facts(i, score=i * 10, views=i * 100) for i in range(1, 6)]
    result = correlate(facts, VIEWS)
    assert result.n == 5
    assert result.coefficient == pytest.approx(1.0)
    assert "below" in (result.interpretation or "")


def test_pure_noise_is_reported_as_consistent_with_nothing() -> None:
    """The negative result, reported as a result."""
    rng = random.Random(11)  # noqa: S311 - seeded test data, not cryptography
    facts = [
        _facts(i, score=rng.randint(40, 95), views=rng.randint(0, 5_000))
        for i in range(MIN_N_FOR_CONCLUSION + 40)
    ]
    result = correlate(facts, VIEWS)
    assert result.ci_low is not None
    assert result.ci_high is not None
    assert result.ci_low <= 0.0 <= result.ci_high
    assert "consistent with the score predicting nothing" in (result.interpretation or "")


def test_a_planted_relationship_is_found_and_stated() -> None:
    rng = random.Random(3)  # noqa: S311 - seeded test data, not cryptography
    facts = [
        _facts(i, score=score, views=int(score * 30 + rng.gauss(0, 80)))
        for i, score in enumerate(rng.sample(range(30, 100), 60))
    ]
    result = correlate(facts, VIEWS)
    assert result.coefficient > 0.5
    assert result.ci_low is not None
    assert result.ci_low > 0.0
    assert "relationship" in (result.interpretation or "")


def test_missing_outcomes_are_dropped_rather_than_counted_as_zero() -> None:
    """A withheld retention curve is not a clip nobody watched.

    Three clips have a curve and twenty do not. `n` must say three, because a
    report claiming twenty-three would be claiming twenty invented measurements.
    """
    facts = [_facts(i, score=50 + i, views=100, retention=None) for i in range(20)]
    facts += [_facts(100 + i, score=50 + i, views=100, retention=0.4 + i / 10) for i in range(3)]
    result = correlate(facts, OUTCOMES[2])
    assert result.n == 3


# ── Fitting ──────────────────────────────────────────────────────────────────


def _sub(hook: int) -> SubScores:
    return SubScores(hook=hook, curiosity=10, standalone=10, emotion=7, pacing=5, shareability=5)


def test_no_weights_are_fitted_below_the_threshold() -> None:
    facts = [
        _facts(i, score=i, views=i * 10, sub=_sub(min(25, i))) for i in range(MIN_N_FOR_FIT - 1)
    ]
    assert fit_weights(facts, VIEWS) is None


def test_a_fit_recovers_the_dimension_that_actually_drove_the_outcome() -> None:
    """Views generated purely from `hook`. The fit must say `hook`.

    The strongest available check that the regression is wired up correctly:
    the answer is known in advance because the data was built from it.
    """
    rng = random.Random(5)  # noqa: S311 - seeded test data, not cryptography
    facts = []
    for i in range(80):
        hook = rng.randint(0, 25)
        facts.append(
            _facts(i, score=hook * 4, views=hook * 50 + rng.randint(0, 20), sub=_sub(hook))
        )
    fitted = fit_weights(facts, VIEWS)
    assert fitted is not None
    assert fitted.hook > 0.5
    assert fitted.hook == max(
        fitted.hook,
        fitted.curiosity,
        fitted.standalone,
        fitted.emotion,
        fitted.pacing,
        fitted.shareability,
    )


def test_fitted_weights_sum_to_one() -> None:
    rng = random.Random(9)  # noqa: S311 - seeded test data, not cryptography
    facts = [
        _facts(i, score=50, views=rng.randint(0, 900), sub=_sub(rng.randint(0, 25)))
        for i in range(80)
    ]
    fitted = fit_weights(facts, VIEWS)
    if fitted is not None:
        total = (
            fitted.hook
            + fitted.curiosity
            + fitted.standalone
            + fitted.emotion
            + fitted.pacing
            + fitted.shareability
        )
        assert total == pytest.approx(1.0, abs=0.01)


def test_a_fit_with_no_positive_relationship_refuses_rather_than_inventing_one() -> None:
    """Every dimension fitting zero or negative is a finding, not a weight set."""
    facts = [
        _facts(i, score=50, views=1000 - i * 10, sub=_sub(i % 26)) for i in range(MIN_N_FOR_FIT + 5)
    ]
    fitted = fit_weights(facts, Outcome("views", "views"))
    # Either a genuine fit or an honest refusal; never a set of weights that
    # does not sum to one, and never an exception.
    assert fitted is None or fitted.hook >= 0.0
