"""Scenes of dialogue: what the model proposed, made drawable and placed in time.

Pure functions, because each decision here is quiet when wrong. A line given
to the wrong speaker is said in the wrong voice; a scene timed a second early
shows the trophy before the line about winning; a script a person wrote and
the model paraphrased is a lie spoken in their voice. None of those produce
an error, so each is pinned by a test instead.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from clipforge_contracts import (
    LlmScriptResponse,
    SceneMood,
    StickActor,
    StickMood,
    StickPose,
    StickProp,
    VoiceKind,
)

from clipforge.synth.voices import NARRATOR, is_narrator, voice_kind_for_name

__all__ = [
    "MAX_LINE_CHARS",
    "MAX_SCENES",
    "Line",
    "Palette",
    "SceneSpec",
    "TimedLine",
    "TimedScene",
    "cast_from_response",
    "full_script",
    "palette_for",
    "parse_dialogue",
    "scenes_from_response",
    "speaking_at",
    "split_sentences",
    "time_scenes",
    "word_count",
]

MAX_SCENES = 16
MAX_LINE_CHARS = 300
#: How early a scene appears before its first line is heard: a beat to look.
_LEAD_SEC = 0.25

_ABBREVIATIONS = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "dr",
        "prof",
        "sr",
        "jr",
        "st",
        "vs",
        "etc",
        "no",
        "u.s",
        "u.k",
        "e.g",
        "i.e",
    }
)
_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[\"'“(A-Z0-9])")
_WORD = re.compile(r"[A-Za-z0-9']+")
_SPEAKER = re.compile(r"^\s*([A-Za-z][^:\n]{0,39}?)\s*:\s*(\S.*)$")


@dataclass(frozen=True)
class Line:
    """One thing said, by one speaker."""

    speaker: str
    text: str


@dataclass(frozen=True)
class SceneSpec:
    """One scene, drawable: its dialogue and the picture the renderer will make of it."""

    index: int
    lines: tuple[Line, ...]
    actors: tuple[StickActor, ...]
    props: tuple[StickProp, ...]
    mood: SceneMood
    label: str | None = None


@dataclass(frozen=True)
class TimedLine(Line):
    """A line as spoken: in which voice, and when, in the whole narration's time."""

    scene: int = 0
    voice: str = ""
    start_sec: float = 0.0
    end_sec: float = 0.0


@dataclass(frozen=True)
class TimedScene(SceneSpec):
    """A scene with its place in the narration and its lines' places within it."""

    start_sec: float = 0.0
    end_sec: float = 0.0
    timed_lines: tuple[TimedLine, ...] = ()

    @property
    def duration_sec(self) -> float:
        return max(0.0, self.end_sec - self.start_sec)


@dataclass(frozen=True)
class Palette:
    """The colours of a scene, from its mood. RGB tuples."""

    sky_top: tuple[int, int, int]
    sky_bottom: tuple[int, int, int]
    ground: tuple[int, int, int]
    ink: tuple[int, int, int]
    accent: tuple[int, int, int]


_PALETTES: dict[SceneMood, Palette] = {
    SceneMood.CALM: Palette(
        (214, 232, 244), (240, 247, 251), (198, 214, 176), (28, 32, 44), (52, 120, 190)
    ),
    SceneMood.UPBEAT: Palette(
        (255, 236, 170), (255, 250, 225), (190, 224, 150), (30, 30, 40), (240, 130, 40)
    ),
    SceneMood.TENSE: Palette(
        (200, 196, 214), (236, 230, 240), (176, 168, 186), (34, 28, 48), (176, 48, 72)
    ),
    SceneMood.GLOOMY: Palette(
        (150, 160, 172), (196, 204, 212), (140, 150, 150), (30, 34, 40), (90, 100, 140)
    ),
    SceneMood.URGENT: Palette(
        (255, 214, 200), (255, 240, 232), (220, 190, 170), (40, 24, 24), (220, 50, 40)
    ),
}


def palette_for(mood: SceneMood) -> Palette:
    return _PALETTES[mood]


def word_count(text: str) -> int:
    return len(_WORD.findall(text))


