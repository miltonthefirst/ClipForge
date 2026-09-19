"""Asking the local model to write a video, or to draw one that is already written.

Two prompts over one schema. **Write** is given a topic, an angle and the
facts the trend carried, and returns the narration as scenes — each a line
the narrator says and a description of who is on screen doing what. **Draw**
is given a finished script and returns the same shape with the lines fixed:
the person wrote it, the model only stages it.

The vocabulary is the point. The model does not describe a picture; it picks
from the poses, moods and props the renderer has code for, and the schema is
passed to Ollama as a format constraint so a pose that does not exist is
refused by the runtime rather than drawn as nothing. A 4B model is weak at
holistic judgement and strong at filling a rubric, which is what this is.

The facts rule is the other point. A trend arrives with signals and video
titles, not a briefing. The script may use those and must not invent names,
numbers or quotes beyond them; when the facts are thin, the honest video
says what is known and what is not. That is in the prompt because the
alternative — a confident 45 seconds about a story the model has never
read — is the failure mode of every tool this is compared against.
"""

from __future__ import annotations

from clipforge_contracts import (
    ComposeOptions,
    LlmScriptResponse,
    SceneMood,
    StickMood,
    StickPose,
    StickProp,
)

from clipforge.models.ollama import OllamaClient

__all__ = [
    "SCRIPT_PROMPT_VERSION",
    "SCRIPT_SYSTEM_PROMPT",
    "WORDS_PER_SECOND",
    "build_script_prompt",
    "scene_count_for",
    "word_budget",
    "write_script",
]

SCRIPT_PROMPT_VERSION = "script-v1"

#: Kokoro at speed 1.0 reads about 180 words a minute: the first real run spoke
#: 58 words in 19.4 seconds. A script written to this budget lands within a few
#: seconds of the target once spoken; the earlier guess of 2.4 left a 30-second
#: video nine seconds short.
WORDS_PER_SECOND = 3.0

SCRIPT_SYSTEM_PROMPT = """\
You write the narration for short vertical explainer videos that are drawn as \
stick-figure cartoons.

You have one job: turn the facts you are given into something a person would \
watch to the end. You do not invent facts. If the facts are thin, say what is \
known and what is not, plainly. Never make up a name, a number, a date or a \
quote that is not in the facts.

The figures on screen are stick figures named by role or first name — "the \
minister", "a fan", "Ada" — and are never drawn to look like anyone. You choose \
what each one is doing and how they feel from fixed lists, and what is drawn \
beside them from a fixed list of props.

Answer in JSON only."""

_LISTS = """\
Poses: {poses}.
Moods: {moods}.
Props: {props}.
Scene moods (the tone, which sets the colours): {scene_moods}."""

_WRITE_TEMPLATE = """\
Topic: {topic}
{angle_line}
What is known:
{context}

Write the narration for a {seconds}-second video: about {words} words in total, \
in {min_scenes} to {max_scenes} scenes. Each scene is one or two spoken sentences.

Rules for the words:
- The first line is the hook: the one thing that makes a viewer stop scrolling. \
No "in this video", no "today we".
- Use only the facts above. Where they are thin, say so in plain words rather \
than filling the gap.
- Speak plainly, as one person to another. No hashtags, no emoji, no lists.
- The last line gives the viewer something to take away, or asks them one question.

Rules for the pictures, per scene:
- Up to three stick figures, each named by role or first name, never a likeness.
- A pose and a mood for each figure, from the lists.
- Up to three props from the list. Use SIGN or SCREEN when a word or two on \
screen would help, and write that word in "label".
- A scene mood, from the list.

{lists}

Also give the video a title of under 70 characters, written for a feed: the hook \
first, no hashtags."""

_DRAW_TEMPLATE = """\
Topic: {topic}
{angle_line}
The narration is already written and will be spoken exactly as it is, in this order:
{numbered}

Return one scene per numbered line, in order, with the line copied exactly as \
written. For each, describe the picture:
- Up to three stick figures, each named by role or first name, never a likeness.
- A pose and a mood for each figure, from the lists.
- Up to three props from the list. Use SIGN or SCREEN when a word or two on \
screen would help, and write that word in "label".
- A scene mood, from the list.

{lists}

Also give the video a title of under 70 characters, written for a feed."""


def word_budget(seconds: int) -> int:
    """How many words fit the target once spoken."""
    return max(20, round(seconds * WORDS_PER_SECOND))


def scene_count_for(seconds: int) -> tuple[int, int]:
    """How many scenes a video of this length wants: one every four to six seconds."""
    return max(2, seconds // 7), max(3, min(16, seconds // 4))


def _lists() -> str:
    return _LISTS.format(
        poses=", ".join(p.value for p in StickPose),
        moods=", ".join(m.value for m in StickMood),
        props=", ".join(p.value for p in StickProp),
        scene_moods=", ".join(m.value for m in SceneMood),
    )


def build_script_prompt(options: ComposeOptions, *, sentences: list[str] | None = None) -> str:
    """The user turn: write from the facts, or draw the sentences given."""
    angle_line = f"Angle: {options.angle.strip()}" if options.angle else "Angle: none given."
    if sentences:
        numbered = "\n".join(f"{i}. {line}" for i, line in enumerate(sentences, start=1))
        return _DRAW_TEMPLATE.format(
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
    sentences: list[str] | None = None,
) -> LlmScriptResponse:
    """One schema-constrained call. Raises `OllamaError`; the stage decides what that costs.

    Sampled rather than greedy — a script wants a voice, and greedy decoding
    from a small model produces the same flat sentence about everything — but
    seeded from the job, so composing again with the same seed writes the same
    words. That is what makes the finished video reproducible from its record.
    """
    return client.generate_structured(
        schema_model=LlmScriptResponse,
        system=SCRIPT_SYSTEM_PROMPT,
        prompt=build_script_prompt(options, sentences=sentences),
        temperature=0.0 if sentences else 0.7,
        seed=options.seed or 0,
    )
