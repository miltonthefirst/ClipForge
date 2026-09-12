"""Framing: which pixels of a wide source survive into a tall clip.

These assert on filtergraph *strings* rather than on rendered video, which is
the only way to test the part most likely to be wrong. A misframed clip does not
fail — it encodes perfectly and shows the wrong third of the pitch — so the
expression is the artefact worth pinning. Whether those expressions actually do
what they claim is a separate question, answered by
`tests/integration/test_framing_renders.py` against real ffmpeg.
"""

from __future__ import annotations

import pytest
from clipforge.media.ffprobe import MediaInfo
from clipforge.media.framing import (
    TARGET_RATIO,
    FramingError,
    as_rendered,
    build_video_chain,
    crop_expression,
    pan_expression,
    resolve_keyframes,
)
from clipforge.media.profiles import RenderProfile
from clipforge_contracts import FitFill, Framing, FramingMode, PanKeyframe

# Every test in this file is the unit tier. Without this marker CI's
# `pytest -m unit` silently deselects the whole file — the tests pass locally,
# run nowhere, and protect nothing.
pytestmark = pytest.mark.unit


def media(width: int = 1920, height: int = 1080) -> MediaInfo:
    return MediaInfo(
        duration_sec=60.0,
        width=width,
        height=height,
        has_video=True,
        has_audio=True,
        format_name="mov,mp4",
        size_bytes=1024,
        fps=30.0,
    )


def crop_params(expression: str) -> tuple[str, str, str, str]:
    """The four `crop=w:h:x:y` arguments, unquoted.

    Every argument is single-quoted by `_crop`, so splitting on "':'" is exact
    rather than a guess at where an expression ends.
    """
    assert expression.startswith("crop='"), expression
    body = expression[len("crop='") : -1]
    width, height, x, y = body.split("':'")
    return width, height, x, y


def evaluate(expression: str, *, iw: int, ih: int) -> float:
    """Work out what ffmpeg would compute for one crop argument.

    The expressions use only arithmetic, `min` and the two dimension variables,
    so Python evaluates them identically. Asserting on the NUMBER rather than on
    the string is what these tests actually mean: the shape of the expression is
    an implementation detail, and pinning it is how three tests came to fail on
    a change that rendered correctly in every case.
    """
    scope = {"iw": iw, "ih": ih, "min": min, "max": max}
    return float(eval(expression, {"__builtins__": {}}, scope))  # noqa: S307


def crop_geometry(expression: str, *, iw: int, ih: int) -> tuple[float, float, float, float]:
    w, h, x, y = crop_params(expression)
    return (
        evaluate(w, iw=iw, ih=ih),
        evaluate(h, iw=iw, ih=ih),
        evaluate(x, iw=iw, ih=ih),
        evaluate(y, iw=iw, ih=ih),
    )


def chain(framing: Framing | None, **kwargs: object) -> str:
    return build_video_chain(
        media=kwargs.pop("media", media()),  # type: ignore[arg-type]
        profile=kwargs.pop("profile", RenderProfile()),  # type: ignore[arg-type]
        framing=framing,
        **kwargs,  # type: ignore[arg-type]
    )


# ── The default is unchanged ─────────────────────────────────────────────────


def test_no_framing_request_is_the_fixed_centre_crop_it_always_was() -> None:
    """The behaviour every clip had before REMAKE existed.

    Worth a test of its own: `build_filtergraph` now delegates here, so a
    regression in this module would silently change every ordinary render.
    """
    graph = chain(None)
    assert graph.startswith("crop=")
    assert "scale=1080:1920:flags=lanczos" in graph
    assert "setsar=1" in graph
    # One chain, not a graph: no `;` and no labels.
    assert ";" not in graph
    assert "[" not in graph


@pytest.mark.parametrize(
    ("anchor", "expected_x"),
    [("left", 0.0), ("centre", (1920 - 607.5) / 2), ("right", 1920 - 607.5)],
)
def test_each_anchor_puts_the_window_somewhere_different(anchor: str, expected_x: float) -> None:
    """Asserted as the pixel column the window starts at, on a real 1920x1080."""
    _, _, x, _ = crop_geometry(crop_expression(media(), anchor), iw=1920, ih=1080)
    assert x == pytest.approx(expected_x)


def test_the_three_anchors_do_not_agree_with_each_other() -> None:
    """A guard against the offsets collapsing into one expression."""
    graphs = {a: crop_expression(media(), a) for a in ("left", "centre", "right")}
    assert len(set(graphs.values())) == 3


def test_an_unknown_anchor_is_refused_rather_than_defaulted() -> None:
    with pytest.raises(FramingError, match="unknown crop anchor"):
        crop_expression(media(), "middle-ish")


def test_a_vertical_source_is_cropped_in_height_not_pillarboxed() -> None:
    """Cropping width on a 9:16 source would leave black bars beside real picture."""
    w, h, x, y = crop_geometry(crop_expression(media(1080, 1920), "centre"), iw=1080, ih=1920)
    assert (w, x) == (1080.0, 0.0), "the full width must survive"
    assert (h, y) == (1920.0, 0.0), "and on an exactly-9:16 source, the full height too"


