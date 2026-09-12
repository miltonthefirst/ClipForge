"""Text normalisation and word error rate.

Used to hold transcription quality to a measurable standard rather than an
impression. Phase 4's exit criterion is that WER against a committed fixture
stays below a threshold and the test fails if it regresses — which only means
anything if both sides are compared on the same terms.

**Normalisation is the whole design.** Whisper's punctuation and casing are
formatting choices, not transcription decisions: it writes "Barnaby Rudge," where
the reference has "Barnaby Rudge", and scoring that as an error would make the
threshold measure typography instead of accuracy. So both sides are lowercased,
stripped of punctuation, and whitespace-collapsed before comparison.

Numbers are the one genuinely ambiguous case and are left alone deliberately —
"1846" and "eighteen forty-six" really are different transcriptions, and quietly
equating them would hide a regression worth knowing about.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

__all__ = ["WerResult", "normalise", "tokenise", "word_error_rate"]

# Curly quotes and dashes vary between the published text and what a model emits,
# and neither is a transcription decision.
_PUNCTUATION = re.compile(r"[^\w\s']", flags=re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Reduce text to what was actually said, as far as that can be automated."""
    # NFKC folds typographic variants (curly quotes, ligatures) onto their plain
    # equivalents, so the two sides do not differ merely by which font produced
    # them.
    folded = unicodedata.normalize("NFKC", text).lower()
    # The suppression below is deliberate: these ARE the ambiguous characters,
    # and folding them is the entire point of the line.
    folded = folded.replace("’", "'").replace("‘", "'")  # noqa: RUF001
    folded = _PUNCTUATION.sub(" ", folded)
    # A trailing possessive apostrophe is punctuation; an internal one is part of
    # the word ("dickens's" stays one token, "dickens'" becomes "dickens").
    folded = re.sub(r"'(?!\w)", " ", folded)
    return _WHITESPACE.sub(" ", folded).strip()


def tokenise(text: str) -> list[str]:
    normalised = normalise(text)
    return normalised.split() if normalised else []


@dataclass(frozen=True)
class WerResult:
    """A word error rate, plus the counts that produced it."""

    substitutions: int
    deletions: int
    insertions: int
    reference_words: int

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    @property
    def rate(self) -> float:
        """Errors per reference word.

        Can exceed 1.0 when a hypothesis is longer than the reference — which is
        correct, and worth not clamping: a model that hallucinated three times the
        text should score worse than one that produced nothing.
        """
        if self.reference_words == 0:
            return 0.0 if self.errors == 0 else 1.0
        return self.errors / self.reference_words

    def __str__(self) -> str:
        return (
            f"WER {self.rate:.1%} "
            f"({self.substitutions}S {self.deletions}D {self.insertions}I "
            f"over {self.reference_words} words)"
        )


def word_error_rate(reference: str, hypothesis: str) -> WerResult:
    """Levenshtein distance over words, reported with its component counts.

    The counts matter more than the rate when a test fails: 12 insertions is a
    hallucination, 12 deletions is truncated audio, and 12 substitutions is a
    genuine accuracy regression. One number cannot tell those apart.
    """
    ref = tokenise(reference)
    hyp = tokenise(hypothesis)

    if not ref:
        return WerResult(0, 0, len(hyp), 0)

    # Standard edit-distance table, carrying the operation counts alongside the
    # cost so the breakdown survives the traceback-free formulation.
    previous: list[tuple[int, int, int, int]] = [(j, 0, 0, j) for j in range(len(hyp) + 1)]

    for i in range(1, len(ref) + 1):
        current: list[tuple[int, int, int, int]] = [(i, 0, i, 0)]
        for j in range(1, len(hyp) + 1):
            if ref[i - 1] == hyp[j - 1]:
                cost, subs, dels, ins = previous[j - 1]
                current.append((cost, subs, dels, ins))
                continue

            sub_cost, sub_s, sub_d, sub_i = previous[j - 1]
            del_cost, del_s, del_d, del_i = previous[j]
            ins_cost, ins_s, ins_d, ins_i = current[j - 1]

            best = min(sub_cost, del_cost, ins_cost)
            if best == sub_cost:
                current.append((best + 1, sub_s + 1, sub_d, sub_i))
            elif best == del_cost:
                current.append((best + 1, del_s, del_d + 1, del_i))
            else:
                current.append((best + 1, ins_s, ins_d, ins_i + 1))
        previous = current

    _, substitutions, deletions, insertions = previous[len(hyp)]
    return WerResult(
        substitutions=substitutions,
        deletions=deletions,
        insertions=insertions,
        reference_words=len(ref),
    )
