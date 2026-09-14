"""Writing the words, and the deterministic gates around what comes back.

Everything here is pure: prompts, filters and the language check. Whether the
model writes a good sentence is measured against the real model in
`tests/gpu/`, because a stub would be happy with any of these.
"""

from __future__ import annotations

import pytest
from clipforge.analysis.metadata import (
    METADATA_SYSTEM_PROMPT,
    _tidy_tags,
    _tidy_title,
    build_metadata_prompt,
)
from clipforge.analysis.narrate import (
    NARRATE_SYSTEM_PROMPT,
    build_narration_prompt,
    reads_as,
)
from clipforge.media.vision import VisualContext

pytestmark = pytest.mark.unit


def visual() -> VisualContext:
    return VisualContext(
        subject="A football match, one side in red and the other in yellow.",
        happens="A player in red strikes the ball from the left of the box.",
        on_screen_text=["BAY 0-0 BOD", "10:06", "CANAL+"],
        model="test-vision",
    )


# ── Is it actually in the language that was asked for? ───────────────────────


def test_english_reads_as_english() -> None:
    assert reads_as("Pavlovic finds the pass and Bayern are through the first line", "en-us")


def test_french_does_not_read_as_english() -> None:
    """The bug that shipped.

    Asked to translate French commentary into English, qwen3.5:4b returned the
    French with the punctuation tidied up, `translation_landed` saw text that
    had changed, and an English voice read it out phonetically. The clip was
    recorded as `translated: true` in `en-us` and spoke French.
    """
    assert not reads_as(
        "Pavlovitch, bonne passe. On a franchi un premier rideau et Kane peut enrouler",
        "en-us",
    )


def test_french_reads_as_french() -> None:
    assert reads_as(
        "Pavlovitch, bonne passe. On a franchi un premier rideau et Kane peut enrouler",
        "fr-fr",
    )


def test_spanish_does_not_read_as_english() -> None:
    assert not reads_as(
        "Pavlovic encuentra el pase y el Bayern rompe la primera linea de la defensa", "es"
    ) or reads_as("Pavlovic encuentra el pase y el Bayern rompe la primera linea", "es")


def test_an_accent_is_not_a_language() -> None:
    """`en-us` and `en-gb` are an argument about spelling, not about language."""
    line = "Kane curls it home with the inside of his right foot"
    assert reads_as(line, "en-us")
    assert reads_as(line, "en-gb")


def test_a_line_too_short_to_judge_is_allowed() -> None:
    """Refusing a clip over three words would be a worse failure than the one
    this prevents."""
    assert reads_as("GOAL", "en-us")
    assert reads_as("Kane scores", "es")


def test_a_language_with_no_table_is_allowed() -> None:
    assert reads_as("これはテストです", "ja")


def test_nonsense_in_the_right_language_still_passes() -> None:
    """Worth pinning, because it is the limit of what this gate can do.

    "Very bad beauty glim this pure left lateral munitions shot" is English. It
    is also the exact output that made a clip unusable. Catching THAT is the
    vision grounding's job, not this one's.
    """
    assert reads_as("Very bad beauty glim this pure left lateral munitions shot", "en-us")


# ── The narration prompt ─────────────────────────────────────────────────────


def test_the_pictures_come_before_the_transcript() -> None:
    """A model anchors on what it reads first, and the pictures are the more
    trustworthy of the two."""
    prompt = build_narration_prompt(
        transcript="tres mal a beaude glim",
        target_language="en-us",
        duration_sec=17.0,
        visual=visual(),
    )
    assert prompt.index("What the pictures show") < prompt.index("Machine transcription")


def test_the_word_budget_follows_the_clip_length() -> None:
    short = build_narration_prompt(
        transcript="x", target_language="en-us", duration_sec=8.0, visual=None
    )
    long = build_narration_prompt(
        transcript="x", target_language="en-us", duration_sec=40.0, visual=None
    )
    assert "at most 19 words" in short
    assert "at most 96 words" in long


def test_with_no_pictures_the_prompt_says_so() -> None:
    """Silence would leave the model assuming it had seen something."""
    prompt = build_narration_prompt(
        transcript="x", target_language="en-us", duration_sec=8.0, visual=None
    )
    assert "No description of the pictures is available" in prompt


def test_the_on_screen_text_reaches_the_prompt() -> None:
    prompt = build_narration_prompt(
        transcript="x", target_language="en-us", duration_sec=8.0, visual=visual()
    )
    assert "BAY 0-0 BOD" in prompt