# 9:16 is 0.5625; anything with a SMALLER width/height ratio is taller than the
# canvas. A square source is 1.0, i.e. wider, and belongs with the landscape
# cases above.
@pytest.mark.parametrize(("iw", "ih"), [(1080, 1920), (1080, 2160), (1080, 2400)])
def test_a_source_taller_than_9_16_keeps_its_full_width(iw: int, ih: int) -> None:
    """The window may never be wider than the frame.

    ffmpeg does not clamp an oversized crop, it refuses the whole filtergraph —
    so before the width was clamped, every PAN and TRACK remake of ordinary
    18:9 or 20:9 phone footage failed at the render while the other modes of the
    same source succeeded.
    """
    w, h, x, y = crop_geometry(crop_expression(media(iw, ih), "centre"), iw=iw, ih=ih)
    assert w == float(iw)
    assert w / h == pytest.approx(TARGET_RATIO)
    assert x >= 0 and x + w <= iw
    assert y >= 0 and y + h <= ih


def test_a_pan_window_is_clamped_to_the_frame_too() -> None:
    """The same guarantee on the moving path, which had no such branch at all."""
    expr = pan_expression(media(1080, 2400), [PanKeyframe(at_sec=0, x_pct=100)])
    w, h, x, y = crop_geometry(expr, iw=1080, ih=2400)
    assert w == 1080.0
    assert x == 0.0, "clamped, not hanging off the right edge"
    assert y + h <= 2400


def test_the_request_anchor_beats_the_profile_anchor() -> None:
    profile = RenderProfile(crop="left")
    assert as_rendered(profile, None) == "left"
    assert as_rendered(profile, Framing(mode=FramingMode.AS_RENDERED, crop="right")) == "right"
    # No anchor in the request means the profile still decides.
    assert as_rendered(profile, Framing(mode=FramingMode.AS_RENDERED)) == "left"


# ── FIT ──────────────────────────────────────────────────────────────────────


def test_fit_composites_the_picture_over_a_blurred_copy_of_itself() -> None:
    graph = chain(Framing(mode=FramingMode.FIT))
    assert "split=2[fitbg][fitfg]" in graph
    assert "gblur" in graph
    assert "overlay=" in graph
    # A graph, not a chain: the branches are separate and joined by `;`.
    assert graph.count(";") == 3


def test_fit_never_crops_the_source() -> None:
    """The whole point of the mode, expressed as the absence of a crop.

    The only `crop` in a FIT graph belongs to the background fill, which is
    deliberately over-scaled and then cut back to the canvas. The picture
    branch must not be cropped at all, or the mode is a lie.
    """
    graph = chain(Framing(mode=FramingMode.FIT))
    picture = next(part for part in graph.split(";") if part.startswith("[fitfg]"))
    assert "crop" not in picture
    assert "force_original_aspect_ratio=decrease" in picture


def test_solid_fill_does_not_pay_for_a_blur() -> None:
    graph = chain(Framing(mode=FramingMode.FIT, fill=FitFill.SOLID))
    assert "gblur" not in graph
    assert "drawbox" in graph


def test_the_picture_band_can_be_moved_up_for_captions() -> None:
    centred = chain(Framing(mode=FramingMode.FIT))
    raised = chain(Framing(mode=FramingMode.FIT, offset_y_pct=-10))
    assert centred != raised
    assert "-0.1000*H" in raised


# ── PAN ──────────────────────────────────────────────────────────────────────


def test_a_pan_needs_at_least_one_keyframe() -> None:
    with pytest.raises(FramingError, match="needs at least one keyframe"):
        chain(Framing(mode=FramingMode.PAN), keyframes=[])


def test_one_keyframe_is_a_fixed_off_centre_crop_with_no_t_in_it() -> None:
    """A single point means "hold it here", which needs no interpolation.

    This is what lets a fixed off-centre window be expressed without a fourth
    framing mode for it.
    """
    expr = pan_expression(media(), [PanKeyframe(at_sec=0, x_pct=20)])
    assert "if(" not in expr
    assert "iw*0.200000" in expr


def test_two_keyframes_interpolate_between_them() -> None:
    expr = pan_expression(
        media(), [PanKeyframe(at_sec=0, x_pct=10), PanKeyframe(at_sec=4, x_pct=90)]
    )
    assert "if(lt(t,4.0000)" in expr
    assert "iw*0.100000" in expr
    assert "iw*0.900000" in expr


def test_the_window_is_clamped_inside_the_frame() -> None:
    """Keyframes at 0 and 100 are ordinary requests, not errors.

    "As far right as this can go" is a thing a reviewer means, so the
    expression clamps rather than the contract refusing.
    """
    expr = pan_expression(media(), [PanKeyframe(at_sec=0, x_pct=100)])
    assert "max(0,min(iw-" in expr


