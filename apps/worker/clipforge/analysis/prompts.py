"""Prompt templates, versioned in the repository.

Every candidate is stamped with the `promptVersion` that produced it. Without
that, a prompt change silently invalidates every historical score and the Phase 9
feedback loop starts comparing candidates that were never judged by the same
standard — which would look like a signal and be an artefact.

Bump `PROMPT_VERSION` whenever the wording changes in a way that could alter a
score. Rewording for clarity counts.
"""

from __future__ import annotations

from clipforge.analysis.ranking import RUBRIC_MAXIMA

__all__ = ["PROMPT_VERSION", "SYSTEM_PROMPT", "THEMED_PROMPT_VERSION", "build_prompt"]

PROMPT_VERSION = "v1"

# A COMPILE job tells the model what the compilation is about, which changes
# what it selects — so those candidates are stamped with their own version and
# never compared against a harvest's under the same label.
THEMED_PROMPT_VERSION = "v1-theme"

_THEME_TEMPLATE = """

This clip is going into a compilation about: {theme}
Prefer the moment that most belongs in that compilation. A moment that is \
strong on its own but has nothing to do with the theme should score lower \
here than it otherwise would."""

SYSTEM_PROMPT = """\
You select moments from a video transcript that would work as standalone \
short-form clips.

You judge; you do not calculate. Return sub-scores only — the total is computed \
elsewhere, and any total you produce is discarded.

Be sparing. Most 120-second stretches of a video contain nothing worth clipping, \
and returning an empty list is the correct answer far more often than not. A \
mediocre clip costs more than no clip: it wastes a render, a review, and the \
credibility of everything ranked below it."""

_RUBRIC = "\n".join(
    f"- {name} (0-{maximum}): {description}"
    for (name, maximum), description in zip(
        RUBRIC_MAXIMA.items(),
        (
            "does the opening line make someone stop scrolling",
            "does it open a loop the viewer needs closed",
            "does it make sense with no surrounding context at all",
            "does it provoke a reaction — surprise, recognition, disagreement",
            "does it move, or does it meander",
            "would someone send this to a specific person they know",
        ),
        strict=True,
    )
)

USER_TEMPLATE = """\
Transcript window, timestamps in absolute seconds from the start of the video:

{window}

Find at most {max_candidates} self-contained moments in this window that would \
work as short-form clips of {min_duration:.0f}-{max_duration:.0f} seconds.

Rules:
- startSec and endSec are ABSOLUTE seconds, in the range {start:.1f} to {end:.1f}.
- Approximate boundaries are fine. They are snapped to silence afterwards, so \
aim for the right moment rather than the exact frame.
- `hook` is the actual opening line, quoted from the transcript.
- `reason` is one sentence on why this works as a standalone clip.
- Return an empty list if nothing here is genuinely worth clipping.

Score each on:
{rubric}"""


def build_prompt(
    *,
    window_text: str,
    start_sec: float,
    end_sec: float,
    max_candidates: int = 3,
    min_duration_sec: float = 15.0,
    max_duration_sec: float = 75.0,
    theme: str | None = None,
) -> str:
    prompt = USER_TEMPLATE.format(
        window=window_text,
        max_candidates=max_candidates,
        min_duration=min_duration_sec,
        max_duration=max_duration_sec,
        start=start_sec,
        end=end_sec,
        rubric=_RUBRIC,
    )
    if theme:
        prompt += _THEME_TEMPLATE.format(theme=theme.strip()[:200])
    return prompt
