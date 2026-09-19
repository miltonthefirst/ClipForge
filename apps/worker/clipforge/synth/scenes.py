"""Scenes: what the model proposed, made drawable and timed against the voice.

Pure functions, because each decision here is quiet when wrong. A scene timed
a second early shows the trophy before the line about winning; a sentence
split at "Mr." makes two scenes of one thought; a script the person wrote and
the model paraphrased is a lie spoken in their voice. None of those produce an
error, so each is pinned by a test instead.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

from clipforge_contracts import (
    LlmScriptResponse,
    SceneMood,
    StickActor,
    StickMood,
    StickPose,
    StickProp,
    TranscriptWord,
)

__all__ = [
    "MAX_SCENES",
    "Palette",
    "SceneSpec",
    "TimedScene",
    "full_script",
    "palette_for",
    "scenes_from_response",
    "split_sentences",
    "time_scenes",
    "word_count",
]

MAX_SCENES = 16

# Abbreviations that end in a full stop and do not end a sentence. English and
# short on purpose, like the scorer's stopwords: the scripts this splits are
# written by this system in English.
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


@dataclass(frozen=True)
class SceneSpec:
    """One scene, drawable: a line and the picture the renderer will make of it."""

    index: int
    line: str
    actors: tuple[StickActor, ...]
    props: tuple[StickProp, ...]
    mood: SceneMood
    label: str | None = None


@dataclass(frozen=True)
class TimedScene(SceneSpec):
    """A scene with its place in the narration."""

    start_sec: float = 0.0
    end_sec: float = 0.0

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


def split_sentences(text: str) -> list[str]:
    """Sentences, for a script somebody wrote: one scene each.

    Splits on a full stop, question or exclamation mark followed by a space and
    a capital, so "3.5 million" stays whole. "Mr." and a fragment under four
    words join what *follows* them — "Mr." belongs to "Smith", and "Was it
    over? Not yet." is one beat — and a short last fragment joins what came
    before. The result is capped at the scene limit by joining the tail, never
    by dropping words.
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
    while len(pieces) > MAX_SCENES:
        pieces[-2] = f"{pieces[-2]} {pieces[-1]}"
        pieces.pop()
    return pieces


def word_count(text: str) -> int:
    return len(_WORD.findall(text))


def full_script(scenes: Sequence[SceneSpec]) -> str:
    return " ".join(scene.line.strip() for scene in scenes if scene.line.strip())


def _actor(actor: StickActor) -> StickActor:
    return StickActor(
        name=" ".join(actor.name.split())[:40] or "someone",
        pose=actor.pose or StickPose.STAND,
        mood=actor.mood or StickMood.NEUTRAL,
    )


def scenes_from_response(
    response: LlmScriptResponse, *, script: str | None = None
) -> list[SceneSpec]:
    """What the model said, as scenes the renderer will accept.

    With a script given, the lines are the script's own sentences and the
    model's lines are ignored: it was asked to stage the words, not to improve
    them. The model's scenes are matched to sentences by position, and a
    sentence the model did not stage gets a figure standing and thinking,
    which is honest about how much was known.
    """
    proposed = list(response.scenes)[:MAX_SCENES]
    if script is not None:
        lines = split_sentences(script)
    else:
        lines = [" ".join(scene.line.split()) for scene in proposed]
        lines = [line for line in lines if line][:MAX_SCENES]

    scenes: list[SceneSpec] = []
    for index, line in enumerate(lines):
        source = proposed[index] if index < len(proposed) else None
        if source is None:
            scenes.append(
                SceneSpec(
                    index=index,
                    line=line,
                    actors=(
                        StickActor(
                            name="the narrator", pose=StickPose.THINK, mood=StickMood.NEUTRAL
                        ),
                    ),
                    props=(),
                    mood=SceneMood.CALM,
                )
            )
            continue
        props = tuple(dict.fromkeys(source.props or []))[:3]
        label = " ".join((source.label or "").split())[:24] or None
        if label and not any(prop in (StickProp.SIGN, StickProp.SCREEN) for prop in props):
            # A label with nothing to write it on: give it a sign.
            props = (*props[:2], StickProp.SIGN)
        scenes.append(
            SceneSpec(
                index=index,
                line=line,
                actors=tuple(_actor(actor) for actor in (source.actors or [])[:3]),
                props=props,
                mood=source.mood or SceneMood.CALM,
                label=label,
            )
        )
    return scenes


def time_scenes(
    scenes: Sequence[SceneSpec],
    *,
    words: Sequence[TranscriptWord] | None,
    total_sec: float,
) -> list[TimedScene]:
    """When each scene is on screen.

    With the narration's own word timings, a scene starts where its first
    word is spoken: the words are walked in order and each scene claims as
    many as its line has. Whisper does not always hear exactly the words the
    script had, so the count, not the text, is what is matched — a scene of
    twelve words claims the next twelve heard, and the drift that leaves is a
    word or two at a boundary rather than a scene out of step.

    Without timings, time is shared in proportion to word count, which is what
    a synthesiser's pace amounts to over a whole sentence.
    """
    if not scenes:
        return []
    total = max(0.5, float(total_sec))
    counts = [max(1, word_count(scene.line)) for scene in scenes]

    starts: list[float] = []
    if words:
        heard = [w for w in words if w.text.strip()]
        if len(heard) >= max(3, sum(counts) // 2):
            cursor = 0
            for count in counts:
                if cursor < len(heard):
                    starts.append(max(0.0, float(heard[cursor].start_sec)))
                else:
                    starts.append(total)
                cursor += count
    if not starts:
        span = sum(counts)
        elapsed = 0.0
        for count in counts:
            starts.append(elapsed)
            elapsed += total * count / span

    starts[0] = 0.0
    timed: list[TimedScene] = []
    for index, scene in enumerate(scenes):
        start = min(starts[index], total)
        end = total if index == len(scenes) - 1 else min(max(starts[index + 1], start), total)
        timed.append(
            TimedScene(
                **vars(scene),
                start_sec=round(start, 3),
                end_sec=round(end, 3),
            )
        )
    # A scene shorter than a second is a flash; give it the second and take it
    # from the next, which keeps the total exact.
    for index in range(len(timed) - 1):
        if timed[index].duration_sec < 1.0:
            wanted = min(timed[index].start_sec + 1.0, timed[index + 1].end_sec)
            timed[index] = replace(timed[index], end_sec=wanted)
            timed[index + 1] = replace(timed[index + 1], start_sec=wanted)
    return timed
