"""Deciding which pixels of a landscape source end up in a 9:16 clip.

Until this module existed there was one answer: take a full-height window a
third of the frame wide, put it in the middle, and leave it there for the whole
clip (`clipforge.media.render._crop_expression`). That is correct for a talking
head, which does not move, and wrong for anything else. On a 1920-wide broadcast
frame the window keeps 608 pixels; a football leaves that window several times a
minute, and the clip that results is of a pitch the ball is not on.

Three answers, because the footage does not have one:

* **FIT** scales the entire frame into the canvas and fills the rest. Nothing
  can leave the picture because nothing is discarded. The picture is smaller,
  and for a wide pitch view that is usually the right trade.
* **PAN** moves a full-height window between points a reviewer set while
  watching. Exact, and exactly as much work as it sounds.
* **TRACK** is PAN with the points found in the footage rather than typed —
  see `clipforge.media.tracking`. The output is the same shape, which is the
  point: a tracked clip records its keyframes, so correcting the tracker means
  editing its answer rather than starting again.

Everything here returns a filtergraph string and touches no files, so the
awkward parts — expression syntax, clamping, the order filters compose in — are
assertable in a unit test without ffmpeg present. That matters more than usual:
a wrong crop expression does not fail, it renders something subtly misframed,
and nobody notices until they watch all of it.

**On escaping.** Time-varying expressions contain commas, and a bare comma ends
a filter. They are wrapped in single quotes rather than backslash-escaped: both
work, but quoting survives being nested inside a filtergraph where each layer
would otherwise consume one backslash, and that nesting is exactly what FIT
introduces. The rule for reading this file is that anything inside `'...'` is
ffmpeg expression syntax and anything outside it is filtergraph syntax.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from clipforge_contracts import FitFill, Framing, FramingMode, PanKeyframe

from clipforge.media.ffprobe import MediaInfo
from clipforge.media.profiles import OUTPUT_HEIGHT, OUTPUT_WIDTH, RenderProfile

__all__ = [
    "TARGET_RATIO",
    "FramingError",
    "as_rendered",
    "build_video_chain",
    "crop_expression",
    "fit_graph",
    "pan_expression",
    "resolve_keyframes",
]

TARGET_RATIO = OUTPUT_WIDTH / OUTPUT_HEIGHT  # 0.5625


class FramingError(ValueError):
    """A framing request that cannot be rendered as asked."""


@dataclass(frozen=True)
class _Window:
    """The moving window's size, in source pixels, as ffmpeg expressions."""

    width_expr: str
    height_expr: str


def as_rendered(profile: RenderProfile, framing: Framing | None) -> str:
    """The anchor a fixed crop should use: the request's, else the profile's."""
    if framing is not None and framing.crop is not None:
        crop = framing.crop
        return str(getattr(crop, "value", crop))
    return profile.crop


def build_video_chain(
    *,
    media: MediaInfo,
    profile: RenderProfile,
    framing: Framing | None,
    keyframes: Sequence[PanKeyframe] = (),
    subtitles_expr: str | None = None,
) -> str:
    """The complete video filtergraph for one clip, ready for `-vf`.

    Ordered the way `clipforge.media.render` has always ordered it, and for the
    same reasons: reframe in source coordinates, then scale, then burn captions
    in output pixels so a profile's font size means one thing on every source.

    FIT returns a *graph* — several chains joined by `;` — because filling the
    canvas means compositing the frame over a blurred copy of itself, and one
    input cannot be used twice in a single chain. Everything appended afterwards
    belongs to the last chain, which is the one carrying the finished picture.
    """
    mode = _mode(framing)

    if mode is FramingMode.FIT:
        chains = fit_graph(framing)
    elif mode in (FramingMode.PAN, FramingMode.TRACK):
        if not keyframes:
            raise FramingError(
                f"{mode.value} framing needs at least one keyframe; "
                "none were given and none could be worked out from the footage"
            )
        chains = [
            ",".join(
                (
                    pan_expression(media, keyframes, zoom=_zoom(framing)),
                    f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:flags=lanczos",
                    "setsar=1",
                )
            )
        ]
    else:
        chains = [
            ",".join(
                (
                    crop_expression(media, as_rendered(profile, framing), zoom=_zoom(framing)),
                    f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:flags=lanczos",
                    "setsar=1",
                )
            )
        ]

    tail = [f"fps={profile.fps}"] if profile.fps else []
    if subtitles_expr:
        tail.append(subtitles_expr)
    if tail:
        chains[-1] = ",".join([chains[-1], *tail])
    return ";".join(chains)


# ── The fixed window ─────────────────────────────────────────────────────────