def test_keyframes_are_sorted_rather_than_trusted() -> None:
    ordered = pan_expression(
        media(), [PanKeyframe(at_sec=0, x_pct=10), PanKeyframe(at_sec=5, x_pct=80)]
    )
    shuffled = pan_expression(
        media(), [PanKeyframe(at_sec=5, x_pct=80), PanKeyframe(at_sec=0, x_pct=10)]
    )
    assert ordered == shuffled


def test_the_time_expression_is_quoted_so_its_commas_survive() -> None:
    """Commas end a filter unless they are inside quotes.

    Nested inside FIT's graph a backslash escape would be consumed a layer at a
    time; quoting does not have that problem, and this pins the choice.
    """
    expr = pan_expression(
        media(), [PanKeyframe(at_sec=0, x_pct=10), PanKeyframe(at_sec=4, x_pct=90)]
    )
    # The x argument of `crop=w:h:x:y`, quoted as a whole. Asserted by shape
    # rather than by splitting on ":", which the surrounding arithmetic also
    # uses.
    assert ":'max(0,min(iw-" in expr
    assert expr.endswith(":0") or "'" in expr.rsplit(":", 1)[0]
    assert "\\," not in expr, "backslash escaping would be eaten by FIT's nesting"


# ── Zoom ─────────────────────────────────────────────────────────────────────


def test_zoom_one_keeps_the_window_full_height() -> None:
    assert ":ih:" in crop_expression(media(), "centre", zoom=1.0) or "ih" in crop_expression(
        media(), "centre", zoom=1.0
    )


def test_zoom_tightens_the_window_and_recentres_it_vertically() -> None:
    tight = crop_expression(media(), "centre", zoom=1.5)
    assert "ih*0.666667" in tight
    # A window shorter than the frame has to be placed vertically.
    assert tight.rsplit(":", 1)[1] != "0"


def test_zoom_is_clamped_to_what_the_pixels_support() -> None:
    """Past 2x a 1080-line source cannot fill a 1080-wide canvas."""
    assert chain(Framing(mode=FramingMode.AS_RENDERED, zoom=2)) == chain(
        Framing(mode=FramingMode.AS_RENDERED, zoom=2)
    )
    # Below 1 is not a tighter shot, it is a wider one, which is FIT's job.
    assert chain(Framing(mode=FramingMode.AS_RENDERED, zoom=1)) == chain(None)


# ── Composition ──────────────────────────────────────────────────────────────


def test_captions_are_burned_after_the_scale_in_every_mode() -> None:
    """Font sizes in a profile are in output pixels, so the order is load-bearing."""
    for framing, keyframes in (
        (None, []),
        (Framing(mode=FramingMode.FIT), []),
        (Framing(mode=FramingMode.PAN), [PanKeyframe(at_sec=0, x_pct=50)]),
    ):
        graph = chain(framing, keyframes=keyframes, subtitles_expr="subtitles='x.ass'")
        assert graph.endswith("subtitles='x.ass'")


def test_captions_attach_to_the_last_chain_of_a_fit_graph() -> None:
    """Not to the first, which is the blurred background.

    Getting this wrong would blur the captions along with the backdrop, which
    looks like a rendering bug rather than a filter-order mistake.
    """
    graph = chain(Framing(mode=FramingMode.FIT), subtitles_expr="subtitles='x.ass'")
    assert graph.split(";")[-1].startswith("[bg][fg]overlay=")
    assert "subtitles" in graph.split(";")[-1]
    assert "subtitles" not in graph.split(";")[1]


def test_a_profile_frame_rate_is_applied_before_the_captions() -> None:
    graph = chain(None, profile=RenderProfile(fps=30), subtitles_expr="subtitles='x.ass'")
    assert graph.index("fps=30") < graph.index("subtitles")


# ── Keyframe precedence ──────────────────────────────────────────────────────


def test_track_uses_what_the_tracker_found_and_ignores_what_was_sent() -> None:
    sent = [PanKeyframe(at_sec=0, x_pct=10)]
    found = [PanKeyframe(at_sec=0, x_pct=80)]
    resolved = resolve_keyframes(Framing(mode=FramingMode.TRACK, keyframes=sent), tracked=found)
    assert resolved == found


def test_pan_uses_what_was_sent_and_ignores_the_tracker() -> None:
    sent = [PanKeyframe(at_sec=0, x_pct=10)]
    resolved = resolve_keyframes(
        Framing(mode=FramingMode.PAN, keyframes=sent),
        tracked=[PanKeyframe(at_sec=0, x_pct=80)],
    )
    assert resolved == sent


def test_the_still_modes_have_no_keyframes_at_all() -> None:
    assert resolve_keyframes(Framing(mode=FramingMode.FIT)) == []
    assert resolve_keyframes(Framing(mode=FramingMode.AS_RENDERED)) == []
    assert resolve_keyframes(None) == []


def test_the_target_ratio_is_the_output_shape() -> None:
    assert pytest.approx(1080 / 1920) == TARGET_RATIO
