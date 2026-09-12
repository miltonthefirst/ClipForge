"""Hiding a fixed mark: the arithmetic and the filtergraph.

Everything here asserts on *strings* and *numbers*, for the same reason
`test_framing` does: a blur box in the wrong place renders perfectly and hides
the crowd instead of the logo, so the expression is the artefact worth pinning.
Whether those expressions do what they claim is
`tests/integration/test_obscure_renders.py`, against real ffmpeg, and whether
detection finds a real logo is a question only real footage can answer.
"""

from __future__ import annotations

import numpy as np
import pytest
from clipforge.media.ffprobe import MediaInfo
from clipforge.media.framing import build_video_chain
from clipforge.media.obscure import (
    DEFAULT_STRENGTH,
    Detection,
    _join_bands,
    _rect,
    build_obscure_chains,
    describe_region,
    detect_static_regions,
    merge_regions,
    overlaps,
)
from clipforge.media.profiles import RenderProfile
from clipforge_contracts import (
    Framing,
    FramingMode,
    ObscureFound,
    ObscureMethod,
    ObscureOptions,
    ObscureRegion,
)

pytestmark = pytest.mark.unit

# A CANAL+ bug on a 1920x1080 broadcast frame, as detection measured it.
BUG = ObscureRegion(x_pct=83.8, y_pct=4.4, w_pct=13.8, h_pct=8.9)


def media(width: int = 1920, height: int = 1080) -> MediaInfo:
    return MediaInfo(
        duration_sec=18.0,
        width=width,
        height=height,
        has_video=True,
        has_audio=True,
        format_name="mov,mp4",
        size_bytes=1_000_000,
    )


def profile() -> RenderProfile:
    return RenderProfile()


def chains(regions: list[ObscureRegion], **kwargs: object) -> str:
    return ";".join(build_obscure_chains(regions, width=1920, height=1080, **kwargs))  # type: ignore[arg-type]


# ── Placing the rectangle ────────────────────────────────────────────────────


def test_nothing_to_hide_costs_nothing() -> None:
    """The common case must produce the filtergraph it produced before."""
    assert build_obscure_chains([], width=1920, height=1080) == []


def test_percentages_resolve_to_pixels() -> None:
    assert _rect(BUG, 1920, 1080, inset=0) == (1608, 48, 264, 96)


def test_every_edge_lands_on_an_even_pixel() -> None:
    """Odd offsets shift chroma against luma on a subsampled format.

    Invisible in the graph and visible in the output, which is the combination
    worth a test.
    """
    odd = ObscureRegion(x_pct=3.1, y_pct=7.3, w_pct=11.7, h_pct=5.3)
    for value in _rect(odd, 1920, 1080, inset=0) or ():
        assert value % 2 == 0


def test_a_rectangle_is_clamped_into_the_frame() -> None:
    huge = ObscureRegion(x_pct=95.0, y_pct=95.0, w_pct=40.0, h_pct=40.0)
    x, y, w, h = _rect(huge, 1920, 1080, inset=0) or (0, 0, 0, 0)
    assert x + w <= 1920
    assert y + h <= 1080


def test_delogo_is_inset_off_the_frame_edge() -> None:
    """delogo refuses a region touching the edge, and refuses the whole render.

    "Logo area is outside of the frame" happens at filter-configuration time,
    so a corner bug would fail the job rather than one filter. A corner bug is
    also the single most likely thing anyone asks to hide.
    """
    corner = ObscureRegion(x_pct=0.0, y_pct=0.0, w_pct=14.0, h_pct=10.0)
    graph = chains([corner.model_copy(update={"method": ObscureMethod.DELOGO})])
    assert "delogo=x=2:y=2:" in graph


# ── Choosing how to hide it ──────────────────────────────────────────────────


def test_a_channel_bug_is_reconstructed_rather_than_blurred() -> None:
    """Small enough to rebuild from its own surroundings, and it looks it."""
    assert chains([BUG]).startswith("delogo=")


def test_a_wide_score_bar_is_blurred_rather_than_reconstructed() -> None:
    """delogo over a 30%-wide plate drew a smear across the top of the frame.

    Measured, not reasoned about: the first render on real football footage
    produced exactly that, and the fix was to bound the sides rather than only
    the area.
    """
    plate = ObscureRegion(x_pct=2.5, y_pct=2.2, w_pct=30.0, h_pct=8.9)
    assert "gblur" in chains([plate])
    assert "delogo" not in chains([plate])


def test_a_stated_method_is_not_second_guessed() -> None:
    assert "gblur" in chains([BUG.model_copy(update={"method": ObscureMethod.BLUR})])


def test_blur_radius_scales_with_the_region() -> None:
    """A sigma that erases a small bug leaves a large caption readable."""
    small = ObscureRegion(x_pct=10, y_pct=10, w_pct=4, h_pct=3, method=ObscureMethod.BLUR)
    large = ObscureRegion(x_pct=10, y_pct=10, w_pct=40, h_pct=30, method=ObscureMethod.BLUR)

    def sigma(region: ObscureRegion) -> float:
        return float(chains([region]).split("sigma=")[1].split(":")[0])

    assert sigma(large) > sigma(small) * 5