def crop_expression(media: MediaInfo, anchor: str, *, zoom: float = 1.0) -> str:
    """A still 9:16 window, anchored left, centre or right.

    Expressed with ffmpeg's own `iw`/`ih` rather than the probed numbers, so the
    filter stays correct when a variable-resolution stream turns out not to
    match what ffprobe reported.

    There is no longer a separate branch for a source that is already tall.
    `_window` clamps the width to the frame, so on a 9:16-or-taller source the
    window becomes the full width, the height works out to `iw / 0.5625`, and
    `_y_offset` centres it — which is precisely what that branch did by hand.
    """
    del media  # the expression is in iw/ih; probed numbers would only go stale
    window = _window(zoom)
    offsets = {
        "centre": f"(iw-({window.width_expr}))/2",
        "left": "0",
        "right": f"iw-({window.width_expr})",
    }
    if anchor not in offsets:
        raise FramingError(f"unknown crop anchor {anchor!r}")
    return _crop(window, x=offsets[anchor])


# ── The moving window ────────────────────────────────────────────────────────


def pan_expression(media: MediaInfo, keyframes: Sequence[PanKeyframe], *, zoom: float = 1.0) -> str:
    """A window whose horizontal position is a function of time.

    Built as one nested expression rather than as a sequence of filters, because
    `crop` evaluates `x` per frame and a single expression is the only form in
    which ffmpeg will do that. It is linear between consecutive keyframes and
    flat outside the first and last — so one keyframe is a legal way of saying
    "hold it here", which is what makes a fixed off-centre crop expressible
    without a fourth mode for it.

    `xPct` is the *centre* of the window, which is how a person thinks about it
    ("the ball is two-thirds across"), so the expression subtracts half a window
    and then clamps. The clamp is not defensive: keyframes at 0 and 100 are
    ordinary requests meaning "as far as this can go", and without it they would
    ask ffmpeg for pixels outside the frame.
    """
    del media  # the expression is in iw/ih; probed dimensions would only go stale
    if not keyframes:
        raise FramingError("a pan needs at least one keyframe")

    window = _window(zoom)
    points = sorted(keyframes, key=lambda k: k.at_sec)
    centre = _centre_expression(points)
    left = f"({centre})-({window.width_expr})/2"
    x = f"max(0,min(iw-({window.width_expr}),{left}))"

    return _crop(window, x=x)


def _centre_expression(points: Sequence[PanKeyframe]) -> str:
    """Piecewise-linear interpolation of the window centre, innermost last.

    Written as nested `if(lt(t,...))` rather than with ffmpeg's `lerp`, which
    takes constants: the endpoints here are expressions in `iw`, because a
    percentage of source width is not a number until ffmpeg knows the width.
    """
    first = _px(points[0].x_pct)
    if len(points) == 1:
        return first

    expr = _px(points[-1].x_pct)  # held flat past the last keyframe
    for start, end in reversed(list(pairwise(points))):
        span = max(1e-6, end.at_sec - start.at_sec)
        a, b = _px(start.x_pct), _px(end.x_pct)
        ramp = f"({a})+(({b})-({a}))*(t-{start.at_sec:.4f})/{span:.4f}"
        expr = f"if(lt(t,{end.at_sec:.4f}),{ramp},{expr})"

    # Before the first keyframe, hold the first value rather than extrapolating
    # backwards off the edge of the frame.
    return f"if(lt(t,{points[0].at_sec:.4f}),{first},{expr})"


def _px(x_pct: float) -> str:
    return f"iw*{max(0.0, min(100.0, x_pct)) / 100:.6f}"


# ── The whole frame ──────────────────────────────────────────────────────────


