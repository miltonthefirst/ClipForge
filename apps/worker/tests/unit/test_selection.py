"""Clip selection: windowing, scoring, merging, snapping, ranking.

Phase 5, exit criteria 1, 3, 4 and 6. Everything here runs with no GPU and no
network, because decisions D4 and D5 deliberately keep every step except the
model call itself a pure function — the model judges, Python does the arithmetic.

The property tests over generated transcripts (criterion 3) matter most: "no
boundary falls inside a word" is the kind of invariant that holds on every
example you think of and fails on the one you did not.
"""

from __future__ import annotations

import itertools
import random
from datetime import UTC, datetime

import pytest
from clipforge.analysis.boundaries import (
    silences_from_speech,
    snap_end,
    snap_start,
    words_in,
)
from clipforge.analysis.ranking import (
    DEFAULT_WEIGHTS,
    RUBRIC_MAXIMA,
    ScoredWindow,
    ScoreWeights,
    iou,
    merge_overlapping,
    rank,
    total_score,
)
from clipforge.analysis.windows import WindowSpec, build_windows, render_window
from clipforge.stages.analyze import select_candidates
from clipforge_contracts import (
    LlmClipProposal,
    LlmClipResponse,
    SubScores,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
)


def scores(**overrides: int) -> SubScores:
    base = {
        "hook": 20,
        "curiosity": 16,
        "standalone": 16,
        "emotion": 12,
        "pacing": 8,
        "shareability": 8,
    }
    return SubScores.model_validate({**base, **overrides})


def window(
    start: float,
    end: float,
    *,
    sub_scores: SubScores | None = None,
    hook: str = "a hook",
    reason: str = "a reason",
    total: int | None = None,
) -> ScoredWindow:
    sub = sub_scores if sub_scores is not None else scores()
    return ScoredWindow(
        start_sec=start,
        end_sec=end,
        sub_scores=sub,
        hook=hook,
        reason=reason,
        total=total if total is not None else total_score(sub),
    )


