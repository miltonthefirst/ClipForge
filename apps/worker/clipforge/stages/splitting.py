"""Splitting one clip into several: what to cut, and what each part inherits.

A remake applies every other correction first and cuts the finished file, so a
part keeps the framing, captions, voice and music of the whole. Nothing here
touches a store or the disk, so the arithmetic — which is where a split goes
wrong — is one testable thing rather than a branch inside an I/O path.

## Two timelines

The reviewer names parts in seconds on the clip as they watched it, because
that is the only timeline they can see. The file being cut starts wherever the
remake's window starts, which is earlier when a part reaches before the clip
does. `SplitPlan` says how far to widen the window; the stage converts each part
to the file's own seconds once it knows where the window actually landed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from clipforge_contracts import AppliedMusic, AppliedVoice, PanKeyframe, SplitRange

__all__ = [
    "MAX_PARTS",
    "MIN_PART_SEC",
    "Part",
    "SplitError",
    "SplitPlan",
    "clock",
    "part_music",
    "part_title",
    "part_voice",
    "plan_split",
    "slice_keyframes",
]

#: Each part is an encode, a poster and an upload. firestore.rules caps the list
#: at the same number, so a request over it never reaches the worker from the app.
MAX_PARTS = 20
#: The same floor `RemakeStage._cut` puts under a whole remake.
MIN_PART_SEC = 1.0
#: How far a part may reach outside the clip: the reach of a trim.
REACH_SEC = 30.0
#: Tolerances for a boundary that was meant to be the clip's own edge. Wider
#: outward, because a part that pokes past the end by a fraction of a second is
#: a rounded duration — the note reader is told "50.7 seconds" and answers 51 —
#: and taking it literally would re-cut from the source for 0.3 s of footage.
_SNAP_OUT_SEC = 1.0
_SNAP_IN_SEC = 0.5


class SplitError(ValueError):
    """The parts cannot be cut as asked. Phrased for the person who asked."""


@dataclass(frozen=True)
class Part:
    """One part, in seconds on the clip as the reviewer watched it."""

    index: int
    count: int
    start_sec: float
    end_sec: float

    @property
    def duration_sec(self) -> float:
        return self.end_sec - self.start_sec


@dataclass(frozen=True)
class SplitPlan:
    """The parts, in order, and the trim that makes the window cover them all.

    The deltas never shrink the window. Parts inside the clip leave it exactly
    as it is, which is what lets a split with nothing else in it copy the
    reviewed file instead of re-cutting from a source that may be gone.
    """

    parts: tuple[Part, ...]
    start_delta_sec: float
    end_delta_sec: float

    @property
    def several(self) -> bool:
        """Two or more clips. One range is the same clip made shorter."""
        return len(self.parts) > 1


def clock(seconds: float) -> str:
    """0:35, 1:02.5 — how a person reads a time on a clip."""
    sign = "-" if seconds < 0 else ""
    tenths_total = round(abs(seconds) * 10)
    minutes, tenths_left = divmod(tenths_total, 600)
    whole, tenths = divmod(tenths_left, 10)
    text = f"{minutes}:{whole:02d}"
    return sign + (f"{text}.{tenths}" if tenths else text)


def plan_split(ranges: Sequence[SplitRange], *, clip_sec: float) -> SplitPlan:
    """Check the parts against a clip `clip_sec` long, and order them.

    Overlaps and gaps are allowed: a reviewer who wants the same moment to end
    one part and open the next, or wants a dull middle left out of both, is
    asking for something legitimate. What is refused is a part too short to be
    a clip, or one reaching further outside the clip than a trim could.
    """
    if not ranges:
        raise SplitError("no parts were given")
    if len(ranges) > MAX_PARTS:
        raise SplitError(
            f"a clip can be split into at most {MAX_PARTS} parts, and {len(ranges)} were asked for"
        )

    snapped: list[tuple[float, float]] = []
    for asked in ranges:
        start = _snap(float(asked.start_sec), clip_sec, start=True)
        end = _snap(float(asked.end_sec), clip_sec, start=False)
        if end - start < MIN_PART_SEC:
            raise SplitError(
                f"the part from {clock(start)} to {clock(end)} is {max(0.0, end - start):.1f}s "
                f"long; each part needs at least {MIN_PART_SEC:g} second"
            )
        if start < -REACH_SEC:
            raise SplitError(
                f"the part starting at {clock(start)} begins more than {REACH_SEC:g} seconds "
                "before the clip does, which is as far as a cut can reach"
            )
        if end > clip_sec + REACH_SEC:
            raise SplitError(
                f"the part ending at {clock(end)} runs more than {REACH_SEC:g} seconds past "
                f"the end of the clip ({clock(clip_sec)}), which is as far as a cut can reach"
            )
        snapped.append((start, end))

    ordered = sorted(snapped)
    count = len(ordered)
    parts = tuple(
        Part(index=i + 1, count=count, start_sec=start, end_sec=end)
        for i, (start, end) in enumerate(ordered)
    )
    return SplitPlan(
        parts=parts,
        start_delta_sec=min(0.0, min(part.start_sec for part in parts)),
        end_delta_sec=max(0.0, max(part.end_sec for part in parts) - clip_sec),
    )


def _snap(value: float, clip_sec: float, *, start: bool) -> float:
    """Pull a boundary onto the clip's own edge when it was plainly meant to be one."""
    if -_SNAP_OUT_SEC <= value <= _SNAP_IN_SEC:
        return 0.0
    if clip_sec - _SNAP_IN_SEC <= value <= clip_sec + _SNAP_OUT_SEC and not start:
        return clip_sec
    return value