def fit_graph(framing: Framing | None) -> list[str]:
    """Scale the whole frame into the canvas and fill what is left.

    Two branches of one input: the fill is the same frame blown up past the
    canvas, cropped to it and blurred into unrecognisability, and the picture is
    the frame scaled to fit inside. Overlaying the second on the first gives a
    background that moves with the footage instead of a black bar.

    `force_original_aspect_ratio=decrease` does the arithmetic that would
    otherwise have to be done here from probed dimensions, and gets it right for
    sources that are not 16:9 — which sports footage frequently is not.

    Returned as separate chains for the caller to join with `;`. The last one
    must stay last: it is the chain the caption filter is appended to.
    """
    zoom = _zoom(framing)
    fill = framing.fill if framing is not None and framing.fill is not None else FitFill.BLUR
    offset = float(framing.offset_y_pct or 0.0) if framing is not None else 0.0

    inner_w = int(OUTPUT_WIDTH * zoom)
    inner_h = int(OUTPUT_HEIGHT * zoom)
    y = f"(H-h)/2+{offset / 100:.4f}*H"

    cover = (
        f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}"
        ":force_original_aspect_ratio=increase:flags=bilinear,"
        f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}"
    )
    if fill is FitFill.BLUR:
        # Two cheap passes rather than one wide one: gblur at a radius that
        # destroys detail in a single pass costs more than the encode does. The
        # eq darkens and saturates so the fill reads as a backdrop rather than
        # as a second, confusing copy of the picture.
        background = f"{cover},gblur=sigma=28:steps=2,eq=brightness=-0.12:saturation=1.15,setsar=1"
    else:
        background = f"{cover},drawbox=x=0:y=0:w=iw:h=ih:color=0x11141A:t=fill,setsar=1"

    picture = (
        f"scale={inner_w}:{inner_h}:force_original_aspect_ratio=decrease:flags=lanczos,setsar=1"
    )

    return [
        "split=2[fitbg][fitfg]",
        f"[fitbg]{background}[bg]",
        f"[fitfg]{picture}[fg]",
        f"[bg][fg]overlay=x=(W-w)/2:y={y}:shortest=1",
    ]


# ── Shared arithmetic ────────────────────────────────────────────────────────


def _crop(window: _Window, *, x: str) -> str:
    """Assemble `crop=w:h:x:y`, quoting every parameter.

    Quoted unconditionally rather than only when needed. Clamping the window
    put a `min(...)` into the width, and its comma would otherwise terminate the
    filter and leave a graph that parses into something entirely different — a
    failure that reads as "unknown filter" rather than as a quoting mistake. The
    rule is easier to keep than the exception: anything inside `'...'` is ffmpeg
    expression syntax, everything outside it is filtergraph syntax.
    """
    return f"crop='{window.width_expr}':'{window.height_expr}':'{x}':'{_y_offset(window)}'"


def _y_offset(window: _Window) -> str:
    """Vertical placement: centred.

    Derived from the window's own height rather than from the zoom, because the
    height is no longer a function of zoom alone — a source taller than 9:16
    gets a window shorter than the frame at zoom 1, and that window has to be
    centred too or a tall clip is cropped to its top.
    """
    return f"(ih-({window.height_expr}))/2"


def _window(zoom: float) -> _Window:
    """The moving window's size, in source pixels.

    At zoom 1 it is as large a 9:16 window as the frame allows. Zoom shrinks it,
    which tightens the shot and costs the margin that keeps a moving subject in
    frame — so it defaults to 1 and the UI says what it trades away.

    **The width is clamped to the frame, and that is not defensive.** A
    full-height 9:16 window is `ih * 0.5625` wide, which exceeds `iw` on any
    source *taller* than 9:16 — ordinary phone footage at 18:9 or 20:9. ffmpeg's
    crop does not clamp an oversized width, it refuses the whole filtergraph
    ("Invalid too big or non positive size for width"), so without this every
    PAN and TRACK remake of a tall source fails at the render while AS_RENDERED
    and FIT of the same source succeed. The height is then derived from the
    clamped width rather than assumed, which is what keeps the window 9:16 in
    both regimes.

    On a landscape source `min` selects `ih * 0.5625` and the height works back
    out to exactly `ih`, so the numbers are identical to the fixed crop this
    module replaced — which matters, because RENDER now goes through here.
    """
    height = "ih" if zoom <= 1.0 else f"ih*{1.0 / zoom:.6f}"
    width = f"min(iw,({height})*{TARGET_RATIO:.6f})"
    return _Window(width_expr=width, height_expr=f"({width})/{TARGET_RATIO:.6f}")


def _mode(framing: Framing | None) -> FramingMode:
    return framing.mode if framing is not None else FramingMode.AS_RENDERED


def _zoom(framing: Framing | None) -> float:
    if framing is None or framing.zoom is None:
        return 1.0
    return max(1.0, min(2.0, float(framing.zoom)))


def resolve_keyframes(
    framing: Framing | None, *, tracked: Sequence[PanKeyframe] = ()
) -> list[PanKeyframe]:
    """Which keyframes a render should use, given the mode.

    TRACK ignores any the client sent and uses what the tracker found; PAN uses
    the client's and never the tracker's. Kept here rather than in the stage so
    the precedence is one testable rule instead of a branch inside an I/O path.
    """
    mode = _mode(framing)
    if mode is FramingMode.TRACK:
        return list(tracked)
    if mode is FramingMode.PAN and framing is not None:
        return list(framing.keyframes or [])
    return []