def split_sentences(text: str) -> list[str]:
    """Sentences, for a passage too long to speak as one line.

    Splits on a full stop, question or exclamation mark followed by a space and
    a capital, so "3.5 million" stays whole. "Mr." and a fragment under four
    words join what *follows* them, and a short last fragment joins what came
    before.
    """
    cleaned = " ".join(text.split())
    if not cleaned:
        return []
    raw = [piece.strip() for piece in _BOUNDARY.split(cleaned) if piece.strip()]
    pieces: list[str] = []
    carry = ""
    for index, piece in enumerate(raw):
        piece = f"{carry} {piece}".strip() if carry else piece
        carry = ""
        last_word = piece.rstrip(".").rsplit(" ", 1)[-1].lower()
        if index < len(raw) - 1 and (last_word in _ABBREVIATIONS or word_count(piece) < 4):
            carry = piece
            continue
        pieces.append(piece)
    if carry:
        if pieces:
            pieces[-1] = f"{pieces[-1]} {carry}"
        else:
            pieces.append(carry)
    if len(pieces) > 1 and word_count(pieces[-1]) < 4:
        tail = pieces.pop()
        pieces[-1] = f"{pieces[-1]} {tail}"
    return pieces


def _fit(text: str) -> list[str]:
    """A line as one or more pieces the contract's length allows."""
    cleaned = " ".join(text.split())
    if len(cleaned) <= MAX_LINE_CHARS:
        return [cleaned] if cleaned else []
    pieces: list[str] = []
    current = ""
    for sentence in split_sentences(cleaned) or [cleaned]:
        if current and len(current) + 1 + len(sentence) > MAX_LINE_CHARS:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return [piece[:MAX_LINE_CHARS] for piece in pieces]


def parse_dialogue(text: str) -> list[Line]:
    """A script a person typed, as lines with speakers.

    One row per line, "Name: words". A row with no name continues the row
    before it — a paragraph is one speech — and, at the very start, is the
    narrator's. A speech too long to say as one line is split at sentences.
    """
    lines: list[Line] = []
    for raw in text.splitlines():
        row = raw.strip()
        if not row:
            continue
        matched = _SPEAKER.match(row)
        if matched:
            speaker = " ".join(matched.group(1).split())[:40]
            lines.append(Line(speaker=speaker, text=matched.group(2).strip()))
        elif lines:
            last = lines[-1]
            lines[-1] = Line(speaker=last.speaker, text=f"{last.text} {row}")
        else:
            lines.append(Line(speaker=NARRATOR, text=row))
    fitted: list[Line] = []
    for line in lines:
        fitted.extend(Line(speaker=line.speaker, text=piece) for piece in _fit(line.text))
    return fitted


def full_script(scenes: Sequence[SceneSpec]) -> str:
    """The dialogue as one text, a row per line, the form `parse_dialogue` reads."""
    return "\n".join(
        f"{line.speaker}: {line.text}" for scene in scenes for line in scene.lines if line.text
    )


def _actor(actor: StickActor) -> StickActor:
    return StickActor(
        name=" ".join(actor.name.split())[:40] or "someone",
        pose=actor.pose or StickPose.STAND,
        mood=actor.mood or StickMood.NEUTRAL,
    )


def actors_for(names: Sequence[str], staged: Sequence[StickActor]) -> tuple[StickActor, ...]:
    """The figures on screen: those the model staged, plus any speaker it forgot."""
    actors = [_actor(actor) for actor in staged[:3]]
    seen = {actor.name.casefold() for actor in actors}
    for name in names:
        key = name.casefold()
        if key in seen or is_narrator(name) or len(actors) >= 3:
            continue
        actors.append(StickActor(name=name[:40], pose=StickPose.TALK, mood=StickMood.NEUTRAL))
        seen.add(key)
    return tuple(actors)


def scenes_from_response(
    response: LlmScriptResponse, *, script: Sequence[Line] | None = None
) -> list[SceneSpec]:
    """What the model said, as scenes the renderer will accept.

    With a script given, the lines are the person's own and the model's words
    are ignored: it was asked to stage the dialogue, not to improve it. Its
    scenes are used for their grouping and their pictures — each takes as many
    of the person's lines as it proposed — and lines it left over become plain
    scenes of their own. Every speaker in a scene is on screen, whether or not
    the model remembered to draw them.
    """
    proposed = list(response.scenes)[:MAX_SCENES]
    scenes: list[SceneSpec] = []

    if script is not None:
        remaining = list(script)
        for source in proposed:
            if not remaining:
                break
            take = max(1, len(source.lines))
            chunk, remaining = remaining[:take], remaining[take:]
            scenes.append(_scene(len(scenes), chunk, source))
        while remaining and len(scenes) < MAX_SCENES:
            chunk, remaining = remaining[:2], remaining[2:]
            scenes.append(_scene(len(scenes), chunk, None))
        if remaining and scenes:
            last = scenes[-1]
            scenes[-1] = SceneSpec(
                index=last.index,
                lines=(*last.lines, *remaining),
                actors=actors_for([line.speaker for line in remaining], last.actors),
                props=last.props,
                mood=last.mood,
                label=last.label,
            )
        return scenes

    for source in proposed:
        lines = [
            Line(speaker=" ".join(line.speaker.split())[:40] or NARRATOR, text=piece)
            for line in source.lines
            for piece in _fit(line.text)
        ][:6]
        if not lines:
            continue
        scenes.append(_scene(len(scenes), lines, source))
    return scenes