def test_strength_is_clamped_rather_than_trusted() -> None:
    wild = BUG.model_copy(update={"method": ObscureMethod.BLUR, "strength": 9.0})
    calm = BUG.model_copy(update={"method": ObscureMethod.BLUR, "strength": 1.0})
    assert chains([wild]) == chains([calm])


def test_the_options_default_applies_to_regions_that_named_nothing() -> None:
    graph = chains([BUG], default_method=ObscureMethod.PIXELATE)
    assert "flags=neighbor" in graph


# ── Composing the graph ──────────────────────────────────────────────────────


def test_one_inline_filter_reads_the_graphs_own_input() -> None:
    """No input label on the first chain, and the agreed label on the last."""
    graph = chains([BUG])
    assert graph.startswith("delogo=")
    assert graph.endswith("[obscured]")


def test_a_blur_splits_crops_and_puts_it_back() -> None:
    graph = chains([BUG.model_copy(update={"method": ObscureMethod.BLUR})])
    assert graph.split(";") == [
        "split=2[ob0a][ob0b]",
        "[ob0b]crop=264:96:1608:48,gblur=sigma=20.6:steps=2[ob0c]",
        "[ob0a][ob0c]overlay=1608:48[obscured]",
    ]


def test_regions_chain_into_one_another() -> None:
    """Each region reads what the one before it wrote."""
    second = ObscureRegion(x_pct=2.5, y_pct=2.2, w_pct=30.0, h_pct=8.9)
    parts = chains([BUG, second]).split(";")
    assert parts[0].endswith("[ob0]")
    assert parts[1].startswith("[ob0]")
    assert parts[-1].endswith("[obscured]")


def test_a_time_window_gates_the_filter() -> None:
    timed = BUG.model_copy(update={"from_sec": 2.0, "to_sec": 5.5})
    assert "enable='between(t,2.000,5.500)'" in chains([timed])


def test_an_open_ended_window_holds_to_the_end() -> None:
    assert "enable='gte(t,3.000)'" in chains([BUG.model_copy(update={"from_sec": 3.0})])


def test_a_timed_blur_is_gated_on_the_overlay() -> None:
    """crop and scale have no timeline support, so the gate goes where one is.

    A disabled overlay passes its first input through, which puts the original
    pixels back — the same result as disabling the blur, by the only route
    ffmpeg offers.
    """
    timed = BUG.model_copy(update={"method": ObscureMethod.BLUR, "from_sec": 1.0})
    graph = chains([timed])
    assert "overlay=1608:48:enable=" in graph
    assert "gblur=sigma=20.6:steps=2[ob0c]" in graph


# ── Where it sits in the whole chain ─────────────────────────────────────────


def test_hiding_happens_before_the_crop() -> None:
    """Source coordinates stop meaning anything the moment the crop runs."""
    graph = build_video_chain(
        media=media(),
        profile=profile(),
        framing=None,
        obscure=ObscureOptions(regions=[BUG]),
    )
    assert graph.index("delogo=") < graph.index("crop='min(iw")


def test_the_framing_reads_what_hiding_wrote() -> None:
    graph = build_video_chain(
        media=media(),
        profile=profile(),
        framing=None,
        obscure=ObscureOptions(regions=[BUG]),
    )
    assert "[obscured]crop='min(iw" in graph


def test_fit_composites_on_top_of_the_hidden_frame() -> None:
    """FIT's first chain is a split, and it has to read the hidden input too."""
    graph = build_video_chain(
        media=media(),
        profile=profile(),
        framing=Framing(mode=FramingMode.FIT),
        obscure=ObscureOptions(regions=[BUG]),
    )
    assert "[obscured]split=2[fitbg][fitfg]" in graph
    assert graph.index("delogo=") < graph.index("[fitbg]")


def test_captions_still_burn_last() -> None:
    graph = build_video_chain(
        media=media(),
        profile=profile(),
        framing=None,
        subtitles_expr="subtitles='x.ass'",
        obscure=ObscureOptions(regions=[BUG]),
    )
    assert graph.index("delogo=") < graph.index("scale=") < graph.index("subtitles=")


def test_an_empty_request_changes_nothing() -> None:
    plain = build_video_chain(media=media(), profile=profile(), framing=None)
    asked = build_video_chain(
        media=media(), profile=profile(), framing=None, obscure=ObscureOptions(auto=True)
    )
    assert plain == asked


def test_dimensions_that_could_not_be_read_are_a_failure_not_a_shrug() -> None:
    """Rendering the logo anyway is the failure this whole module exists for."""
    from clipforge.media.framing import FramingError

    with pytest.raises(FramingError, match="dimensions"):
        build_video_chain(
            media=media(width=None, height=None),  # type: ignore[arg-type]
            profile=profile(),
            framing=None,
            obscure=ObscureOptions(regions=[BUG]),
        )