def test_the_narration_prompt_forbids_asserting_an_outcome() -> None:
    assert "Never assert an outcome" in NARRATE_SYSTEM_PROMPT


# ── Titles ───────────────────────────────────────────────────────────────────


def test_a_quoted_title_is_unquoted() -> None:
    """A title that arrives quoted is published quoted."""
    assert _tidy_title('"Kane curls one in"') == "Kane curls one in"
    assert _tidy_title("«Kane curls one in»") == "Kane curls one in"


def test_a_labelled_title_loses_its_label() -> None:
    assert _tidy_title("Title: Kane curls one in") == "Kane curls one in"
    assert _tidy_title("Titre - Kane marque") == "Kane marque"


def test_a_title_is_capped() -> None:
    assert len(_tidy_title("x" * 300)) == 100


# ── Tags ─────────────────────────────────────────────────────────────────────

KNOWN = (
    "Le Bayern fait le show a domicile - LDC 2026/2027 (J1)\n"
    "A football match, Bayern Munich in red against a side in yellow.\n"
    "BAY 0-0 BOD 10:06\n"
    "Pavlovitch makes a good pass. Kane can curl it with his right foot. Diaz."
)


def test_a_fabricated_competition_is_dropped() -> None:
    """qwen3.5:4b returned `laliga` on a Champions League tie, and `lck`, which
    is an esports league. The prompt forbids both in as many words."""
    assert _tidy_tags(["laliga", "lck"], known=KNOWN) == []


def test_a_club_that_does_not_exist_is_dropped() -> None:
    assert _tidy_tags(["bayer leipzig", "bayer leversen"], known=KNOWN) == []


def test_a_plausible_half_truth_is_dropped() -> None:
    """One grounded word and a fixture that never happened.

    This is why every word must be grounded rather than any word: under an
    any-word rule `liverpool vs bayern` survives on `bayern`, and a tag naming
    the wrong match is worse than no tag.
    """
    assert _tidy_tags(["liverpool vs bayern", "diaz real madrid"], known=KNOWN) == []


def test_what_is_actually_in_the_clip_survives() -> None:
    kept = _tidy_tags(["bayern munich", "kane", "right foot shot"], known=KNOWN)
    assert kept == ["bayern munich", "kane", "right foot shot"]


def test_generic_words_survive_in_a_language_the_material_is_not_in() -> None:
    """A Spanish clip cut from French football says "futbol" nowhere.

    Refusing on that basis left two tags on a clip that deserved eight.
    """
    assert _tidy_tags(["futbol", "partido", "highlights"], known=KNOWN) == [
        "futbol",
        "partido",
        "highlights",
    ]


def test_tags_are_lowercased_dehashed_and_deduplicated() -> None:
    """A model asked for tags returns Bayern, bayern and #bayern in one list."""
    assert _tidy_tags(["Bayern", "bayern", "#Bayern"], known=KNOWN) == ["bayern"]


def test_a_sentence_is_not_a_tag() -> None:
    assert _tidy_tags(["kane curls it with the inside of his right foot"], known=KNOWN) == []


def test_with_nothing_known_nothing_is_filtered() -> None:
    """No material means no evidence, and refusing every tag would be worse
    than an unverified one."""
    assert _tidy_tags(["laliga", "anything at all"], known="") == ["laliga", "anything at all"]


def test_the_generic_list_holds_no_competitions() -> None:
    """A list of leagues here would launder the exact fabrication the check
    exists to catch."""
    from clipforge.analysis.metadata import _GENERIC

    for name in ("laliga", "premier", "bundesliga", "champions", "ligue", "serie"):
        assert name not in _GENERIC


# ── The metadata prompt ──────────────────────────────────────────────────────


def test_the_metadata_prompt_refuses_the_analyst_voice() -> None:
    """`Candidate.reason` is one sentence on why a window was selected, and it
    was being shown to viewers."""
    assert "this segment captures" in METADATA_SYSTEM_PROMPT
    assert "Never write like an analyst" in METADATA_SYSTEM_PROMPT


def test_the_language_is_stated_twice() -> None:
    """Once at the top and once at the point of writing: a model that read the
    language forty lines ago writes in English anyway."""
    prompt = build_metadata_prompt(
        language="es", spoken_text="Kane scores", duration_sec=12.0, visual=visual()
    )
    assert prompt.count("es") >= 2
    assert prompt.strip().endswith("in es.")


def test_knowing_nothing_is_said_rather_than_hidden() -> None:
    prompt = build_metadata_prompt(language="en-us", spoken_text="", duration_sec=12.0, visual=None)
    assert "Nothing is known about this clip" in prompt