def transcript_from(words: list[tuple[str, float, float]], duration: float) -> Transcript:
    """One segment per five words, so segment structure is exercised too."""
    segments: list[TranscriptSegment] = []
    for index in range(0, len(words), 5):
        chunk = words[index : index + 5]
        segments.append(
            TranscriptSegment(
                index=len(segments),
                text=" ".join(w[0] for w in chunk),
                start_sec=chunk[0][1],
                end_sec=chunk[-1][2],
                words=[TranscriptWord(text=t, start_sec=s, end_sec=e) for t, s, e in chunk],
            )
        )
    return Transcript(
        source_id="src",
        model_version="test",
        language="en",
        duration_sec=duration,
        segments=segments,
        created_at=datetime(2026, 9, 8, tzinfo=UTC),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scoring — and exit criterion 6
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_the_default_weights_reproduce_the_plain_rubric_sum() -> None:
    """The plan states the rubric twice — maxima summing to 100, and a weighted
    formula. They coincide when the weights are the rubric proportions, which is
    what makes both statements true rather than one of them a compromise."""
    sub = scores()
    plain = sum(sub.model_dump().values())
    assert total_score(sub, DEFAULT_WEIGHTS) == plain


@pytest.mark.unit
def test_a_perfect_score_is_one_hundred() -> None:
    assert total_score(SubScores.model_validate(RUBRIC_MAXIMA)) == 100


@pytest.mark.unit
def test_a_zero_score_is_zero() -> None:
    assert total_score(scores(**dict.fromkeys(RUBRIC_MAXIMA, 0))) == 0


@pytest.mark.unit
def test_each_dimension_contributes_in_proportion_to_its_weight() -> None:
    """A dimension worth 25 points and one worth 10 must contribute by weight,
    not by scale — which is why sub-scores are normalised before weighting."""
    hook_only = scores(**{**dict.fromkeys(RUBRIC_MAXIMA, 0), "hook": 25})
    pacing_only = scores(**{**dict.fromkeys(RUBRIC_MAXIMA, 0), "pacing": 10})

    assert total_score(hook_only) == 25
    assert total_score(pacing_only) == 10


@pytest.mark.unit
def test_reweighting_reranks_without_touching_the_model() -> None:
    """Phase 5, exit criterion 6. This is the entire reason D5 forbids the model
    from computing its own total."""
    hooky = window(0, 30, sub_scores=scores(hook=25, shareability=0))
    shareable = window(60, 90, sub_scores=scores(hook=0, shareability=10))

    default_order = [w.start_sec for w in rank([hooky, shareable])]
    shareability_first = ScoreWeights(hook=0.0, shareability=1.0)
    reweighted = [w.start_sec for w in rank([hooky, shareable], weights=shareability_first)]

    assert default_order == [0, 60]
    assert reweighted == [60, 0]


@pytest.mark.unit
def test_a_total_cannot_escape_the_contract_bound() -> None:
    """Weights are configurable, so a caller can supply a set that does not sum
    to 1 — the contract still says 0-100."""
    absurd = ScoreWeights(hook=10.0, curiosity=10.0, standalone=10.0)
    assert 0 <= total_score(scores(), absurd) <= 100


# ─────────────────────────────────────────────────────────────────────────────
# Windowing
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_windows_overlap_so_every_moment_is_seen_several_times() -> None:
    """A moment that only reads as interesting in context still gets found."""
    words = [(f"w{i}", i * 1.0, i * 1.0 + 0.9) for i in range(300)]
    windows = build_windows(transcript_from(words, 300.0), WindowSpec(120.0, 30.0))

    assert len(windows) > 1
    covering_moment_150 = [w for w in windows if w.start_sec <= 150 <= w.end_sec]
    assert len(covering_moment_150) >= 3


@pytest.mark.unit
def test_a_sentence_straddling_an_edge_appears_in_both_windows() -> None:
    """The model must never be shown half a sentence and asked whether it is
    self-contained."""
    words = [(f"w{i}", i * 1.0, i * 1.0 + 0.9) for i in range(200)]
    windows = build_windows(transcript_from(words, 200.0), WindowSpec(120.0, 30.0))

    # Consecutive windows must share segments. Otherwise a sentence sitting at an
    # edge would be shown to exactly one window, with half its context missing.
    assert len(windows) >= 2
    for earlier, later in itertools.pairwise(windows):
        shared = {s.index for s in earlier.segments} & {s.index for s in later.segments}
        assert shared, f"windows {earlier.index} and {later.index} share no segments"


@pytest.mark.unit
def test_a_transcript_shorter_than_one_window_yields_exactly_one() -> None:
    words = [(f"w{i}", i * 1.0, i * 1.0 + 0.9) for i in range(20)]
    assert len(build_windows(transcript_from(words, 20.0), WindowSpec(120.0, 30.0))) == 1


@pytest.mark.unit
def test_an_empty_transcript_yields_no_windows() -> None:
    empty = Transcript(
        source_id="src",
        model_version="test",
        segments=[],
        created_at=datetime(2026, 9, 8, tzinfo=UTC),
    )
    assert build_windows(empty) == []


@pytest.mark.unit
def test_a_rendered_window_states_absolute_timestamps() -> None:
    """Window-relative text with absolute answers expected is a reliable way to
    get boundaries wrong by exactly the window offset."""
    words = [(f"w{i}", 100.0 + i, 100.0 + i + 0.9) for i in range(10)]
    windows = build_windows(transcript_from(words, 200.0), WindowSpec(120.0, 30.0))
    assert "[100.0-" in render_window(windows[0])


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 4 — merging
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_iou_is_one_for_identical_ranges() -> None:
    assert iou(window(0, 30), window(0, 30)) == pytest.approx(1.0)


@pytest.mark.unit
def test_iou_is_zero_for_disjoint_ranges() -> None:
    assert iou(window(0, 30), window(40, 70)) == 0.0


@pytest.mark.unit
def test_iou_scales_with_clip_length_where_raw_overlap_would_not() -> None:
    """Ten seconds of overlap is significant for two 15-second clips and
    insignificant for two 75-second ones."""
    short_pair = iou(window(0, 15), window(5, 20))
    long_pair = iou(window(0, 75), window(65, 140))
    assert short_pair > long_pair


@pytest.mark.unit
def test_merging_keeps_the_best_description_of_a_moment() -> None:
    """Four views of one joke should become one candidate, and it should be the
    best-scoring one — not whichever came first."""
    weak = window(10, 40, sub_scores=scores(hook=5), total=40)
    strong = window(12, 42, sub_scores=scores(hook=25), total=90)

    kept = merge_overlapping([weak, strong], threshold=0.5)

    assert len(kept) == 1
    assert kept[0].total == 90


@pytest.mark.unit
def test_no_two_survivors_overlap_beyond_the_threshold() -> None:
    """Phase 5, exit criterion 4."""
    candidates = [window(i * 3.0, i * 3.0 + 30) for i in range(20)]
    kept = merge_overlapping(candidates, threshold=0.5)

    for i, a in enumerate(kept):
        for b in kept[i + 1 :]:
            assert iou(a, b) <= 0.5


@pytest.mark.unit
def test_distinct_moments_survive_merging() -> None:
    kept = merge_overlapping([window(0, 30), window(100, 130), window(200, 230)])
    assert len(kept) == 3


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 3 — boundary snapping
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_silences_are_the_complement_of_speech() -> None:
    """Stored as the complement rather than detected separately, so the two can
    never disagree about the same moment."""
    silences = silences_from_speech([(2.0, 5.0), (8.0, 12.0)], duration_sec=15.0)
    assert silences == [(0.0, 2.0), (5.0, 8.0), (12.0, 15.0)]


@pytest.mark.unit
def test_a_gap_shorter_than_the_minimum_is_not_a_silence() -> None:
    """Cutting on a 50ms gap between words produces an audible clip, not a clean
    one."""
    silences = silences_from_speech(
        [(0.0, 5.0), (5.05, 10.0)], duration_sec=10.0, min_silence_sec=0.2
    )
    assert silences == []


@pytest.mark.unit
def test_a_start_never_lands_after_the_first_word() -> None:
    word = TranscriptWord(text="because", start_sec=10.0, end_sec=10.5)
    result = snap_start(12.0, first_word=word, silences=[(5.0, 9.9)], source_start_sec=0.0)
    assert result.time_sec <= 10.0


@pytest.mark.unit
def test_a_start_prefers_the_silence_closest_to_the_speech() -> None:
    """Carry the least dead air while still starting cleanly."""
    word = TranscriptWord(text="because", start_sec=10.0, end_sec=10.5)
    result = snap_start(
        10.0, first_word=word, silences=[(7.0, 7.5), (9.0, 9.8)], source_start_sec=0.0
    )
    assert result.snapped_to_silence
    assert 9.0 <= result.time_sec <= 9.8


@pytest.mark.unit
def test_a_start_with_no_usable_silence_sits_just_before_the_word() -> None:
    """A hair early is inaudible; a hair late clips the word, which is the whole
    failure being avoided."""
    word = TranscriptWord(text="because", start_sec=10.0, end_sec=10.5)
    result = snap_start(10.0, first_word=word, silences=[], source_start_sec=0.0)
    assert not result.snapped_to_silence
    assert result.time_sec < 10.0


@pytest.mark.unit
def test_an_end_never_lands_before_the_last_word_finishes() -> None:
    word = TranscriptWord(text="finished", start_sec=40.0, end_sec=40.8)
    result = snap_end(40.2, last_word=word, silences=[(41.0, 42.0)], source_end_sec=60.0)
    assert result.time_sec >= 40.8


@pytest.mark.unit
def test_an_end_prefers_the_earliest_silence_after_the_last_word() -> None:
    """End promptly rather than trailing into the next sentence."""
    word = TranscriptWord(text="finished", start_sec=40.0, end_sec=40.8)
    result = snap_end(
        41.0, last_word=word, silences=[(41.0, 41.4), (42.5, 43.0)], source_end_sec=60.0
    )
    assert result.snapped_to_silence
    assert 41.0 <= result.time_sec <= 41.4


@pytest.mark.unit
def test_boundaries_are_clamped_to_the_source() -> None:
    first = TranscriptWord(text="start", start_sec=0.2, end_sec=0.6)
    last = TranscriptWord(text="end", start_sec=59.0, end_sec=59.8)

    start = snap_start(0.0, first_word=first, silences=[], source_start_sec=0.0)
    end = snap_end(60.0, last_word=last, silences=[], source_end_sec=60.0)

    assert start.time_sec >= 0.0
    assert end.time_sec <= 60.0


@pytest.mark.unit
def test_words_in_a_range_include_ones_that_straddle_it() -> None:
    """A word straddling the boundary is precisely the one that must not be cut
    through, so it has to be visible to the snapper."""
    words = [("hello", 9.5, 10.4), ("world", 10.5, 11.0)]
    found = words_in(transcript_from(words, 20.0), 10.0, 10.6)
    assert [w.text for w in found] == ["hello", "world"]


@pytest.mark.unit
@pytest.mark.parametrize("seed", range(25))
def test_property_no_boundary_ever_falls_inside_a_word(seed: int) -> None:
    """Phase 5, exit criterion 3, over generated transcripts.

    'No boundary falls inside a word' is exactly the kind of invariant that holds
    on every example you think of and fails on the one you did not.
    """
    rng = random.Random(seed)  # noqa: S311 - test data, not cryptography
    words: list[tuple[str, float, float]] = []
    cursor = rng.uniform(0.0, 2.0)
    for i in range(120):
        length = rng.uniform(0.15, 0.7)
        words.append((f"w{i}", cursor, cursor + length))
        cursor += length + rng.choice([0.02, 0.05, 0.3, 0.9])
    duration = cursor + 2.0

    transcript = transcript_from(words, duration)
    speech = tuple((s.start_sec, s.end_sec) for s in transcript.segments)
    silences = silences_from_speech(speech, duration_sec=duration)

    proposed_start = rng.uniform(0, duration * 0.6)
    proposed_end = proposed_start + rng.uniform(15, 60)
    inside = words_in(transcript, proposed_start, proposed_end)
    if not inside:
        pytest.skip("no words in the proposed range")

    # The stage passes the surrounding words so the snapper can enforce its own
    # invariant rather than inferring it from a silence map that was produced by
    # a different pass and can disagree by milliseconds.
    everything = words_in(transcript, 0.0, duration)
    start = snap_start(proposed_start, first_word=inside[0], silences=silences, words=everything)
    end = snap_end(
        proposed_end,
        last_word=inside[-1],
        silences=silences,
        source_end_sec=duration,
        words=everything,
    )

    for _, word_start, word_end in words:
        assert not (word_start < start.time_sec < word_end), "start fell inside a word"
        assert not (word_start < end.time_sec < word_end), "end fell inside a word"


# ─────────────────────────────────────────────────────────────────────────────
# Ranking and filtering
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_clips_outside_the_duration_bounds_are_dropped() -> None:
    kept = rank([window(0, 5), window(10, 40), window(100, 300)])
    assert [w.start_sec for w in kept] == [10]


@pytest.mark.unit
def test_the_score_floor_is_enforced() -> None:
    weak = window(0, 30, sub_scores=scores(**dict.fromkeys(RUBRIC_MAXIMA, 1)))
    strong = window(60, 90, sub_scores=SubScores.model_validate(RUBRIC_MAXIMA))
    kept = rank([weak, strong], score_floor=50)
    assert [w.start_sec for w in kept] == [60]


@pytest.mark.unit
def test_only_the_top_n_are_emitted() -> None:
    assert len(rank([window(i * 100.0, i * 100.0 + 30) for i in range(10)], limit=5)) == 5


@pytest.mark.unit
def test_ties_break_deterministically_by_start_time() -> None:
    """A golden test over a fixed transcript is worthless if equal scores
    shuffle between runs."""
    windows = [window(90, 120), window(30, 60), window(60, 90)]
    assert [w.start_sec for w in rank(windows)] == [30, 60, 90]


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 1 — the whole algorithm, against a scripted model
# ─────────────────────────────────────────────────────────────────────────────


def _scripted(proposals: dict[int, list[LlmClipProposal]]) -> object:
    def propose(window_obj: object) -> LlmClipResponse:
        return LlmClipResponse(clips=proposals.get(window_obj.index, []))  # type: ignore[attr-defined]

    return propose


@pytest.mark.unit
def test_the_pipeline_is_deterministic_over_a_fixed_transcript() -> None:
    """Phase 5, exit criterion 1. With a fixed model response, the same
    transcript must produce byte-identical candidates every run."""
    words = [(f"w{i}", i * 0.5, i * 0.5 + 0.4) for i in range(400)]
    transcript = transcript_from(words, 200.0)
    speech = tuple((s.start_sec, s.end_sec) for s in transcript.segments)

    proposals = {
        0: [
            LlmClipProposal(
                start_sec=10.0,
                end_sec=45.0,
                sub_scores=scores(hook=24),
                hook="a strong opening",
                reason="clear payoff",
            )
        ],
        2: [
            LlmClipProposal(
                start_sec=70.0,
                end_sec=110.0,
                sub_scores=scores(hook=10),
                hook="a weaker opening",
                reason="meanders",
            )
        ],
    }

    first = select_candidates(transcript, speech, propose=_scripted(proposals))
    second = select_candidates(transcript, speech, propose=_scripted(proposals))

    assert [(w.start_sec, w.end_sec, w.total) for w in first] == [
        (w.start_sec, w.end_sec, w.total) for w in second
    ]
    assert len(first) == 2
    assert first[0].total > first[1].total


@pytest.mark.unit
def test_a_clip_proposed_outside_its_window_is_discarded() -> None:
    """A model that ignores its window bounds proposes someone else's sentence.
    Cheaper to drop than to render."""
    words = [(f"w{i}", i * 0.5, i * 0.5 + 0.4) for i in range(400)]
    transcript = transcript_from(words, 200.0)
    speech = tuple((s.start_sec, s.end_sec) for s in transcript.segments)

    escaping = {
        0: [
            LlmClipProposal(
                start_sec=180.0,
                end_sec=195.0,
                sub_scores=scores(),
                hook="not in this window",
                reason="the model wandered",
            )
        ]
    }
    assert select_candidates(transcript, speech, propose=_scripted(escaping)) == []


@pytest.mark.unit
def test_an_inverted_proposal_is_discarded() -> None:
    words = [(f"w{i}", i * 0.5, i * 0.5 + 0.4) for i in range(200)]
    transcript = transcript_from(words, 100.0)
    speech = tuple((s.start_sec, s.end_sec) for s in transcript.segments)

    inverted = {
        0: [
            LlmClipProposal(
                start_sec=50.0,
                end_sec=20.0,
                sub_scores=scores(),
                hook="backwards",
                reason="end before start",
            )
        ]
    }
    assert select_candidates(transcript, speech, propose=_scripted(inverted)) == []


@pytest.mark.unit
def test_a_model_that_finds_nothing_produces_nothing() -> None:
    """Returning an empty list is the correct answer far more often than not, and
    the pipeline must not manufacture candidates to fill a quota."""
    words = [(f"w{i}", i * 0.5, i * 0.5 + 0.4) for i in range(200)]
    transcript = transcript_from(words, 100.0)
    speech = tuple((s.start_sec, s.end_sec) for s in transcript.segments)

    assert select_candidates(transcript, speech, propose=_scripted({})) == []
