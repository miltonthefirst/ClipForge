"""Asking the local model to write a conversation, or to stage one already written.

Two prompts over one schema. **Write** is given a topic, an angle and the
facts the trend carried, and returns a cast and scenes of dialogue: who is on
screen, what each of them says, in what order. **Stage** is given lines a
person wrote and returns the same shape with the words fixed: the person
wrote it, the model only casts and stages it.

Dialogue, not voice-over. The first version of this narrated a description
over figures who stood there, and it looked like what it was: a slideshow
with a commentary. A cartoon is characters talking to each other, so every
line belongs to a named character and is spoken in that character's voice;
a narrator is allowed only for what no character could say.

The vocabulary is still the point. The model does not describe a picture; it
picks from the poses, moods and props the renderer has code for, and from
four kinds of voice the synthesiser has, and the schema is passed to Ollama
as a format constraint so a pose that does not exist is refused by the
runtime. The facts rule holds too: use what is given, say what is not known,
never invent a name, a number or a quote.
"""

from __future__ import annotations

from collections.abc import Sequence

from clipforge_contracts import (
    ComposeOptions,
    LlmScriptResponse,
    SceneMood,
    StickMood,
    StickPose,
    StickProp,
    VoiceKind,
)

from clipforge.models.ollama import OllamaClient
from clipforge.synth.scenes import Line

__all__ = [
    "SCRIPT_PROMPT_VERSION",
    "SCRIPT_SYSTEM_PROMPT",
    "WORDS_PER_SECOND",
    "build_script_prompt",
    "scene_count_for",
    "word_budget",
    "write_script",
]

SCRIPT_PROMPT_VERSION = "script-v2"

#: Kokoro at speed 1.0 reads about 180 words a minute: the first real run spoke
#: 58 words in 19.4 seconds. Dialogue adds a beat between lines, so a video
#: written to this budget lands a little over the target rather than under it.
WORDS_PER_SECOND = 2.8

SCRIPT_SYSTEM_PROMPT = """\
You write short vertical cartoon videos as conversations between two or three \
stick-figure characters.

You have one job: turn the facts you are given into a conversation a person \
would watch to the end. Every line is spoken by a named character, on screen, \
to another character. Do not narrate; do not describe what is happening. A \
"Narrator" may speak at most once at the start and once at the end, and only \
for something no character could say.

You do not invent facts. If the facts are thin, have a character say what is \
known and what is not, plainly. Never make up a name, a number, a date or a \
quote that is not in the facts.

The characters are stick figures named by role or first name — "the minister", \
"a fan", "Ada" — and are never drawn to look like anyone. You choose the kind of \
voice each one speaks with, what each one is doing and how they feel, from fixed \
lists, and what is drawn beside them from a fixed list of props.

Answer in JSON only."""

_LISTS = """\
Voice kinds: {voices}.
Poses: {poses}.
Moods: {moods}.
Props: {props}.
Scene moods (the tone, which sets the colours): {scene_moods}."""

_WRITE_TEMPLATE = """\
Topic: {topic}
{angle_line}
What is known:
{context}

Write a {seconds}-second conversation: about {words} words in total across all \
the lines, in {min_scenes} to {max_scenes} scenes.

The cast: two or three characters who would actually talk about this — people \
it affects, or a curious one and one who knows. Give each a name (a role or a \
first name, never a real person) and a voice kind from the list.

Rules for the words:
- The first line is the hook: something one character says that makes a viewer \
stop scrolling. No "in this video", no "today we".
- Characters talk to each other: they ask, answer, disagree, react. Short lines. \
No speeches.
- Use only the facts above. Where they are thin, a character says so.
- Speak plainly. No hashtags, no emoji, no lists.
- Write for the ear, because every line is read aloud: no URLs, no "r/" names, \
no symbols or abbreviations; say a place or a forum by its plain name and a \
number the way a person would say it.
- The last line lands: a question one character leaves the other with, or the \
one thing to take away.

Rules for the pictures, per scene:
- The figures on screen are the characters in the scene — whoever speaks or \
listens — each with a pose and a mood from the lists. A listener reacts.
- Up to three props from the list. Use SIGN or SCREEN when a word or two on \
screen would help, and write that word in "label".
- A scene mood, from the list.

{lists}

Also give the video a title of under 70 characters, written for a feed: the hook \
first, no hashtags."""

_STAGE_TEMPLATE = """\
Topic: {topic}
{angle_line}
The dialogue is already written and will be spoken exactly as it is, in this order:
{numbered}

The cast is every speaker above except "Narrator". Give each a voice kind from \
the list. Then group the numbered lines into scenes, in order, copying each line \
exactly as written under its speaker, and for each scene describe the picture:
- The figures on screen are the characters in the scene — whoever speaks or \
listens — each with a pose and a mood from the lists.
- Up to three props from the list. Use SIGN or SCREEN when a word or two on \
screen would help, and write that word in "label".
- A scene mood, from the list.

{lists}

Also give the video a title of under 70 characters, written for a feed."""


def word_budget(seconds: int) -> int:
    """How many words fit the target once spoken, with a beat between lines."""
    return max(20, round(seconds * WORDS_PER_SECOND))


def scene_count_for(seconds: int) -> tuple[int, int]:
    """How many scenes a video of this length wants: one every five to eight seconds."""
    return max(2, seconds // 9), max(3, min(16, seconds // 5))


def _lists() -> str:
    return _LISTS.format(
        voices=", ".join(v.value for v in VoiceKind),
        poses=", ".join(p.value for p in StickPose),
        moods=", ".join(m.value for m in StickMood),
        props=", ".join(p.value for p in StickProp),
        scene_moods=", ".join(m.value for m in SceneMood),
    )


def build_script_prompt(options: ComposeOptions, *, lines: Sequence[Line] | None = None) -> str:
    """The user turn: write from the facts, or stage the lines given."""
    angle_line = f"Angle: {options.angle.strip()}" if options.angle else "Angle: none given."
    if lines:
        numbered = "\n".join(
            f"{i}. {line.speaker}: {line.text}" for i, line in enumerate(lines, start=1)
        )
        return _STAGE_TEMPLATE.format(
            topic=options.topic.strip(), angle_line=angle_line, numbered=numbered, lists=_lists()
        )
    seconds = options.target_duration_sec or 45
    low, high = scene_count_for(seconds)
    context = (options.context or "").strip() or "- Nothing beyond the topic itself."
    return _WRITE_TEMPLATE.format(
        topic=options.topic.strip(),
        angle_line=angle_line,
        context=context,
        seconds=seconds,
        words=word_budget(seconds),
        min_scenes=low,
        max_scenes=high,
        lists=_lists(),
    )


def write_script(
    client: OllamaClient,
    options: ComposeOptions,
    *,
    lines: Sequence[Line] | None = None,
) -> LlmScriptResponse:
    """One schema-constrained call. Raises `OllamaError`; the stage decides what that costs.

    Sampled rather than greedy — a conversation wants voices, and greedy
    decoding from a small model produces the same flat exchange about
    everything — but seeded from the job, so composing again with the same
    seed writes the same words.
    """
    return client.generate_structured(
        schema_model=LlmScriptResponse,
        system=SCRIPT_SYSTEM_PROMPT,
        prompt=build_script_prompt(options, lines=lines),
        temperature=0.0 if lines else 0.7,
        seed=options.seed or 0,
    )
