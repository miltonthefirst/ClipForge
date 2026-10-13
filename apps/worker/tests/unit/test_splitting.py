"""Splitting a clip into parts: the arithmetic, before anything is cut.

A live request, reproduced. "Split this into two, beginning to 35s then 35s to
the end" had no control to go to, and came back as one clip with its start
moved thirty seconds earlier. The parts are now their own thing, and what can
go wrong with them — a boundary a rounded duration puts past the end, a part too
short to be a clip, a path through the frame replayed from the wrong moment —
is all arithmetic, and all here.
"""

from __future__ import annotations

import pytest
from clipforge.cli import _parse_part
from clipforge.media.split import build_cut_args
from clipforge.stages.splitting import (
    MAX_PARTS,
    Part,
    SplitError,
    clock,
    part_music,
    part_title,
    part_voice,
    plan_split,
    slice_keyframes,
)
from clipforge_contracts import (
    AppliedMusic,
    AppliedVoice,
    MusicCaptions,
    MusicMode,
    PanKeyframe,
    SpeechMode,
    SplitRange,
)

pytestmark = pytest.mark.unit


def ranges(*pairs: tuple[float, float]) -> list[SplitRange]:
    # Built without validation, so a range the contract itself refuses (a start
    # before -30) still reaches the planner's own check, which is the one that
    # stands if the contract's bounds ever move.
    return [SplitRange.model_construct(start_sec=start, end_sec=end) for start, end in pairs]


# ── Planning ─────────────────────────────────────────────────────────────────


def test_the_request_that_started_this_is_two_parts_and_no_trim() -> None:
    plan = plan_split(ranges((0, 35), (35, 50.7)), clip_sec=50.7)

    assert [(p.start_sec, p.end_sec) for p in plan.parts] == [(0, 35), (35, 50.7)]
    assert [(p.index, p.count) for p in plan.parts] == [(1, 2), (2, 2)]
    assert (plan.start_delta_sec, plan.end_delta_sec) == (0.0, 0.0)
    assert plan.several


def test_parts_are_numbered_by_where_they_start_not_by_how_they_were_listed() -> None:
    plan = plan_split(ranges((35, 50.7), (0, 35)), clip_sec=50.7)

    assert [p.start_sec for p in plan.parts] == [0, 35]


def test_gaps_and_overlaps_are_both_allowed() -> None:
    """Leaving a dull middle out, or letting one moment end a part and open the
    next, are both things a reviewer legitimately asks for."""
    gap = plan_split(ranges((0, 20), (30, 50)), clip_sec=50)
    overlap = plan_split(ranges((0, 25), (20, 50)), clip_sec=50)

    assert len(gap.parts) == 2
    assert len(overlap.parts) == 2


def test_a_single_range_is_the_same_clip_made_shorter() -> None:
    plan = plan_split(ranges((5, 20)), clip_sec=50)

    assert not plan.several
    assert (plan.start_delta_sec, plan.end_delta_sec) == (0.0, 0.0)


def test_an_end_a_rounded_duration_put_past_the_clip_is_the_clip_s_end() -> None:
    """The note reader was told the clip's length and wrote it back rounded.
    Taken literally, 0.3 s past the end re-cuts the whole thing from the source."""
    plan = plan_split(ranges((0, 35), (35, 51)), clip_sec=50.7)

    assert plan.parts[-1].end_sec == pytest.approx(50.7)
    assert plan.end_delta_sec == 0.0


def test_a_start_a_hair_before_the_clip_is_the_clip_s_start() -> None:
    plan = plan_split(ranges((-0.4, 20), (20, 40)), clip_sec=40)

    assert plan.parts[0].start_sec == 0.0
    assert plan.start_delta_sec == 0.0


def test_a_part_reaching_outside_the_clip_widens_the_window_to_cover_it() -> None:
    plan = plan_split(ranges((-5, 20), (20, 60)), clip_sec=50)

    assert plan.start_delta_sec == -5
    assert plan.end_delta_sec == 10


def test_parts_inside_the_clip_never_shrink_the_window() -> None:
    """Shrinking it would re-cut from the source for no reason — and the source
    may be gone, when the rendered file alone would have done."""
    plan = plan_split(ranges((10, 20), (25, 30)), clip_sec=50)

    assert (plan.start_delta_sec, plan.end_delta_sec) == (0.0, 0.0)


@pytest.mark.parametrize(
    ("pairs", "says"),
    [
        (((0, 0.5), (0.5, 50)), "at least 1 second"),
        (((20, 10), (20, 50)), "at least 1 second"),
        (((-40, 10), (10, 50)), "before the clip does"),
        (((0, 20), (20, 90)), "past the end of the clip"),
    ],
)
def test_a_part_that_cannot_be_cut_is_refused_with_its_own_times(
    pairs: tuple[tuple[float, float], ...], says: str
) -> None:
    with pytest.raises(SplitError, match=says):
        plan_split(ranges(*pairs), clip_sec=50)