def _scene(index: int, lines: Sequence[Line], source: object | None) -> SceneSpec:
    speakers = [line.speaker for line in lines]
    if source is None:
        return SceneSpec(
            index=index,
            lines=tuple(lines),
            actors=actors_for(speakers, ()),
            props=(),
            mood=SceneMood.CALM,
        )
    staged = getattr(source, "actors", None) or []
    props = tuple(dict.fromkeys(getattr(source, "props", None) or []))[:3]
    label = " ".join((getattr(source, "label", None) or "").split())[:24] or None
    if label and not any(prop in (StickProp.SIGN, StickProp.SCREEN) for prop in props):
        props = (*props[:2], StickProp.SIGN)
    return SceneSpec(
        index=index,
        lines=tuple(lines),
        actors=actors_for(speakers, staged),
        props=props,
        mood=getattr(source, "mood", None) or SceneMood.CALM,
        label=label,
    )


def cast_from_response(
    response: LlmScriptResponse, scenes: Sequence[SceneSpec]
) -> list[tuple[str, VoiceKind]]:
    """Who speaks, with a voice kind each: the model's cast, plus any speaker it left out."""
    cast: list[tuple[str, VoiceKind]] = []
    seen: set[str] = set()
    for member in response.cast:
        name = " ".join(member.name.split())[:40]
        key = name.casefold()
        if not name or key in seen or is_narrator(name):
            continue
        cast.append((name, member.voice))
        seen.add(key)
    for scene in scenes:
        for line in scene.lines:
            key = line.speaker.casefold()
            if key in seen or is_narrator(line.speaker):
                continue
            cast.append((line.speaker, voice_kind_for_name(line.speaker, len(cast))))
            seen.add(key)
    return cast


def time_scenes(
    scenes: Sequence[SceneSpec], *, lines: Sequence[TimedLine], total_sec: float
) -> list[TimedScene]:
    """When each scene is on screen, from when its lines were spoken.

    The narration was built line by line, so every line's start and end are
    known exactly; a scene runs from a beat before its first line to a beat
    before the next scene's. No guessing from word counts, no alignment.
    """
    if not scenes:
        return []
    total = max(0.5, float(total_sec))
    by_scene: dict[int, list[TimedLine]] = {}
    for line in lines:
        by_scene.setdefault(line.scene, []).append(line)

    starts: list[float] = []
    for scene in scenes:
        spoken = by_scene.get(scene.index) or []
        starts.append(max(0.0, spoken[0].start_sec - _LEAD_SEC) if spoken else -1.0)
    # A scene with nothing spoken (it should not happen) borrows the next start.
    for index in range(len(starts) - 1, -1, -1):
        if starts[index] < 0:
            starts[index] = starts[index + 1] if index + 1 < len(starts) else total
    starts[0] = 0.0
    for index in range(1, len(starts)):
        starts[index] = max(starts[index], starts[index - 1])

    timed: list[TimedScene] = []
    for index, scene in enumerate(scenes):
        start = min(starts[index], total)
        end = total if index == len(scenes) - 1 else min(max(starts[index + 1], start), total)
        timed.append(
            TimedScene(
                **vars(scene),
                start_sec=round(start, 3),
                end_sec=round(end, 3),
                timed_lines=tuple(by_scene.get(scene.index) or ()),
            )
        )
    return timed


def speaking_at(scene: TimedScene, at_sec: float) -> str | None:
    """Who is talking at this moment of the whole narration, or None between lines."""
    for line in scene.timed_lines:
        if line.start_sec <= at_sec < line.end_sec:
            return line.speaker
    return None
