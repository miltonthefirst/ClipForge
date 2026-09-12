"""Normalisation and word error rate.

These underpin Phase 4's accuracy criterion, so they have to be right in their
own terms first: a WER implementation that is subtly wrong makes the threshold
meaningless in either direction.
"""

from __future__ import annotations

import pytest
from clipforge.text import normalise, tokenise, word_error_rate

# ─────────────────────────────────────────────────────────────────────────────
# Normalisation — what counts as "the same words"
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_case_and_punctuation_are_not_transcription_errors() -> None:
    """Whisper's punctuation is a formatting choice. Scoring it would make the
    threshold measure typography instead of accuracy."""
    assert normalise("Barnaby Rudge, says:") == normalise("barnaby rudge says")


@pytest.mark.unit
def test_typographic_quotes_fold_onto_plain_ones() -> None:
    """The published text uses curly quotes; a model emits straight ones. Neither
    is a transcription decision."""
    assert normalise("Dickens’s note") == normalise("Dickens's note")  # noqa: RUF001


@pytest.mark.unit
def test_an_internal_apostrophe_is_part_of_the_word() -> None:
    assert tokenise("dickens's idea") == ["dickens's", "idea"]


@pytest.mark.unit
def test_a_trailing_apostrophe_is_punctuation() -> None:
    assert tokenise("williams' backwards") == ["williams", "backwards"]


@pytest.mark.unit
def test_whitespace_and_line_breaks_collapse() -> None:
    assert normalise("  once   upon\na midnight  ") == "once upon a midnight"


@pytest.mark.unit
def test_numbers_are_deliberately_not_equated_with_their_words() -> None:
    """'1846' and 'eighteen forty-six' really are different transcriptions.
    Quietly equating them would hide a regression worth knowing about."""
    assert normalise("1846") != normalise("eighteen forty six")


@pytest.mark.unit
def test_em_dashes_become_word_boundaries_not_joins() -> None:
    assert tokenise("says—by the way") == ["says", "by", "the", "way"]


# ─────────────────────────────────────────────────────────────────────────────
# Word error rate
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_identical_text_scores_zero() -> None:
    result = word_error_rate("the quick brown fox", "The quick brown fox.")
    assert result.rate == 0.0
    assert result.errors == 0


@pytest.mark.unit
def test_a_substitution_is_counted_as_one_error() -> None:
    result = word_error_rate("the quick brown fox", "the quick green fox")
    assert (result.substitutions, result.deletions, result.insertions) == (1, 0, 0)
    assert result.rate == pytest.approx(0.25)


@pytest.mark.unit
def test_a_deletion_is_counted_as_one_error() -> None:
    result = word_error_rate("the quick brown fox", "the quick fox")
    assert (result.substitutions, result.deletions, result.insertions) == (0, 1, 0)


@pytest.mark.unit
def test_an_insertion_is_counted_as_one_error() -> None:
    result = word_error_rate("the quick brown fox", "the very quick brown fox")
    assert (result.substitutions, result.deletions, result.insertions) == (0, 0, 1)


@pytest.mark.unit
def test_the_breakdown_distinguishes_failure_modes() -> None:
    """This is why the counts are reported and not just the rate: 12 insertions
    is a hallucination, 12 deletions is truncated audio, 12 substitutions is an
    accuracy regression. One number cannot tell those apart."""
    hallucinated = word_error_rate("a b c", "a b c d e f")
    truncated = word_error_rate("a b c d e f", "a b c")

    assert hallucinated.insertions == 3 and hallucinated.deletions == 0
    assert truncated.deletions == 3 and truncated.insertions == 0


@pytest.mark.unit
def test_an_empty_hypothesis_scores_total_failure() -> None:
    assert word_error_rate("a b c d", "").rate == 1.0


@pytest.mark.unit
def test_an_empty_reference_with_output_is_all_insertions() -> None:
    result = word_error_rate("", "unexpected words")
    assert result.insertions == 2
    assert result.rate == 1.0


@pytest.mark.unit
def test_both_empty_scores_zero() -> None:
    assert word_error_rate("", "").rate == 0.0


@pytest.mark.unit
def test_the_rate_is_not_clamped_at_one() -> None:
    """A model that hallucinated three times the text should score worse than one
    that produced nothing at all."""
    assert word_error_rate("a", "a b c d e").rate > 1.0


@pytest.mark.unit
def test_the_summary_names_the_component_counts() -> None:
    assert "1S" in str(word_error_rate("a b c", "a x c"))