def test_more_parts_than_the_cap_are_refused() -> None:
    too_many = ranges(*[(i, i + 1.5) for i in range(MAX_PARTS + 1)])

    with pytest.raises(SplitError, match=f"at most {MAX_PARTS}"):
        plan_split(too_many, clip_sec=60)


def test_no_parts_is_not_a_plan() -> None:
    with pytest.raises(SplitError):
        plan_split([], clip_sec=60)


# ── What each part inherits ──────────────────────────────────────────────────


def test_a_part_replays_the_window_s_path_from_its_own_first_frame() -> None:
    path = [
        PanKeyframe(at_sec=0, x_pct=20),
        PanKeyframe(at_sec=10, x_pct=80),
        PanKeyframe(at_sec=20, x_pct=40),
    ]

    sliced = slice_keyframes(path, 5, 15)

    assert [(k.at_sec, k.x_pct) for k in sliced] == [(0.0, 50.0), (5.0, 80.0), (10.0, 60.0)]


def test_a_part_past_the_last_point_holds_where_the_window_stopped() -> None:
    sliced = slice_keyframes([PanKeyframe(at_sec=0, x_pct=30)], 20, 30)

    assert [k.x_pct for k in sliced] == [30, 30]


def test_a_clip_with_no_path_gives_its_parts_none() -> None:
    assert slice_keyframes([], 0, 10) == []


def test_each_part_says_where_it_sits_in_the_whole() -> None:
    part = Part(index=2, count=3, start_sec=20, end_sec=40)

    assert part_title("Dare Remachinda Ep 10", part) == "Dare Remachinda Ep 10 (Part 2/3)"
    assert part_title(None, part) is None


def test_a_long_title_is_shortened_to_make_room_rather_than_losing_the_part() -> None:
    titled = part_title("x" * 200, Part(index=1, count=2, start_sec=0, end_sec=10))

    assert titled is not None
    assert len(titled) <= 200
    assert titled.endswith("(Part 1/2)")


def test_a_part_does_not_claim_the_whole_narration() -> None:
    voice = AppliedVoice(
        mode=SpeechMode.REPLACE,
        voice="af_heart",
        language="en-us",
        engine="kokoro",
        spoken_text="the whole script",
        speech_duration_sec=48.0,
    )

    kept = part_voice(voice)

    assert kept is not None
    assert (kept.voice, kept.language) == ("af_heart", "en-us")
    assert kept.spoken_text is None
    assert kept.speech_duration_sec is None


def test_a_later_part_starts_later_in_the_track() -> None:
    music = AppliedMusic(
        mode=MusicMode.REPLACE,
        captions=MusicCaptions.KEEP,
        source="bed.wav",
        music_start_sec=12.0,
    )

    moved = part_music(music, 35.0)

    assert moved is not None
    assert moved.music_start_sec == 47.0
    assert part_music(None, 35.0) is None


@pytest.mark.parametrize(
    ("seconds", "reads"),
    [(0, "0:00"), (35, "0:35"), (50.7, "0:50.7"), (62.5, "1:02.5"), (-5, "-0:05")],
)
def test_times_read_the_way_a_player_shows_them(seconds: float, reads: str) -> None:
    assert clock(seconds) == reads


# ── The cut ──────────────────────────────────────────────────────────────────


def test_a_part_is_cut_exactly_and_re_encoded() -> None:
    """A stream copy can only start on a keyframe, which would put "35 seconds"
    up to two seconds early and repeat that stretch in the part before."""
    args = build_cut_args(source="in.mp4", destination="out.mp4", start_sec=35, end_sec=50.7)

    assert args[args.index("-ss") + 1] == "35.000"
    assert args.index("-ss") < args.index("-i"), "an input seek, which is exact on re-encode"
    assert args[args.index("-t") + 1] == "15.700"
    assert args[args.index("-c:v") + 1] == "libx264"
    assert "0:a:0?" in args, "a clip with no audio still has a picture worth cutting"


@pytest.mark.parametrize(
    ("typed", "reads"),
    [
        ("0-35", (0.0, 35.0)),
        ("0:35-0:50.7", (35.0, 50.7)),
        ("-5-20", (-5.0, 20.0)),
        (" 1:02.5 - 1:10 ", (62.5, 70.0)),
        ("35", None),
        ("abc", None),
    ],
)
def test_the_command_line_reads_a_part_the_way_the_form_does(
    typed: str, reads: tuple[float, float] | None
) -> None:
    assert _parse_part(typed) == reads
