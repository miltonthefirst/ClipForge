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

from clipforge_contracts import LlmTrendVerdict, Trend

from clipforge.models.ollama import OllamaClient

__all__ = ["CURATE_PROMPT_VERSION", "CURATE_SYSTEM_PROMPT", "build_curate_prompt", "curate_trend"]

CURATE_PROMPT_VERSION = "curate-v1"

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
- relevance: 0-10, how much this belongs on a channel about the interests \
above. 0 if there are no interests listed, or nothing here relates to them.
- worthClipping: true only if there is at least one video that a clip could be \
cut from AND the topic is something a viewer would stop scrolling for.
- compilationTitle: a title, under 70 characters, for a video stitched from the \
best of these videos. Written for a feed: the hook first, no hashtags."""


def build_curate_prompt(trend: Trend, *, interests: Sequence[str]) -> str:
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
    if interests:
        interest_line = "This channel is about: " + ", ".join(interests) + "."
    else:
        interest_line = "This channel has not said what it is about."
    return _USER_TEMPLATE.format(
        topic=trend.topic, signals=signals, videos=videos, interests=interest_line
    )


def curate_trend(
    client: OllamaClient, trend: Trend, *, interests: Sequence[str]
) -> LlmTrendVerdict:
    """One schema-constrained call. Raises `OllamaError`; the stage decides what that costs."""
    return client.generate_structured(
        schema_model=LlmTrendVerdict,
        system=CURATE_SYSTEM_PROMPT,
        prompt=build_curate_prompt(trend, interests=interests),
    )