# ── Putting boxes together ───────────────────────────────────────────────────


def test_two_names_for_the_same_rectangle_are_one_rectangle() -> None:
    nearly = BUG.model_copy(update={"x_pct": 84.2, "y_pct": 4.9})
    assert overlaps(BUG, nearly)


def test_separate_marks_stay_separate() -> None:
    other = ObscureRegion(x_pct=2.5, y_pct=2.2, w_pct=30.0, h_pct=8.9)
    assert not overlaps(BUG, other)


def test_a_small_box_inside_a_large_one_counts_as_covered() -> None:
    """Measured against the smaller, so a generous hand-drawn box wins."""
    drawn = ObscureRegion(x_pct=80.0, y_pct=2.0, w_pct=20.0, h_pct=14.0)
    assert overlaps(BUG, drawn)


def test_precedence_is_the_order_they_are_passed() -> None:
    drawn = ObscureRegion(x_pct=80.0, y_pct=2.0, w_pct=20.0, h_pct=14.0, label="by hand")
    merged = merge_regions([drawn], [BUG])
    assert len(merged) == 1
    assert merged[0].label == "by hand"


def test_distinct_marks_all_survive_the_merge() -> None:
    plate = ObscureRegion(x_pct=2.5, y_pct=2.2, w_pct=30.0, h_pct=8.9)
    assert len(merge_regions([plate], [BUG])) == 2


# ── Joining the halves of one graphic ────────────────────────────────────────


def test_a_badge_and_the_clock_beside_it_become_one_plate() -> None:
    """A match clock's digits change every second, so only the badge is static.

    Detection came back with a box over the badge and left "34:37" beside it in
    the open, which looks like a fault rather than a decision. Two finds at the
    same height with a small gap are one plate, and the gap is swallowed.
    """
    badge = ObscureRegion(x_pct=2.5, y_pct=2.2, w_pct=5.0, h_pct=8.9)
    score = ObscureRegion(x_pct=12.5, y_pct=2.2, w_pct=20.0, h_pct=8.9)
    joined = _join_bands([(0.73, badge), (0.75, score)])
    assert len(joined) == 1
    confidence, plate = joined[0]
    assert (plate.x_pct, plate.w_pct) == (2.5, 30.0)
    assert confidence == 0.75


def test_marks_at_opposite_ends_of_the_frame_are_not_one_plate() -> None:
    score = ObscureRegion(x_pct=2.5, y_pct=2.2, w_pct=20.0, h_pct=8.9)
    assert len(_join_bands([(0.9, BUG), (0.7, score)])) == 2


def test_marks_at_different_heights_are_not_one_plate() -> None:
    top = ObscureRegion(x_pct=10.0, y_pct=2.0, w_pct=8.0, h_pct=6.0)
    bottom = ObscureRegion(x_pct=20.0, y_pct=80.0, w_pct=8.0, h_pct=6.0)
    assert len(_join_bands([(0.8, top), (0.8, bottom)])) == 2


# ── Detection, on footage it must refuse to guess about ──────────────────────


def test_a_clip_too_short_to_judge_says_so(tmp_path: object) -> None:
    found = detect_static_regions(
        __import__("pathlib").Path("nonexistent.mp4"), start_sec=0.0, end_sec=0.4
    )
    assert found.regions == []
    assert "too short" in found.reason


def test_a_reason_is_carried_rather_than_an_empty_list() -> None:
    """ "Nothing found" and "nothing could be found" need different next moves."""
    assert Detection([], 0, "the footage barely moves").reason


# ── Saying what happened ─────────────────────────────────────────────────────


def test_a_region_describes_itself_by_where_it_is() -> None:
    assert describe_region(BUG) == "top right (14% x 9%)"


def test_a_label_wins_over_the_geometry() -> None:
    assert describe_region(BUG.model_copy(update={"label": "canal+ bug"})) == "canal+ bug"


def test_the_default_strength_is_strong_enough_to_be_a_decision() -> None:
    """A blur you can read the logo through looks like a bug, not a choice."""
    assert DEFAULT_STRENGTH >= 0.5


def test_a_found_region_records_that_it_was_found() -> None:
    assert ObscureRegion(x_pct=1, y_pct=1, w_pct=5, h_pct=5, found=ObscureFound.AUTO).found is (
        ObscureFound.AUTO
    )


def test_the_grid_reduction_averages_rather_than_samples() -> None:
    """A logo is a few cells, so one unlucky pixel must not decide a cell."""
    from clipforge.media.obscure import _cells

    plane = np.zeros((12, 12), dtype=np.float32)
    plane[0, 0] = 255.0
    reduced = _cells(plane)
    assert reduced[0, 0] == pytest.approx(255.0 / 36)
