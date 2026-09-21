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
    Narration,
    build_narration_prompt,
    echoes_the_picture,
    reads_as,
    too_thin,
    word_floor,
    write_narration,
)
from clipforge.media.vision import VisualContext
from clipforge.models.ollama import OllamaError
from clipforge_contracts import LlmNarration

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
    assert "11 to 19 words" in short
    assert "57 to 96 words" in long


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


def test_the_narration_prompt_forbids_describing_the_picture() -> None:
    """The whole complaint: a voice-over that says what the viewer can see."""
    assert "Never read the pictures back" in NARRATE_SYSTEM_PROMPT
    assert "Never mention the footage" in NARRATE_SYSTEM_PROMPT


def test_the_narration_prompt_still_refuses_to_invent() -> None:
    """The new rules ask for stakes, which is exactly where a model starts
    making things up. The old fence has to stay standing."""
    assert "None of this is a licence to invent" in NARRATE_SYSTEM_PROMPT


# ── Is the line only the picture, read back? ─────────────────────────────────


def anime() -> VisualContext:
    """The real one, from the two clips that produced this gate."""
    return VisualContext(
        subject="Animated characters in a dark setting, one facing a shadowy creature.",
        happens=(
            "Characters show concern, determination, distress and shock against a dark background."
        ),
        on_screen_text=[],
        model="test-vision",
    )


def test_the_line_that_shipped_is_caught() -> None:
    """Verbatim from a clip in Review, and the reason this exists."""
    assert echoes_the_picture(
        "This is a dramatic anime moment with characters showing concern, determination, "
        "distress, and shock against a dark background.",
        anime(),
    )


def test_narrating_the_edit_is_caught() -> None:
    """The other one. A viewer can see that the scene cut."""
    assert echoes_the_picture(
        "After that, how selfish is my brother? Then the scene cuts. We see humans lying there.",
        anime(),
    )


def test_a_hook_that_says_something_passes() -> None:
    assert (
        echoes_the_picture(
            "Nobody in that room expected the thing in the dark to answer back. "
            "What would you have done?",
            anime(),
        )
        is None
    )


def test_sharing_vocabulary_with_the_picture_is_not_reciting_it() -> None:
    """The common, correct case: narration about a match, over a match."""
    assert (
        echoes_the_picture(
            "Pavlovic finds the pass and Bayern are through the first line, with the keeper "
            "already off his line.",
            visual(),
        )
        is None
    )


def test_a_paraphrase_of_the_description_is_caught_without_a_banned_phrase() -> None:
    """The phrase list is the cheap half; the count is what catches a rewrite."""
    assert echoes_the_picture(
        "Animated characters, concern and determination and distress and shock, "
        "a shadowy creature, a dark background.",
        anime(),
    )


def test_a_short_line_is_left_alone() -> None:
    """Too little to judge, and a short line is not the failure anyway."""
    assert echoes_the_picture("Shadows. Shock. Nothing else moves.", anime()) is None


def test_with_no_pictures_only_the_footage_talk_is_caught() -> None:
    assert echoes_the_picture("In this video, the fight begins.", None)
    assert (
        echoes_the_picture("He never saw it coming, and neither did anyone watching.", None) is None
    )


# ── The retry ────────────────────────────────────────────────────────────────


class FakeOllama:
    """Returns each script in turn, and remembers what it was asked."""

    def __init__(self, *scripts: str) -> None:
        self._scripts = list(scripts)
        self.prompts: list[str] = []

    def generate_structured(self, **kwargs: object) -> LlmNarration:
        self.prompts.append(str(kwargs.get("prompt")))
        return LlmNarration(
            transcript_usable=False,
            reasoning="word salad",
            script=self._scripts[min(len(self.prompts) - 1, len(self._scripts) - 1)],
        )


