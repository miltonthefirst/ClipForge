"""Asking the local model what a trend would be *about*, one row at a time.

The ranking is done before this runs and does not need a model. What a model
adds is the sentence a person actually reads before deciding — "the angle" —
and a judgement the arithmetic cannot make: whether this belongs on a channel
about the things the run was asked about.

Bounded by design. One small call per trend, schema-constrained, on the model
the ANALYZE stage already uses, under one broker lease. It can be turned off
per run and it degrades to nothing when the model is not there: the list is
still ranked, just not explained.
"""

from __future__ import annotations

from collections.abc import Sequence

from clipforge_contracts import Category, LlmTrendVerdict, Trend

from clipforge.models.ollama import OllamaClient

__all__ = [
    "CURATE_PROMPT_VERSION",
    "CURATE_SYSTEM_PROMPT",
    "build_curate_prompt",
    "curate_trend",
    "describe_channel",
]

# v2: the channel's category, when the run named one, is put to the model
# beside its topics, and relevance is judged against either.
CURATE_PROMPT_VERSION = "curate-v2"

CURATE_SYSTEM_PROMPT = """\
You are the editor of a short-form video channel deciding what to make next.

You are shown one topic that is trending right now, the evidence that it is, \
and the videos that exist about it. You judge; you do not invent. Do not claim \
anything about the topic that is not in the evidence in front of you.

Answer in JSON only."""

_USER_TEMPLATE = """\
Topic: {topic}

Evidence it is moving:
{signals}

Videos found about it:
{videos}

{interests}

Answer these four things:
- angle: ONE sentence saying what a 30-60 second clip about this would actually \
show or say. Concrete, not a slogan. If the evidence does not support a clip, \
say what is missing.
- relevance: 0-10, how much this belongs on the channel described above: its \
interests, its category, or both. 0 if neither is given, or nothing here \
relates to them.
- worthClipping: true only if there is at least one video that a clip could be \
cut from AND the topic is something a viewer would stop scrolling for.
- compilationTitle: a title, under 70 characters, for a video stitched from the \
best of these videos. Written for a feed: the hook first, no hashtags."""


def build_curate_prompt(
    trend: Trend, *, interests: Sequence[str], category: Category | None = None
) -> str:
    signals = (
        "\n".join(
            f"- {signal.source.value.replace('_', ' ').title()}: "
            f"{signal.detail or 'seen'} (strength {signal.strength:.2f})"
            for signal in trend.signals
        )
        or "- none recorded"
    )
    videos = (
        "\n".join(
            f"- {video.title[:120]}"
            + (f" — {video.channel}" if video.channel else "")
            + (f", {video.view_count:,} views" if video.view_count is not None else "")
            + (f", {video.duration_sec / 60:.0f} min" if video.duration_sec else "")
            for video in trend.videos[:6]
        )
        or "- none found"
    )
    return _USER_TEMPLATE.format(
        topic=trend.topic,
        signals=signals,
        videos=videos,
        interests=describe_channel(interests, category),
    )


def describe_channel(interests: Sequence[str], category: Category | None) -> str:
    """The sentence that tells the model what the channel is, or that nobody said.

    The category is named by its label and its search terms rather than its
    code, because "Football (soccer): football highlights, champions league"
    is what a person meant and "football" is a key.
    """
    lines: list[str] = []
    if interests:
        lines.append("This channel is about: " + ", ".join(interests) + ".")
    if category is not None:
        lines.append(
            f"Its category is {category.label}"
            + (f" ({', '.join(category.terms)})" if category.terms else "")
            + "."
        )
    if not lines:
        return "This channel has not said what it is about."
    return " ".join(lines)


def curate_trend(
    client: OllamaClient,
    trend: Trend,
    *,
    interests: Sequence[str],
    category: Category | None = None,
) -> LlmTrendVerdict:
    """One schema-constrained call. Raises `OllamaError`; the stage decides what that costs."""
    return client.generate_structured(
        schema_model=LlmTrendVerdict,
        system=CURATE_SYSTEM_PROMPT,
        prompt=build_curate_prompt(trend, interests=interests, category=category),
    )
