"""Boundary snapping: turning an LLM's approximate window into an exact cut.

Decision **D4**. Language models are unreliable at precise timestamps and
excellent at judging where something interesting is. Word-level Whisper output
plus a silence map is exact and has no judgement at all. So the model proposes a
region and this module decides the actual frame — splitting the judgement from
the arithmetic.

The failure this prevents is the single most obvious tell of an automated clip:
starting mid-word. A clip that begins on "...ecause I think" reads as broken in a
way that a slightly-too-early start never does.

Two asymmetries are deliberate:

**The start window is wider backwards than forwards.** Starting a little early is
almost free — a beat of silence before someone speaks is normal editing — while
starting late clips the first word, which is exactly the failure being avoided.

**The end window only extends forwards.** Ending early truncates the payoff;
running slightly long is unnoticeable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from clipforge_contracts import Transcript, TranscriptWord

__all__ = [
    "DEFAULT_TOLERANCES",
    "SnapTolerances",
    "SnappedBoundary",
    "silences_from_speech",
    "snap_end",
    "snap_start",
    "words_in",
]


@dataclass(frozen=True)
class SnapTolerances:
    """How far a boundary may move, and what counts as a silence."""

    min_silence_sec: float = 0.20
    start_back_sec: float = 3.0
    start_forward_sec: float = 1.5
    end_forward_sec: float = 3.0


@dataclass(frozen=True)
class SnappedBoundary:
    """Where a boundary landed, and whether it found a silence to land on."""

    time_sec: float
    snapped_to_silence: bool


def silences_from_speech(
    speech_spans: Sequence[tuple[float, float]],
    *,
    duration_sec: float,
    min_silence_sec: float = 0.20,
) -> list[tuple[float, float]]:
    """Invert a speech map into the silences between and around it.

    Silence is stored as the complement of speech rather than detected
    separately, so the two can never disagree about the same moment.
    """
    if duration_sec <= 0:
        return []

    ordered = sorted(speech_spans)
    silences: list[tuple[float, float]] = []
    cursor = 0.0

    for start, end in ordered:
        if start - cursor >= min_silence_sec:
            silences.append((cursor, start))
        cursor = max(cursor, end)

    if duration_sec - cursor >= min_silence_sec:
        silences.append((cursor, duration_sec))

    return silences


def words_in(transcript: Transcript, start_sec: float, end_sec: float) -> list[TranscriptWord]:
    """Every word that overlaps the range at all.

    Overlap rather than containment: a word straddling the boundary is exactly
    the one that must not be cut through, so it has to be visible here.
    """
    return [
        word
        for segment in transcript.segments
        for word in (segment.words or [])
        if word.end_sec > start_sec and word.start_sec < end_sec
    ]


def _silence_midpoints(
    silences: Sequence[tuple[float, float]], low: float, high: float
) -> list[float]:
    """Candidate cut points inside a search range.

    A cut lands in the *middle* of a silence rather than at its edge, so a small
    timing error in either direction still falls in the quiet part.
    """
    points: list[float] = []
    for start, end in silences:
        midpoint = (start + end) / 2
        if low <= midpoint <= high:
            points.append(midpoint)
        # A long silence may extend past the search range while still offering a
        # usable point inside it.
        elif start < low < end:
            points.append(low)
        elif start < high < end:
            points.append(high)
    return points


def _push_out_of_words(
    time_sec: float, words: Sequence[TranscriptWord], *, direction: str
) -> float:
    """Move a boundary out of any word it landed strictly inside.

    The silence map and the word timings come from different passes over the
    same audio and can disagree by milliseconds, so a midpoint that looks like
    silence can still fall a hair inside a word. Rather than trusting the two to
    agree, the invariant is enforced here directly — "never cut through a word"
    is the guarantee, and it should not depend on two data sources lining up.

    A start boundary moves to the word's start (earlier, inaudible); an end
    boundary moves to the word's end (later, unnoticeable).
    """
    for word in words:
        if word.start_sec < time_sec < word.end_sec:
            return word.start_sec if direction == "before" else word.end_sec
    return time_sec


def snap_start(
    proposed_sec: float,
    *,
    first_word: TranscriptWord | None,
    silences: Sequence[tuple[float, float]],
    tolerances: SnapTolerances | None = None,
    source_start_sec: float = 0.0,
    words: Sequence[TranscriptWord] = (),
) -> SnappedBoundary:
    """Move a proposed start onto a silence, without clipping the first word."""
    tolerances = tolerances or DEFAULT_TOLERANCES
    anchor = first_word.start_sec if first_word else proposed_sec
    low = max(source_start_sec, anchor - tolerances.start_back_sec)
    # Never past the first word: that is the whole point.
    high = min(anchor, anchor + tolerances.start_forward_sec)

    if high < low:
        landed = max(source_start_sec, anchor)
        return SnappedBoundary(
            _push_out_of_words(landed, words, direction="before"), snapped_to_silence=False
        )

    points = _silence_midpoints(silences, low, high)
    if not points:
        # No silence to land on. Sit just before the first word rather than on
        # it — a hair early is inaudible, a hair late clips the word.
        fallback = max(source_start_sec, anchor - 0.05)
        return SnappedBoundary(
            _push_out_of_words(fallback, words, direction="before"), snapped_to_silence=False
        )

    # The latest usable silence: closest to the speech, so the clip carries the
    # least dead air while still starting cleanly.
    landed = _push_out_of_words(max(points), words, direction="before")
    return SnappedBoundary(max(landed, source_start_sec), snapped_to_silence=True)


def snap_end(
    proposed_sec: float,
    *,
    last_word: TranscriptWord | None,
    silences: Sequence[tuple[float, float]],
    tolerances: SnapTolerances | None = None,
    source_end_sec: float,
    words: Sequence[TranscriptWord] = (),
) -> SnappedBoundary:
    """Move a proposed end onto a silence, without cutting the last word short."""
    tolerances = tolerances or DEFAULT_TOLERANCES
    anchor = last_word.end_sec if last_word else proposed_sec
    low = anchor
    high = min(source_end_sec, anchor + tolerances.end_forward_sec)

    if high < low:
        landed = min(source_end_sec, anchor)
        return SnappedBoundary(
            _push_out_of_words(landed, words, direction="after"), snapped_to_silence=False
        )

    points = _silence_midpoints(silences, low, high)
    if not points:
        fallback = min(source_end_sec, anchor + 0.05)
        return SnappedBoundary(
            _push_out_of_words(fallback, words, direction="after"), snapped_to_silence=False
        )

    # The earliest usable silence, for the same reason in reverse: end promptly
    # after the last word rather than trailing into the next sentence.
    landed = _push_out_of_words(min(points), words, direction="after")
    return SnappedBoundary(min(landed, source_end_sec), snapped_to_silence=True)


DEFAULT_TOLERANCES = SnapTolerances()