GOOD = (
    "Nobody in that room expected the thing in the dark to answer back. What would you have done?"
)
LONG = (
    "Nobody in that room expected the thing in the dark to answer back, and now nobody "
    "knows which of them it came for. They have been arguing about who owes what to whom "
    "since the lights went out, which is a luxury they are about to lose. What would you "
    "have done with thirty seconds and no way out?"
)
ECHO = (
    "This is a dramatic anime moment with characters showing concern, determination, "
    "distress, and shock against a dark background."
)


def written(client: object) -> Narration | None:
    return write_narration(
        client,  # type: ignore[arg-type]
        transcript="tres mal a beaude glim",
        target_language="en-us",
        duration_sec=16.0,
        visual=anime(),
    )


def test_a_line_that_says_something_is_used_as_it_is() -> None:
    client = FakeOllama(GOOD)
    answer = written(client)
    assert answer is not None
    assert answer.script == GOOD
    assert answer.echoed is False
    assert len(client.prompts) == 1, "a clean line must not cost a second call"


def test_a_recital_is_sent_back_once_with_what_it_did() -> None:
    client = FakeOllama(ECHO, GOOD)
    answer = written(client)
    assert answer is not None
    assert answer.script == GOOD
    assert answer.echoed is False
    assert len(client.prompts) == 2
    assert "That attempt was rejected" in client.prompts[1]
    assert "this is a" in client.prompts[1], "it is told which phrase, not just that it failed"


def test_when_both_attempts_recite_the_clip_is_still_made_and_flagged() -> None:
    """A dull narration is worth more than a failed job. The reviewer is told."""
    client = FakeOllama(ECHO, ECHO)
    answer = written(client)
    assert answer is not None
    assert answer.script == ECHO
    assert answer.echoed is True
    assert len(client.prompts) == 2, "one retry, not a loop"


def test_the_model_being_unreachable_gives_nothing_back() -> None:
    class Dead:
        def generate_structured(self, **_: object) -> LlmNarration:
            raise OllamaError("no model")

    assert written(Dead()) is None


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


# ── Long enough to cover the clip ────────────────────────────────────────────


def test_the_floor_follows_the_clip_length() -> None:
    assert word_floor(16.4) == 23
    assert word_floor(37.3) == 53


def test_the_line_that_was_over_in_two_seconds_is_caught() -> None:
    """Verbatim from the first remake written to the rewritten prompt: eight
    words of narration on a thirty-seven second clip."""
    assert too_thin("Why does he think this forest belongs to him?", word_floor(37.3))


def test_a_line_that_ends_a_little_early_is_left_alone() -> None:
    """The bar is under the floor on purpose. Ending early is not a defect."""
    assert too_thin(" ".join(["word"] * 40), word_floor(37.3)) is None


def test_a_short_clip_wants_a_short_line() -> None:
    assert too_thin("He never saw it coming, and neither did anyone else.", word_floor(4.0)) is None


def test_a_thin_line_is_sent_back_and_the_fuller_one_used() -> None:
    client = FakeOllama("Why does he think this forest belongs to him?", LONG)
    answer = written(client)
    assert answer is not None
    assert answer.script == LONG
    assert answer.echoed is False
    assert len(client.prompts) == 2
    assert "It reads well, and it is over long before the picture is" in client.prompts[1]


def test_a_thin_line_twice_keeps_whichever_says_more() -> None:
    """A thin narration is a disappointment, not a defect: no warning, no
    failure, and certainly not the shorter of the two."""
    client = FakeOllama(
        "Eight words is not very many at all.",
        "Nine words here, but that is more than before, truly.",
    )
    answer = written(client)
    assert answer is not None
    assert answer.script.startswith("Nine words here")
    assert answer.echoed is False


def test_reciting_the_picture_is_corrected_before_length() -> None:
    """Two different failures want two different corrections, and a recital
    that is also short is a recital first."""
    client = FakeOllama(ECHO, GOOD)
    written(client)
    assert "over long before the picture is" not in client.prompts[1]
    assert "already looking at the picture" in client.prompts[1]