def slice_keyframes(
    keyframes: Sequence[PanKeyframe], start: float, end: float
) -> list[PanKeyframe]:
    """The window's path over one part, re-based to that part's own first frame.

    `PanKeyframe.atSec` is seconds from the start of the clip, so a part that
    kept the whole clip's points would replay the path from the wrong moment in
    any later correction of it. The position at each edge is interpolated, the
    way the renderer does between points, so the part starts where the picture
    actually was rather than at the nearest point the reviewer happened to set.
    """
    if not keyframes:
        return []
    ordered = sorted(keyframes, key=lambda k: k.at_sec)

    def x_at(at: float) -> float:
        if at <= ordered[0].at_sec:
            return ordered[0].x_pct
        for before, after in pairwise(ordered):
            if before.at_sec <= at <= after.at_sec:
                span = after.at_sec - before.at_sec
                if span <= 0:
                    return after.x_pct
                return before.x_pct + (after.x_pct - before.x_pct) * (at - before.at_sec) / span
        return ordered[-1].x_pct

    inside = [
        PanKeyframe(at_sec=round(k.at_sec - start, 3), x_pct=k.x_pct)
        for k in ordered
        if start < k.at_sec < end
    ]
    return [
        PanKeyframe(at_sec=0.0, x_pct=round(x_at(start), 3)),
        *inside,
        PanKeyframe(at_sec=round(end - start, 3), x_pct=round(x_at(end), 3)),
    ]


def part_title(title: str | None, part: Part) -> str | None:
    """The whole's title with the part's place in it, within the contract's 200."""
    if not title:
        return title
    suffix = f" (Part {part.index}/{part.count})"
    return title[: 200 - len(suffix)].rstrip() + suffix


def part_voice(voice: AppliedVoice | None) -> AppliedVoice | None:
    """The narration record for a part, without the whole's words and length.

    The spoken text is the whole narration, and a later correction of one part
    would reproduce all of it over that part's few seconds; without it the
    correction re-derives the words for the part's own window, as it does for a
    clip old enough to have none. The length would have the page warn that the
    narration overruns a picture it was never meant to fit.
    """
    if voice is None:
        return None
    return voice.model_copy(update={"spoken_text": None, "speech_duration_sec": None})


def part_music(music: AppliedMusic | None, offset_sec: float) -> AppliedMusic | None:
    """The soundtrack record for a part that begins `offset_sec` into the whole.

    The track is mixed into the file from `musicStartSec` onwards, so a part
    that starts later in the file starts later in the track too. Recorded so a
    correction of the part that re-cuts it puts back the same stretch of music.
    """
    if music is None or music.music_start_sec is None:
        return music
    return music.model_copy(
        update={"music_start_sec": round(music.music_start_sec + offset_sec, 3)}
    )
