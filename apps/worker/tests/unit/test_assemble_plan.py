"""The parts of assembly that decide things, asserted without an encode.

The filtergraph most of all: it is the piece most likely to be wrong and the
one whose wrongness costs a two-minute encode to discover.
"""

from __future__ import annotations

import pytest
from clipforge.media.assemble import (
    build_concat_filter,
    clamp_window,
    segment_budget_sec,
    title_card_ass,
    wrap_title,
)
from clipforge.media.profiles import CaptionStyle
from clipforge_contracts import CompileTransition

pytestmark = pytest.mark.unit


def test_the_budget_shares_the_target_evenly_under_the_cap() -> None:
    assert segment_budget_sec(3, target_sec=60, max_segment_sec=20) == 20.0
    assert segment_budget_sec(6, target_sec=60, max_segment_sec=20) == 10.0
    assert segment_budget_sec(2, target_sec=60, max_segment_sec=20) == 20.0


def test_the_budget_never_drops_below_five_seconds() -> None:
    assert segment_budget_sec(12, target_sec=20, max_segment_sec=20) == 5.0


def test_a_given_window_is_trimmed_from_the_end_and_says_so() -> None:
    assert clamp_window(10.0, 20.0, max_sec=20.0) == (10.0, 20.0, False)
    assert clamp_window(10.0, 50.0, max_sec=20.0) == (10.0, 30.0, True)
    # A negative start is nobody's intention.
    assert clamp_window(-3.0, 5.0, max_sec=20.0) == (0.0, 5.0, False)


def test_titles_wrap_at_word_boundaries_and_stop_at_four_lines() -> None:
    assert wrap_title("Best goals of the week") == ["Best goals of the", "week"]
    long = wrap_title(
        "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen"
    )
    assert len(long) == 4
    assert long[-1].endswith("…")


def test_the_title_card_is_one_centred_dialogue_for_the_whole_card() -> None:
    document = title_card_ass("Goals & {braces}", style=CaptionStyle(), duration_sec=2.0)
    assert "PlayResX: 1080" in document
    assert "Style: Card,Arial," in document
    # Alignment 5 is the centre of the frame.
    assert ",5,60,60,0,1" in document
    assert "Dialogue: 0,0:00:00.00,0:00:02.00,Card" in document
    # Braces are escaped, or libass would read them as an override block.
    assert "\\{braces\\}" in document


def test_the_concat_filter_retimes_every_input_and_joins_in_order() -> None:
    graph = build_concat_filter([2.0, 12.5, 8.0], transition=CompileTransition.CUT)
    assert graph.startswith("[0:v]setpts=PTS-STARTPTS[v0];[0:a]asetpts=PTS-STARTPTS[a0];")
    assert graph.endswith("[v0][a0][v1][a1][v2][a2]concat=n=3:v=1:a=1[v][a]")
    assert "fade" not in graph


def test_fades_dip_to_black_at_both_ends_of_every_input() -> None:
    graph = build_concat_filter([2.0, 12.5], transition=CompileTransition.FADE)
    assert "[1:v]setpts=PTS-STARTPTS,fade=t=in:st=0:d=0.25,fade=t=out:st=12.250:d=0.25[v1]" in graph
    assert (
        "[1:a]asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.25,afade=t=out:st=12.250:d=0.25[a1]" in graph
    )


def test_a_segment_too_short_to_fade_is_left_alone() -> None:
    graph = build_concat_filter([0.4], transition=CompileTransition.FADE)
    assert "fade" not in graph


def test_nothing_to_join_is_refused_before_ffmpeg_sees_it() -> None:
    with pytest.raises(ValueError, match="nothing"):
        build_concat_filter([], transition=CompileTransition.CUT)
