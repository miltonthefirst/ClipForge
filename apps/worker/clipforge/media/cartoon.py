"""Drawing stick-figure cartoons, one frame at a time, and encoding them.

The renderer for a composed video. It takes a timed scene — a line, some
figures with a pose and a mood each, some props, a palette — and draws every
frame of it with Pillow, then pipes the frames into ffmpeg as raw video so
the result is a segment like any the compile pipeline joins.

**Deterministic on purpose.** Every position is a function of the scene and
of time; the only randomness is a generator seeded from the job's seed and the
scene's index, used for the placement of clouds and the like. Drawing the same
scene twice produces the same bytes, which is what lets a composed clip be
reproduced from its record and what lets the tests hash a frame.

**Procedural, not keyframed.** A pose is a small function of a phase: a walk
is two legs swinging in opposition and two arms swinging against them, a wave
is a forearm on a sine, a celebration is both arms up and the whole figure on
a bounce. There is no animation data to author and nothing to load. What it
buys is that a new pose is thirty lines here and one word in the contract's
enum, and that the model chooses from a vocabulary rather than describing a
picture nobody could draw.

**No likeness.** A figure has a head, a face with a mood, and a name under its
feet. The name is a role or a first name from the script. Nothing here can draw
a real person, and that is the rule of the visual mode rather than a limit of
the prompt (docs/adr/0027-drawn-cartoons-as-the-first-visual-mode.md).

Supersampled twice and downsampled, because Pillow draws lines without
anti-aliasing and a jagged stick figure looks broken rather than drawn.
"""

from __future__ import annotations

import math
import random
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from clipforge_contracts import StickActor, StickMood, StickPose, StickProp
from PIL import Image, ImageDraw, ImageFont

from clipforge.media.assemble import AssembledClip, output_args
from clipforge.media.profiles import OUTPUT_HEIGHT, OUTPUT_WIDTH, RenderProfile
from clipforge.media.render import RenderError
from clipforge.observability import get_logger
from clipforge.synth.scenes import Palette, TimedScene, palette_for

log = get_logger(__name__)

__all__ = [
    "DEFAULT_FPS",
    "Canvas",
    "draw_frame",
    "frame_count",
    "render_scene",
]

DEFAULT_FPS = 30
_SUPERSAMPLE = 2
#: Where the ground is, as a fraction of the height. Figures stand on it. High
#: enough that the captions the profile burns into the lower fifth land on the
#: ground beneath the figures rather than across their legs.
_GROUND = 0.62
#: A figure's height as a fraction of the frame height.
_FIGURE = 0.30
#: The slow push-in over a scene, so nothing is quite still.
_ZOOM_PER_SCENE = 0.04

RGB = tuple[int, int, int]
Point = tuple[float, float]


@dataclass(frozen=True)
class Canvas:
    """A frame's geometry, in the virtual 1080x1920 the profile renders."""

    width: int = OUTPUT_WIDTH
    height: int = OUTPUT_HEIGHT

    @property
    def ground_y(self) -> float:
        return self.height * _GROUND

    @property
    def figure_height(self) -> float:
        return self.height * _FIGURE


class _Pen:
    """Draws in virtual coordinates through a camera, onto a supersampled image."""

    def __init__(
        self, image: Image.Image, *, canvas: Canvas, scale: float, zoom: float, pan: float
    ) -> None:
        self._draw = ImageDraw.Draw(image)
        self._canvas = canvas
        self._scale = scale
        self._zoom = zoom
        self._pan = pan
        self._cx = canvas.width / 2
        self._cy = canvas.height * 0.55

    def at(self, point: Point) -> tuple[float, float]:
        x, y = point
        x = self._cx + (x - self._cx) * self._zoom + self._pan
        y = self._cy + (y - self._cy) * self._zoom
        return x * self._scale, y * self._scale

    def px(self, value: float) -> float:
        return value * self._scale * self._zoom

    def line(self, points: Sequence[Point], colour: RGB, width: float) -> None:
        self._draw.line(
            [self.at(p) for p in points],
            fill=colour,
            width=max(1, round(self.px(width))),
            joint="curve",
        )

    def circle(
        self,
        centre: Point,
        radius: float,
        *,
        fill: RGB | None,
        outline: RGB | None,
        width: float = 0,
    ) -> None:
        cx, cy = self.at(centre)
        r = self.px(radius)
        self._draw.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            fill=fill,
            outline=outline,
            width=max(1, round(self.px(width))) if outline else 0,
        )

    def polygon(
        self, points: Sequence[Point], *, fill: RGB | None, outline: RGB | None, width: float = 0
    ) -> None:
        self._draw.polygon(
            [self.at(p) for p in points],
            fill=fill,
            outline=outline,
            width=max(1, round(self.px(width))) if outline else 0,
        )

    def rect(
        self,
        top_left: Point,
        bottom_right: Point,
        *,
        fill: RGB | None,
        outline: RGB | None,
        width: float = 0,
        radius: float = 0,
    ) -> None:
        x0, y0 = self.at(top_left)
        x1, y1 = self.at(bottom_right)
        self._draw.rounded_rectangle(
            [x0, y0, x1, y1],
            radius=self.px(radius),
            fill=fill,
            outline=outline,
            width=max(1, round(self.px(width))) if outline else 0,
        )

    def arc(
        self,
        centre: Point,
        radius: float,
        start_deg: float,
        end_deg: float,
        colour: RGB,
        width: float,
    ) -> None:
        cx, cy = self.at(centre)
        r = self.px(radius)
        self._draw.arc(
            [cx - r, cy - r, cx + r, cy + r],
            start=start_deg,
            end=end_deg,
            fill=colour,
            width=max(1, round(self.px(width))),
        )

    def text(
        self, centre: Point, text: str, *, size: float, colour: RGB, bold: bool = False
    ) -> None:
        font = _font(round(self.px(size)), bold=bold)
        cx, cy = self.at(centre)
        self._draw.text((cx, cy), text, fill=colour, font=font, anchor="mm")

    def text_width(self, text: str, *, size: float) -> float:
        font = _font(round(self.px(size)))
        left, _, right, _ = font.getbbox(text)
        return (right - left) / (self._scale * self._zoom)


_FONT_CANDIDATES = (
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)
_FONTS: dict[tuple[int, bool], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def _font(size: int, *, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    key = (max(8, size), bold)
    cached = _FONTS.get(key)
    if cached is not None:
        return cached
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont | None = None
    for candidate in _FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(candidate, key[0])
            break
        except OSError:
            continue
    if font is None:
        # Pillow's built-in vector face, so a machine with no system fonts
        # still labels its figures rather than failing to draw.
        font = ImageFont.load_default(size=key[0])
    _FONTS[key] = font
    return font


# ── Poses ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Posture:
    """Where every joint is, relative to the feet, in units of figure height."""

    body_dx: float = 0.0  # lean, at the shoulder
    lift: float = 0.0  # whole figure off the ground
    head_tilt: float = 0.0
    left_arm: tuple[float, float] = (-0.16, 0.18)  # (shoulder->elbow) vector
    left_fore: tuple[float, float] = (-0.10, 0.18)
    right_arm: tuple[float, float] = (0.16, 0.18)
    right_fore: tuple[float, float] = (0.10, 0.18)
    left_leg: tuple[float, float] = (-0.08, 0.22)  # hip->knee
    left_shin: tuple[float, float] = (-0.02, 0.22)
    right_leg: tuple[float, float] = (0.08, 0.22)
    right_shin: tuple[float, float] = (0.02, 0.22)
    rotate: float = 0.0  # whole-figure rotation, degrees (FALL)
    mouth_open: float = 0.0


def _rot(v: tuple[float, float], degrees: float) -> tuple[float, float]:
    a = math.radians(degrees)
    x, y = v
    return (x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a))


def _posture(pose: StickPose, phase: float, t: float) -> _Posture:
    s = math.sin(phase)
    c = math.cos(phase)
    base = _Posture()
    if pose is StickPose.STAND:
        return _Posture(
            lift=0.004 * math.sin(phase * 0.5),
            left_arm=_rot(base.left_arm, 3 * s),
            right_arm=_rot(base.right_arm, -3 * s),
        )
    if pose is StickPose.WALK:
        swing = 28 * s
        return _Posture(
            lift=0.012 * abs(c),
            left_leg=_rot((0.0, 0.22), swing),
            left_shin=_rot((0.0, 0.22), max(0.0, swing) * 0.6),
            right_leg=_rot((0.0, 0.22), -swing),
            right_shin=_rot((0.0, 0.22), max(0.0, -swing) * 0.6),
            left_arm=_rot((0.0, 0.18), -20 * s - 8),
            left_fore=_rot((0.0, 0.16), -20 * s - 20),
            right_arm=_rot((0.0, 0.18), 20 * s + 8),
            right_fore=_rot((0.0, 0.16), 20 * s + 20),
        )
    if pose is StickPose.RUN:
        swing = 45 * s
        return _Posture(
            body_dx=0.06,
            lift=0.03 * abs(c),
            left_leg=_rot((0.0, 0.22), swing),
            left_shin=_rot((0.0, 0.22), max(0.0, swing) * 1.1),
            right_leg=_rot((0.0, 0.22), -swing),
            right_shin=_rot((0.0, 0.22), max(0.0, -swing) * 1.1),
            left_arm=_rot((0.0, 0.18), -40 * s - 20),
            left_fore=_rot((0.0, 0.16), -60),
            right_arm=_rot((0.0, 0.18), 40 * s + 20),
            right_fore=_rot((0.0, 0.16), 60),
        )
    if pose is StickPose.POINT:
        return _Posture(
            lift=0.004 * math.sin(phase * 0.5),
            right_arm=_rot((0.20, 0.0), -6 * s),
            right_fore=_rot((0.18, 0.0), -6 * s),
        )
    if pose is StickPose.WAVE:
        return _Posture(
            right_arm=(0.16, -0.12),
            right_fore=_rot((0.0, -0.18), 30 * s),
            mouth_open=0.0,
        )
    if pose is StickPose.TALK:
        gesture = 12 * s
        return _Posture(
            left_arm=_rot(base.left_arm, gesture),
            left_fore=_rot((-0.14, 0.02), gesture * 2),
            right_arm=_rot(base.right_arm, -gesture),
            right_fore=_rot((0.14, 0.02), -gesture * 2),
            mouth_open=max(0.0, math.sin(t * 9.0)) * 0.7 + 0.15,
        )
    if pose is StickPose.THINK:
        return _Posture(
            head_tilt=8.0,
            right_arm=(0.10, 0.08),
            right_fore=(-0.06, -0.20),
            left_arm=(-0.08, 0.16),
            left_fore=(0.12, 0.02),
        )
    if pose is StickPose.SHRUG:
        up = 0.5 + 0.5 * s
        return _Posture(
            left_arm=(-0.18, -0.04 * up + 0.02),
            left_fore=(-0.06, -0.14),
            right_arm=(0.18, -0.04 * up + 0.02),
            right_fore=(0.06, -0.14),
            head_tilt=-6.0 * up,
        )
    if pose is StickPose.CELEBRATE:
        return _Posture(
            lift=0.10 * abs(s),
            left_arm=_rot((-0.14, -0.14), 10 * s),
            left_fore=_rot((-0.06, -0.18), 10 * s),
            right_arm=_rot((0.14, -0.14), -10 * s),
            right_fore=_rot((0.06, -0.18), -10 * s),
            mouth_open=0.8,
        )
    if pose is StickPose.SIT:
        return _Posture(
            lift=0.16,
            left_leg=(0.16, 0.02),
            left_shin=(0.0, 0.16),
            right_leg=(0.18, 0.04),
            right_shin=(0.0, 0.14),
            left_arm=(-0.06, 0.16),
            right_arm=(0.14, 0.12),
            right_fore=(0.10, 0.0),
        )
    # FALL is what is left.
    return _Posture(
        rotate=78.0 + 2 * s,
        left_arm=(-0.16, -0.10),
        right_arm=(0.16, -0.06),
        left_leg=(-0.10, 0.20),
        right_leg=(0.12, 0.18),
        mouth_open=0.6,
    )


# ── Figures ───────────────────────────────────────────────────────────────────


def _draw_face(
    pen: _Pen,
    centre: Point,
    radius: float,
    mood: StickMood,
    *,
    ink: RGB,
    mouth_open: float,
    facing: int,
) -> None:
    cx, cy = centre
    eye_dx = radius * 0.36
    eye_y = cy - radius * 0.12
    eye_r = radius * 0.09
    brow_w = radius * 0.34
    brow_y = cy - radius * 0.42
    # Eyes.
    for side in (-1, 1):
        ex = cx + side * eye_dx + facing * radius * 0.08
        if mood is StickMood.HAPPY:
            pen.arc((ex, eye_y + eye_r), eye_r * 1.6, 200, 340, ink, radius * 0.07)
        elif mood is StickMood.SURPRISED:
            pen.circle((ex, eye_y), eye_r * 1.5, fill=None, outline=ink, width=radius * 0.06)
        else:
            pen.circle((ex, eye_y), eye_r, fill=ink, outline=None)
    # Brows say most of the mood.
    tilt = {
        StickMood.ANGRY: 14.0,
        StickMood.SAD: -12.0,
        StickMood.WORRIED: -18.0,
        StickMood.SURPRISED: 0.0,
    }.get(mood)
    if tilt is not None:
        lift = -radius * 0.14 if mood is StickMood.SURPRISED else 0.0
        for side in (-1, 1):
            bx = cx + side * eye_dx + facing * radius * 0.08
            dx, dy = _rot((brow_w / 2, 0.0), tilt * side)
            pen.line(
                [(bx - dx, brow_y + lift - dy), (bx + dx, brow_y + lift + dy)], ink, radius * 0.07
            )
    # Mouth.
    my = cy + radius * 0.40
    mw = radius * 0.5
    if mouth_open > 0.05:
        pen.circle(
            (cx + facing * radius * 0.1, my),
            radius * (0.08 + 0.14 * mouth_open),
            fill=ink,
            outline=None,
        )
    elif mood is StickMood.HAPPY:
        pen.arc((cx, my - radius * 0.12), mw * 0.7, 20, 160, ink, radius * 0.07)
    elif mood is StickMood.SAD or mood is StickMood.WORRIED:
        pen.arc((cx, my + radius * 0.22), mw * 0.6, 200, 340, ink, radius * 0.07)
    elif mood is StickMood.ANGRY:
        pen.line(
            [(cx - mw / 2, my + radius * 0.04), (cx + mw / 2, my - radius * 0.04)],
            ink,
            radius * 0.08,
        )
    elif mood is StickMood.SURPRISED:
        pen.circle((cx, my), radius * 0.16, fill=None, outline=ink, width=radius * 0.06)
    else:
        pen.line([(cx - mw / 2, my), (cx + mw / 2, my)], ink, radius * 0.07)


def _draw_figure(
    pen: _Pen,
    canvas: Canvas,
    actor: StickActor,
    *,
    x: float,
    facing: int,
    phase: float,
    t: float,
    ink: RGB,
    accent: RGB,
) -> None:
    height = canvas.figure_height
    posture = _posture(actor.pose, phase, t)
    stroke = height * 0.032
    feet_y = canvas.ground_y - posture.lift * height

    def rel(dx: float, dy: float) -> Point:
        # dx along facing, dy down, in units of figure height, around the feet.
        px = dx * facing * height
        py = dy * height
        if posture.rotate:
            px, py = _rot((px, py - height * 0.5), posture.rotate * facing)
            py += height * 0.5
            py -= height * 0.42  # lying down, the mass sits lower
        return (x + px, feet_y - height + py)

    hip = rel(0.0, 0.56)
    if actor.pose is StickPose.SIT:
        # A bench under the hips, drawn first so the figure sits in front of it.
        seat_y = hip[1] + stroke
        left, right = x - facing * height * 0.16, x + facing * height * 0.34
        pen.rect(
            (min(left, right), seat_y),
            (max(left, right), seat_y + height * 0.05),
            fill=(150, 110, 80),
            outline=ink,
            width=stroke * 0.6,
            radius=stroke,
        )
        for leg_x in (min(left, right) + height * 0.04, max(left, right) - height * 0.04):
            pen.line([(leg_x, seat_y + height * 0.05), (leg_x, canvas.ground_y)], ink, stroke * 0.8)
    shoulder = rel(posture.body_dx, 0.28)
    neck = rel(posture.body_dx, 0.24)
    head_r = height * 0.11
    head = rel(posture.body_dx + math.sin(math.radians(posture.head_tilt)) * 0.06, 0.12)

    # Legs.
    for leg, shin in (
        (posture.left_leg, posture.left_shin),
        (posture.right_leg, posture.right_shin),
    ):
        knee = rel(leg[0], 0.56 + leg[1])
        foot = rel(leg[0] + shin[0], 0.56 + leg[1] + shin[1])
        pen.line([hip, knee, foot], ink, stroke)
        toe = (foot[0] + facing * height * 0.06, foot[1])
        pen.line([foot, toe], ink, stroke)
    # Body.
    pen.line([hip, shoulder, neck], ink, stroke)
    # Arms.
    for arm, fore in (
        (posture.left_arm, posture.left_fore),
        (posture.right_arm, posture.right_fore),
    ):
        elbow = rel(posture.body_dx + arm[0], 0.28 + arm[1])
        hand = rel(posture.body_dx + arm[0] + fore[0], 0.28 + arm[1] + fore[1])
        pen.line([shoulder, elbow, hand], ink, stroke)
        pen.circle(hand, stroke * 0.9, fill=ink, outline=None)
    # Head and face.
    pen.circle(head, head_r, fill=(255, 250, 240), outline=ink, width=stroke)
    _draw_face(pen, head, head_r, actor.mood, ink=ink, mouth_open=posture.mouth_open, facing=facing)
    # Name, under the feet, on a small tag.
    label = actor.name
    size = height * 0.085
    width = pen.text_width(label, size=size) + height * 0.12
    tag_y = canvas.ground_y + height * 0.10
    pen.rect(
        (x - width / 2, tag_y - size * 0.75),
        (x + width / 2, tag_y + size * 0.75),
        fill=accent,
        outline=None,
        radius=size * 0.4,
    )
    pen.text((x, tag_y), label, size=size, colour=(255, 255, 255))


def _positions(count: int, canvas: Canvas) -> list[tuple[float, int]]:
    """Where each figure stands, and which way it faces."""
    w = canvas.width
    if count <= 1:
        return [(w * 0.5, 1)]
    if count == 2:
        return [(w * 0.30, 1), (w * 0.70, -1)]
    return [(w * 0.18, 1), (w * 0.50, 1), (w * 0.82, -1)]


# ── Props ─────────────────────────────────────────────────────────────────────

PropDrawer = Callable[[_Pen, Point, float, Palette, str | None, float], None]


def _prop_ball(pen: _Pen, c: Point, s: float, p: Palette, _label: str | None, t: float) -> None:
    cx, cy = c
    cy -= abs(math.sin(t * 4.0)) * s * 0.5
    pen.circle((cx, cy), s * 0.35, fill=(255, 255, 255), outline=p.ink, width=s * 0.04)
    pen.polygon(
        [
            (cx, cy - s * 0.14),
            (cx + s * 0.13, cy - s * 0.04),
            (cx + s * 0.08, cy + s * 0.12),
            (cx - s * 0.08, cy + s * 0.12),
            (cx - s * 0.13, cy - s * 0.04),
        ],
        fill=p.ink,
        outline=None,
    )


def _prop_trophy(pen: _Pen, c: Point, s: float, p: Palette, _label: str | None, _t: float) -> None:
    cx, cy = c
    gold = (232, 178, 40)
    pen.polygon(
        [
            (cx - s * 0.3, cy - s * 0.4),
            (cx + s * 0.3, cy - s * 0.4),
            (cx + s * 0.18, cy + s * 0.05),
            (cx - s * 0.18, cy + s * 0.05),
        ],
        fill=gold,
        outline=p.ink,
        width=s * 0.03,
    )
    pen.rect(
        (cx - s * 0.05, cy + s * 0.05),
        (cx + s * 0.05, cy + s * 0.3),
        fill=gold,
        outline=p.ink,
        width=s * 0.03,
    )
    pen.rect(
        (cx - s * 0.22, cy + s * 0.3),
        (cx + s * 0.22, cy + s * 0.42),
        fill=p.ink,
        outline=None,
        radius=s * 0.03,
    )
    pen.arc((cx - s * 0.36, cy - s * 0.22), s * 0.14, 90, 270, p.ink, s * 0.03)
    pen.arc((cx + s * 0.36, cy - s * 0.22), s * 0.14, 270, 90, p.ink, s * 0.03)


def _prop_medal(pen: _Pen, c: Point, s: float, p: Palette, _label: str | None, _t: float) -> None:
    cx, cy = c
    pen.polygon(
        [
            (cx - s * 0.12, cy - s * 0.42),
            (cx + s * 0.12, cy - s * 0.42),
            (cx + s * 0.06, cy - s * 0.08),
            (cx - s * 0.06, cy - s * 0.08),
        ],
        fill=p.accent,
        outline=None,
    )
    pen.circle((cx, cy + s * 0.12), s * 0.24, fill=(232, 178, 40), outline=p.ink, width=s * 0.03)
    pen.text((cx, cy + s * 0.12), "1", size=s * 0.26, colour=p.ink)


def _prop_sign(pen: _Pen, c: Point, s: float, p: Palette, label: str | None, _t: float) -> None:
    cx, cy = c
    text = label or "!"
    width = max(s * 0.7, pen.text_width(text, size=s * 0.2) + s * 0.24)
    pen.line([(cx, cy + s * 0.5), (cx, cy - s * 0.05)], p.ink, s * 0.04)
    pen.rect(
        (cx - width / 2, cy - s * 0.45),
        (cx + width / 2, cy - s * 0.02),
        fill=(255, 252, 240),
        outline=p.ink,
        width=s * 0.035,
        radius=s * 0.04,
    )
    pen.text((cx, cy - s * 0.235), text, size=s * 0.2, colour=p.ink)


def _prop_screen(pen: _Pen, c: Point, s: float, p: Palette, label: str | None, _t: float) -> None:
    cx, cy = c
    pen.rect(
        (cx - s * 0.45, cy - s * 0.35),
        (cx + s * 0.45, cy + s * 0.25),
        fill=(24, 28, 40),
        outline=p.ink,
        width=s * 0.035,
        radius=s * 0.05,
    )
    pen.rect(
        (cx - s * 0.12, cy + s * 0.25), (cx + s * 0.12, cy + s * 0.34), fill=p.ink, outline=None
    )
    pen.line([(cx - s * 0.3, cy + s * 0.4), (cx + s * 0.3, cy + s * 0.4)], p.ink, s * 0.04)
    pen.text((cx, cy - s * 0.05), label or "•••", size=s * 0.18, colour=(140, 220, 255))


def _chart(pen: _Pen, c: Point, s: float, p: Palette, up: bool, t: float) -> None:
    cx, cy = c
    pen.line(
        [
            (cx - s * 0.4, cy - s * 0.4),
            (cx - s * 0.4, cy + s * 0.35),
            (cx + s * 0.45, cy + s * 0.35),
        ],
        p.ink,
        s * 0.035,
    )
    grow = min(1.0, 0.3 + t * 0.7)
    xs = [cx - s * 0.32 + s * 0.7 * k / 4 for k in range(5)]
    heights = [0.1, 0.25, 0.2, 0.45, 0.65] if up else [0.65, 0.5, 0.55, 0.3, 0.1]
    points = [(x, cy + s * 0.3 - s * h * grow) for x, h in zip(xs, heights, strict=True)]
    colour = (40, 160, 90) if up else (210, 60, 60)
    pen.line(points, colour, s * 0.05)
    tip = points[-1]
    d = -1 if up else 1
    pen.polygon(
        [tip, (tip[0] - s * 0.1, tip[1] - d * s * 0.02), (tip[0] - s * 0.02, tip[1] - d * s * 0.1)],
        fill=colour,
        outline=None,
    )


def _prop_chart_up(pen: _Pen, c: Point, s: float, p: Palette, _l: str | None, t: float) -> None:
    _chart(pen, c, s, p, True, t)


def _prop_chart_down(pen: _Pen, c: Point, s: float, p: Palette, _l: str | None, t: float) -> None:
    _chart(pen, c, s, p, False, t)


def _prop_building(pen: _Pen, c: Point, s: float, p: Palette, _l: str | None, _t: float) -> None:
    cx, cy = c
    pen.rect(
        (cx - s * 0.3, cy - s * 0.5),
        (cx + s * 0.3, cy + s * 0.5),
        fill=(214, 220, 230),
        outline=p.ink,
        width=s * 0.035,
    )
    for row in range(4):
        for col in range(3):
            wx = cx - s * 0.2 + col * s * 0.2
            wy = cy - s * 0.4 + row * s * 0.22
            pen.rect(
                (wx - s * 0.05, wy - s * 0.06),
                (wx + s * 0.05, wy + s * 0.06),
                fill=(255, 236, 150),
                outline=p.ink,
                width=s * 0.02,
            )
    pen.rect((cx - s * 0.07, cy + s * 0.3), (cx + s * 0.07, cy + s * 0.5), fill=p.ink, outline=None)


def _prop_house(pen: _Pen, c: Point, s: float, p: Palette, _l: str | None, _t: float) -> None:
    cx, cy = c
    pen.rect(
        (cx - s * 0.32, cy - s * 0.05),
        (cx + s * 0.32, cy + s * 0.45),
        fill=(246, 226, 190),
        outline=p.ink,
        width=s * 0.035,
    )
    pen.polygon(
        [(cx - s * 0.4, cy - s * 0.05), (cx, cy - s * 0.42), (cx + s * 0.4, cy - s * 0.05)],
        fill=(190, 80, 60),
        outline=p.ink,
        width=s * 0.035,
    )
    pen.rect(
        (cx - s * 0.07, cy + s * 0.18), (cx + s * 0.07, cy + s * 0.45), fill=p.ink, outline=None
    )
    pen.rect(
        (cx + s * 0.12, cy + s * 0.05),
        (cx + s * 0.24, cy + s * 0.17),
        fill=(190, 230, 255),
        outline=p.ink,
        width=s * 0.02,
    )


def _prop_car(pen: _Pen, c: Point, s: float, p: Palette, _l: str | None, t: float) -> None:
    cx, cy = c
    cy += math.sin(t * 12) * s * 0.01
    pen.rect(
        (cx - s * 0.5, cy),
        (cx + s * 0.5, cy + s * 0.25),
        fill=p.accent,
        outline=p.ink,
        width=s * 0.035,
        radius=s * 0.06,
    )
    pen.polygon(
        [
            (cx - s * 0.3, cy),
            (cx - s * 0.18, cy - s * 0.22),
            (cx + s * 0.2, cy - s * 0.22),
            (cx + s * 0.32, cy),
        ],
        fill=p.accent,
        outline=p.ink,
        width=s * 0.035,
    )
    pen.rect(
        (cx - s * 0.15, cy - s * 0.18),
        (cx + s * 0.16, cy - s * 0.02),
        fill=(200, 235, 255),
        outline=None,
    )
    for wx in (cx - s * 0.28, cx + s * 0.28):
        pen.circle((wx, cy + s * 0.25), s * 0.11, fill=p.ink, outline=None)
        pen.circle((wx, cy + s * 0.25), s * 0.05, fill=(230, 230, 230), outline=None)


def _prop_plane(pen: _Pen, c: Point, s: float, p: Palette, _l: str | None, t: float) -> None:
    cx, cy = c
    cy += math.sin(t * 2) * s * 0.06
    pen.polygon(
        [
            (cx - s * 0.5, cy),
            (cx + s * 0.4, cy - s * 0.1),
            (cx + s * 0.5, cy),
            (cx + s * 0.4, cy + s * 0.1),
        ],
        fill=(236, 240, 246),
        outline=p.ink,
        width=s * 0.03,
    )
    pen.polygon(
        [(cx - s * 0.05, cy), (cx + s * 0.15, cy - s * 0.02), (cx - s * 0.1, cy - s * 0.32)],
        fill=p.accent,
        outline=p.ink,
        width=s * 0.03,
    )
    pen.polygon(
        [(cx - s * 0.05, cy), (cx + s * 0.15, cy + s * 0.02), (cx - s * 0.1, cy + s * 0.32)],
        fill=p.accent,
        outline=p.ink,
        width=s * 0.03,
    )
    pen.polygon(
        [(cx - s * 0.5, cy), (cx - s * 0.35, cy - s * 0.02), (cx - s * 0.45, cy - s * 0.2)],
        fill=p.accent,
        outline=p.ink,
        width=s * 0.03,
    )


def _prop_boat(pen: _Pen, c: Point, s: float, p: Palette, _l: str | None, t: float) -> None:
    cx, cy = c
    cy += math.sin(t * 2.5) * s * 0.04
    pen.polygon(
        [
            (cx - s * 0.5, cy),
            (cx + s * 0.5, cy),
            (cx + s * 0.35, cy + s * 0.25),
            (cx - s * 0.35, cy + s * 0.25),
        ],
        fill=(150, 90, 60),
        outline=p.ink,
        width=s * 0.03,
    )
    pen.line([(cx, cy), (cx, cy - s * 0.55)], p.ink, s * 0.04)
    pen.polygon(
        [
            (cx + s * 0.02, cy - s * 0.52),
            (cx + s * 0.4, cy - s * 0.06),
            (cx + s * 0.02, cy - s * 0.06),
        ],
        fill=(255, 255, 255),
        outline=p.ink,
        width=s * 0.03,
    )


def _prop_phone(pen: _Pen, c: Point, s: float, p: Palette, label: str | None, t: float) -> None:
    cx, cy = c
    pen.rect(
        (cx - s * 0.18, cy - s * 0.38),
        (cx + s * 0.18, cy + s * 0.38),
        fill=p.ink,
        outline=None,
        radius=s * 0.06,
    )
    pen.rect(
        (cx - s * 0.15, cy - s * 0.33),
        (cx + s * 0.15, cy + s * 0.31),
        fill=(230, 240, 250),
        outline=None,
        radius=s * 0.03,
    )
    if label:
        pen.text((cx, cy - s * 0.02), label, size=s * 0.11, colour=p.ink)
    ring = 0.5 + 0.5 * math.sin(t * 6)
    pen.arc((cx + s * 0.3, cy - s * 0.4), s * 0.12 * (1 + ring * 0.6), 300, 30, p.accent, s * 0.03)


def _prop_money(pen: _Pen, c: Point, s: float, p: Palette, _l: str | None, t: float) -> None:
    cx, cy = c
    green = (70, 150, 90)
    for k in range(3):
        oy = -k * s * 0.08 - abs(math.sin(t * 3 + k)) * s * 0.03
        pen.rect(
            (cx - s * 0.4, cy - s * 0.18 + oy),
            (cx + s * 0.4, cy + s * 0.18 + oy),
            fill=green,
            outline=p.ink,
            width=s * 0.03,
            radius=s * 0.03,
        )
    pen.circle((cx, cy - s * 0.16), s * 0.12, fill=(200, 235, 200), outline=p.ink, width=s * 0.02)
    pen.text((cx, cy - s * 0.16), "$", size=s * 0.18, colour=p.ink)


def _prop_heart(pen: _Pen, c: Point, s: float, p: Palette, _l: str | None, t: float) -> None:
    cx, cy = c
    beat = 1.0 + 0.08 * max(0.0, math.sin(t * 5))
    r = s * 0.2 * beat
    red = (225, 60, 80)
    pen.circle((cx - r * 0.9, cy - r * 0.4), r, fill=red, outline=None)
    pen.circle((cx + r * 0.9, cy - r * 0.4), r, fill=red, outline=None)
    pen.polygon(
        [(cx - r * 1.85, cy - r * 0.2), (cx + r * 1.85, cy - r * 0.2), (cx, cy + r * 1.7)],
        fill=red,
        outline=None,
    )


def _prop_question(pen: _Pen, c: Point, s: float, p: Palette, _l: str | None, t: float) -> None:
    cx, cy = c
    cy += math.sin(t * 3) * s * 0.04
    pen.circle((cx, cy), s * 0.36, fill=(255, 255, 255), outline=p.ink, width=s * 0.035)
    pen.text((cx, cy), "?", size=s * 0.5, colour=p.accent)


def _prop_star(pen: _Pen, c: Point, s: float, p: Palette, _l: str | None, t: float) -> None:
    cx, cy = c
    spin = t * 40
    points: list[Point] = []
    for k in range(10):
        r = s * (0.42 if k % 2 == 0 else 0.18)
        a = math.radians(-90 + k * 36 + spin)
        points.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    pen.polygon(points, fill=(250, 200, 50), outline=p.ink, width=s * 0.03)


def _prop_flag(pen: _Pen, c: Point, s: float, p: Palette, label: str | None, t: float) -> None:
    cx, cy = c
    pen.line([(cx - s * 0.35, cy + s * 0.5), (cx - s * 0.35, cy - s * 0.5)], p.ink, s * 0.04)
    wave = math.sin(t * 5) * s * 0.05
    pen.polygon(
        [
            (cx - s * 0.33, cy - s * 0.5),
            (cx + s * 0.4, cy - s * 0.45 + wave),
            (cx + s * 0.4, cy - s * 0.05 + wave),
            (cx - s * 0.33, cy - s * 0.1),
        ],
        fill=p.accent,
        outline=p.ink,
        width=s * 0.03,
    )
    if label:
        pen.text(
            (cx + s * 0.04, cy - s * 0.28 + wave / 2), label, size=s * 0.14, colour=(255, 255, 255)
        )


def _prop_globe(pen: _Pen, c: Point, s: float, p: Palette, _l: str | None, t: float) -> None:
    cx, cy = c
    pen.circle((cx, cy), s * 0.4, fill=(120, 180, 240), outline=p.ink, width=s * 0.035)
    shift = (t * 30) % 60
    for k in range(-1, 3):
        x = cx - s * 0.4 + ((k * 20 + shift) / 60) * s * 0.8
        pen.arc((x, cy), s * 0.16, 0, 360, (80, 150, 90), s * 0.08)
    pen.arc((cx, cy), s * 0.4, 0, 360, p.ink, s * 0.035)


def _prop_clock(pen: _Pen, c: Point, s: float, p: Palette, _l: str | None, t: float) -> None:
    cx, cy = c
    pen.circle((cx, cy), s * 0.4, fill=(255, 255, 255), outline=p.ink, width=s * 0.04)
    for k in range(12):
        a = math.radians(k * 30)
        pen.line(
            [
                (cx + s * 0.32 * math.sin(a), cy - s * 0.32 * math.cos(a)),
                (cx + s * 0.36 * math.sin(a), cy - s * 0.36 * math.cos(a)),
            ],
            p.ink,
            s * 0.02,
        )
    minute = math.radians(t * 60)
    hour = math.radians(t * 5 + 60)
    pen.line(
        [(cx, cy), (cx + s * 0.3 * math.sin(minute), cy - s * 0.3 * math.cos(minute))],
        p.ink,
        s * 0.03,
    )
    pen.line(
        [(cx, cy), (cx + s * 0.2 * math.sin(hour), cy - s * 0.2 * math.cos(hour))], p.ink, s * 0.045
    )


def _prop_microphone(pen: _Pen, c: Point, s: float, p: Palette, _l: str | None, _t: float) -> None:
    cx, cy = c
    pen.rect(
        (cx - s * 0.12, cy - s * 0.45),
        (cx + s * 0.12, cy - s * 0.05),
        fill=(90, 90, 100),
        outline=p.ink,
        width=s * 0.03,
        radius=s * 0.12,
    )
    pen.arc((cx, cy - s * 0.15), s * 0.2, 0, 180, p.ink, s * 0.03)
    pen.line(
        [(cx, cy + s * 0.05), (cx, cy + s * 0.35), (cx - s * 0.15, cy + s * 0.35)], p.ink, s * 0.03
    )
    pen.line([(cx, cy + s * 0.35), (cx + s * 0.15, cy + s * 0.35)], p.ink, s * 0.03)


def _prop_book(pen: _Pen, c: Point, s: float, p: Palette, _l: str | None, _t: float) -> None:
    cx, cy = c
    for side in (-1, 1):
        pen.polygon(
            [
                (cx, cy - s * 0.25),
                (cx + side * s * 0.4, cy - s * 0.32),
                (cx + side * s * 0.4, cy + s * 0.25),
                (cx, cy + s * 0.32),
            ],
            fill=(255, 250, 235),
            outline=p.ink,
            width=s * 0.03,
        )
        for row in range(3):
            y = cy - s * 0.12 + row * s * 0.14
            pen.line(
                [(cx + side * s * 0.08, y), (cx + side * s * 0.32, y - side * s * 0.015)],
                (170, 170, 170),
                s * 0.015,
            )


def _prop_podium(pen: _Pen, c: Point, s: float, p: Palette, label: str | None, _t: float) -> None:
    cx, cy = c
    pen.polygon(
        [
            (cx - s * 0.3, cy - s * 0.1),
            (cx + s * 0.3, cy - s * 0.1),
            (cx + s * 0.38, cy + s * 0.5),
            (cx - s * 0.38, cy + s * 0.5),
        ],
        fill=(150, 110, 80),
        outline=p.ink,
        width=s * 0.03,
    )
    pen.rect(
        (cx - s * 0.36, cy - s * 0.2),
        (cx + s * 0.36, cy - s * 0.08),
        fill=(120, 85, 60),
        outline=p.ink,
        width=s * 0.03,
    )
    if label:
        pen.text((cx, cy + s * 0.2), label, size=s * 0.14, colour=(255, 255, 255))


def _prop_table(pen: _Pen, c: Point, s: float, p: Palette, _l: str | None, _t: float) -> None:
    cx, cy = c
    pen.rect(
        (cx - s * 0.5, cy),
        (cx + s * 0.5, cy + s * 0.07),
        fill=(160, 120, 80),
        outline=p.ink,
        width=s * 0.03,
    )
    for lx in (cx - s * 0.42, cx + s * 0.42):
        pen.line([(lx, cy + s * 0.07), (lx, cy + s * 0.5)], p.ink, s * 0.04)


def _prop_camera(pen: _Pen, c: Point, s: float, p: Palette, _l: str | None, t: float) -> None:
    cx, cy = c
    pen.rect(
        (cx - s * 0.4, cy - s * 0.22),
        (cx + s * 0.4, cy + s * 0.25),
        fill=(60, 60, 70),
        outline=p.ink,
        width=s * 0.03,
        radius=s * 0.05,
    )
    pen.circle((cx, cy), s * 0.18, fill=(30, 30, 40), outline=(200, 200, 210), width=s * 0.03)
    pen.circle((cx, cy), s * 0.08, fill=(90, 150, 220), outline=None)
    blink = (t * 2) % 1 < 0.5
    pen.circle(
        (cx + s * 0.3, cy - s * 0.14),
        s * 0.04,
        fill=(240, 60, 60) if blink else (120, 40, 40),
        outline=None,
    )


def _prop_music_note(pen: _Pen, c: Point, s: float, p: Palette, _l: str | None, t: float) -> None:
    cx, cy = c
    cy += math.sin(t * 4) * s * 0.06
    pen.circle((cx - s * 0.18, cy + s * 0.25), s * 0.12, fill=p.ink, outline=None)
    pen.circle((cx + s * 0.18, cy + s * 0.32), s * 0.12, fill=p.ink, outline=None)
    pen.line(
        [
            (cx - s * 0.07, cy + s * 0.25),
            (cx - s * 0.07, cy - s * 0.35),
            (cx + s * 0.29, cy - s * 0.42),
            (cx + s * 0.29, cy + s * 0.32),
        ],
        p.ink,
        s * 0.04,
    )
    pen.line([(cx - s * 0.07, cy - s * 0.35), (cx + s * 0.29, cy - s * 0.42)], p.ink, s * 0.08)


def _prop_food(pen: _Pen, c: Point, s: float, p: Palette, _l: str | None, _t: float) -> None:
    cx, cy = c
    pen.rect(
        (cx - s * 0.36, cy - s * 0.3),
        (cx + s * 0.36, cy - s * 0.05),
        fill=(240, 190, 110),
        outline=p.ink,
        width=s * 0.03,
        radius=s * 0.16,
    )
    pen.rect(
        (cx - s * 0.38, cy - s * 0.06),
        (cx + s * 0.38, cy + s * 0.04),
        fill=(120, 190, 80),
        outline=p.ink,
        width=s * 0.02,
    )
    pen.rect(
        (cx - s * 0.36, cy + s * 0.03),
        (cx + s * 0.36, cy + s * 0.15),
        fill=(120, 70, 40),
        outline=p.ink,
        width=s * 0.02,
    )
    pen.rect(
        (cx - s * 0.36, cy + s * 0.14),
        (cx + s * 0.36, cy + s * 0.3),
        fill=(240, 190, 110),
        outline=p.ink,
        width=s * 0.03,
        radius=s * 0.08,
    )


def _prop_sun(pen: _Pen, c: Point, s: float, p: Palette, _l: str | None, t: float) -> None:
    cx, cy = c
    for k in range(8):
        a = math.radians(k * 45 + t * 10)
        pen.line(
            [
                (cx + s * 0.32 * math.cos(a), cy + s * 0.32 * math.sin(a)),
                (cx + s * 0.46 * math.cos(a), cy + s * 0.46 * math.sin(a)),
            ],
            (245, 190, 60),
            s * 0.04,
        )
    pen.circle((cx, cy), s * 0.26, fill=(255, 215, 80), outline=p.ink, width=s * 0.03)


def _prop_cloud(pen: _Pen, c: Point, s: float, p: Palette, _l: str | None, t: float) -> None:
    cx, cy = c
    cx += math.sin(t * 0.8) * s * 0.06
    for dx, dy, r in ((-0.22, 0.05, 0.18), (0.0, -0.08, 0.24), (0.22, 0.05, 0.18)):
        pen.circle((cx + s * dx, cy + s * dy), s * r, fill=(250, 250, 252), outline=None)
    pen.rect(
        (cx - s * 0.38, cy + s * 0.02),
        (cx + s * 0.38, cy + s * 0.2),
        fill=(250, 250, 252),
        outline=None,
        radius=s * 0.08,
    )
    pen.arc((cx, cy), s * 0.38, 0, 360, p.ink, s * 0.0)


def _prop_lightning(pen: _Pen, c: Point, s: float, p: Palette, _l: str | None, t: float) -> None:
    cx, cy = c
    flash = (t * 3) % 1 < 0.7
    colour = (255, 220, 60) if flash else (230, 200, 90)
    pen.polygon(
        [
            (cx + s * 0.1, cy - s * 0.5),
            (cx - s * 0.2, cy + s * 0.05),
            (cx + s * 0.02, cy + s * 0.05),
            (cx - s * 0.1, cy + s * 0.5),
            (cx + s * 0.25, cy - s * 0.1),
            (cx + s * 0.04, cy - s * 0.1),
        ],
        fill=colour,
        outline=p.ink,
        width=s * 0.03,
    )


_PROPS: dict[StickProp, PropDrawer] = {
    StickProp.BALL: _prop_ball,
    StickProp.TROPHY: _prop_trophy,
    StickProp.MEDAL: _prop_medal,
    StickProp.SIGN: _prop_sign,
    StickProp.SCREEN: _prop_screen,
    StickProp.CHART_UP: _prop_chart_up,
    StickProp.CHART_DOWN: _prop_chart_down,
    StickProp.BUILDING: _prop_building,
    StickProp.HOUSE: _prop_house,
    StickProp.CAR: _prop_car,
    StickProp.PLANE: _prop_plane,
    StickProp.BOAT: _prop_boat,
    StickProp.PHONE: _prop_phone,
    StickProp.MONEY: _prop_money,
    StickProp.HEART: _prop_heart,
    StickProp.QUESTION: _prop_question,
    StickProp.STAR: _prop_star,
    StickProp.FLAG: _prop_flag,
    StickProp.GLOBE: _prop_globe,
    StickProp.CLOCK: _prop_clock,
    StickProp.MICROPHONE: _prop_microphone,
    StickProp.BOOK: _prop_book,
    StickProp.PODIUM: _prop_podium,
    StickProp.TABLE: _prop_table,
    StickProp.CAMERA: _prop_camera,
    StickProp.MUSIC_NOTE: _prop_music_note,
    StickProp.FOOD: _prop_food,
    StickProp.SUN: _prop_sun,
    StickProp.CLOUD: _prop_cloud,
    StickProp.LIGHTNING: _prop_lightning,
}

#: Props that live in the sky rather than on the ground.
_AIRBORNE = frozenset(
    {
        StickProp.SUN,
        StickProp.CLOUD,
        StickProp.PLANE,
        StickProp.LIGHTNING,
        StickProp.STAR,
        StickProp.QUESTION,
        StickProp.HEART,
        StickProp.MUSIC_NOTE,
    }
)
#: Props that stand on the ground behind the figures, drawn first.
_BACKDROP = frozenset(
    {
        StickProp.BUILDING,
        StickProp.HOUSE,
        StickProp.PODIUM,
        StickProp.TABLE,
        StickProp.SCREEN,
        StickProp.SIGN,
    }
)


def _prop_spots(
    props: Sequence[StickProp], canvas: Canvas, actors: int
) -> list[tuple[StickProp, Point, float]]:
    """Where each prop goes: skies above, backdrops behind, the rest beside the figures."""
    w, h = canvas.width, canvas.height
    size = h * 0.16
    spots: list[tuple[StickProp, Point, float]] = []
    sky = [p for p in props if p in _AIRBORNE]
    back = [p for p in props if p in _BACKDROP]
    hand = [p for p in props if p not in _AIRBORNE and p not in _BACKDROP]
    sky_x = (
        [w * 0.5]
        if len(sky) == 1
        else [w * 0.28, w * 0.72]
        if len(sky) == 2
        else [w * 0.2, w * 0.5, w * 0.8]
    )
    for prop, x in zip(sky, sky_x, strict=False):
        spots.append((prop, (x, h * 0.17), size))
    back_x = [w * 0.5] if actors == 0 else [w * 0.82] if actors < 3 else [w * 0.5]
    if len(back) > 1:
        back_x = [w * 0.15, w * 0.85, w * 0.5][: len(back)]
    for prop, x in zip(back, back_x, strict=False):
        spots.append((prop, (x, canvas.ground_y - size * 0.5), size * 1.4))
    ground_x = [w * 0.5] if actors == 0 else [w * 0.85] if actors == 1 else [w * 0.5]
    if len(hand) > 1:
        ground_x = (
            [w * 0.5, w * 0.15, w * 0.85][: len(hand)]
            if actors == 2
            else [w * 0.15, w * 0.85, w * 0.5][: len(hand)]
        )
    for prop, x in zip(hand, ground_x, strict=False):
        spots.append((prop, (x, canvas.ground_y - size * 0.45), size))
    return spots


# ── Frames ────────────────────────────────────────────────────────────────────


def _background(
    image: Image.Image, palette: Palette, canvas: Canvas, scale: float, rng: random.Random
) -> None:
    draw = ImageDraw.Draw(image)
    w = round(canvas.width * scale)
    h = round(canvas.height * scale)
    ground = round(canvas.ground_y * scale)
    top, bottom = palette.sky_top, palette.sky_bottom
    for y in range(0, ground, max(1, round(4 * scale))):
        k = y / max(1, ground)
        colour = tuple(round(top[i] + (bottom[i] - top[i]) * k) for i in range(3))
        draw.rectangle([0, y, w, y + round(4 * scale)], fill=colour)
    draw.rectangle([0, ground, w, h], fill=palette.ground)
    # A hint of horizon.
    draw.line([(0, ground), (w, ground)], fill=palette.ink, width=max(1, round(3 * scale)))
    # A few distant hills, placed by the seed so scenes differ but frames do not.
    for _ in range(3):
        cx = rng.uniform(0.1, 0.9) * w
        r = rng.uniform(0.18, 0.4) * w
        shade = tuple(max(0, c - 18) for c in palette.ground)
        draw.ellipse([cx - r, ground - r * 0.32, cx + r, ground + r * 0.32], fill=shade)
    draw.rectangle([0, ground + max(1, round(2 * scale)), w, h], fill=palette.ground)


def draw_frame(
    scene: TimedScene,
    t: float,
    *,
    seed: int = 0,
    canvas: Canvas | None = None,
    supersample: int = _SUPERSAMPLE,
) -> Image.Image:
    """One frame of a scene, `t` seconds into it, at the canvas size."""
    canvas = canvas or Canvas()
    palette = palette_for(scene.mood)
    scale = float(supersample)
    image = Image.new("RGB", (round(canvas.width * scale), round(canvas.height * scale)))
    rng = random.Random(seed * 1000 + scene.index)  # noqa: S311 - placement, not security
    _background(image, palette, canvas, scale, rng)

    progress = 0.0 if scene.duration_sec <= 0 else min(1.0, max(0.0, t / scene.duration_sec))
    zoom = 1.0 + _ZOOM_PER_SCENE * progress
    pan = (canvas.width * 0.015) * progress * (1 if scene.index % 2 == 0 else -1)
    pen = _Pen(image, canvas=canvas, scale=scale, zoom=zoom, pan=pan)

    actors = list(scene.actors)[:3]
    spots = _prop_spots(list(scene.props)[:3], canvas, len(actors))
    for prop, centre, size in spots:
        if prop in _BACKDROP or prop in _AIRBORNE:
            _PROPS[prop](pen, centre, size, palette, scene.label, t)
    phase = t * 2 * math.pi * 1.1
    for actor, (x, facing) in zip(actors, _positions(len(actors), canvas), strict=False):
        _draw_figure(
            pen,
            canvas,
            actor,
            x=x,
            facing=facing,
            phase=phase,
            t=t,
            ink=palette.ink,
            accent=palette.accent,
        )
    for prop, centre, size in spots:
        if prop not in _BACKDROP and prop not in _AIRBORNE:
            _PROPS[prop](pen, centre, size, palette, scene.label, t)

    if supersample != 1:
        image = image.resize((canvas.width, canvas.height), Image.Resampling.LANCZOS)
    return image


def frame_count(duration_sec: float, fps: int) -> int:
    return max(1, round(duration_sec * fps))


def render_scene(
    scene: TimedScene,
    destination: Path,
    *,
    profile: RenderProfile,
    seed: int = 0,
    fps: int = DEFAULT_FPS,
    supersample: int = _SUPERSAMPLE,
    encoder: str = "h264_nvenc",
    ffmpeg_bin: str = "ffmpeg",
    timeout_s: float = 900.0,
    on_progress: Callable[[int, int], None] | None = None,
) -> AssembledClip:
    """Draw every frame of a scene and encode them as a silent segment.

    Silent audio rather than none, so the segment concatenates with a title
    card and a narration can be laid over the whole. Frames go to ffmpeg over
    a pipe: a 45-second video is over a thousand of them, and writing each as
    a file would spend longer on disk than on drawing.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_suffix(destination.suffix + ".partial")
    total = frame_count(scene.duration_sec, fps)
    canvas = Canvas()
    argv = [
        ffmpeg_bin,
        "-y",
        "-v",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{canvas.width}x{canvas.height}",
        "-r",
        str(fps),
        "-i",
        "-",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=48000:cl=stereo",
        "-t",
        f"{total / fps:.3f}",
        "-shortest",
        "-r",
        str(fps),
        *output_args(profile, encoder),
        str(staging),
    ]
    log.info(
        "cartoon.render", scene=scene.index, frames=total, seconds=round(scene.duration_sec, 2)
    )
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    assert process.stdin is not None  # noqa: S101 - Popen with PIPE guarantees it
    try:
        for index in range(total):
            frame = draw_frame(
                scene, index / fps, seed=seed, canvas=canvas, supersample=supersample
            )
            process.stdin.write(frame.tobytes())
            if on_progress is not None and index % fps == 0:
                on_progress(index, total)
        process.stdin.close()
        _, stderr = process.communicate(timeout=timeout_s)
    except BrokenPipeError as exc:
        _, stderr = process.communicate(timeout=30)
        raise RenderError(
            f"ffmpeg stopped taking frames: {stderr.decode(errors='replace')[-800:]}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        process.kill()
        raise RenderError(
            f"encoding scene {scene.index} took longer than {timeout_s:.0f}s"
        ) from exc
    if process.returncode != 0:
        raise RenderError(
            f"ffmpeg failed encoding scene {scene.index}: {stderr.decode(errors='replace')[-800:]}"
        )
    staging.replace(destination)
    return AssembledClip(
        path=destination,
        duration_sec=total / fps,
        size_bytes=destination.stat().st_size,
        width=canvas.width,
        height=canvas.height,
    )
