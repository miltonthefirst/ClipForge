"""The gates between a bad input and a finished clip.

Every string in the first section is verbatim from a remake that completed
successfully on the live project and was unusable. They are here so those five
outcomes cannot come back quietly.
"""

from __future__ import annotations

import pytest
from clipforge.analysis.feedback import (
    ClipFacts,
    is_probably_an_instruction,
    is_probably_hallucinated,
    speakability,
    translation_landed,
)

pytestmark = pytest.mark.unit

FACTS = ClipFacts(duration_sec=17.1, source_language="fr", word_count=35, mean_confidence=0.72)


# ── The five that shipped ────────────────────────────────────────────────────


def test_a_hallucinated_music_transcript_is_never_narrated() -> None:
    """Whisper's boilerplate, spoken aloud over a goal, stage direction included.

    The clip's audio was music. Whisper produces this kind of subtitle furniture
    from speechless audio with high confidence, and the pipeline translated it
    and read it out. The reviewer rejected the clip.
    """
    verdict = speakability(
        "Here is a nice musical instrumental for you. [Instrumental music plays here] Thank you.",
        FACTS,
        from_transcript=True,
    )
    assert not verdict.ok
    assert verdict.refusal is not None
    assert "boilerplate" in verdict.refusal


def test_feedback_typed_into_the_script_box_is_not_read_aloud() -> None:
    """A synthesiser narrated the reviewer's own instruction.

    The two boxes look alike and only one is obviously "for the machine", so
    this is a fair thing for a person to do — and refusing it with an
    explanation costs a sentence, while accepting it costs a clip.
    """
    verdict = speakability(
        "Cut the Canal plus caption or watermark in the upper right corner.",
        FACTS,
        from_transcript=False,
    )
    assert not verdict.ok
    assert verdict.refusal is not None
    assert "notes box" in verdict.refusal


def test_a_translation_that_only_added_punctuation_is_rejected() -> None:
    """Asked for English, the clip spoke French.

    Given lowercase, unpunctuated ASR French and told to translate, the local
    model restores the punctuation and returns the same language. It satisfies
    the schema, so the clip was recorded as translated.
    """
    source = (
        "pavlovitch bonne passe on a franchi un premier rideau kane peut enroule du plat du "
        "pied droit nouvelle intervention verticale surtout sur diaz en appui"
    )
    punctuated = (
        "Pavlovitch, bonne passe. On a franchi un premier rideau. Kane peut enrouler du plat "
        "du pied droit. Nouvelle intervention verticale, surtout sur Diaz en appui."
    )
    assert not translation_landed(source, punctuated, target_language="en-us")


def test_a_real_translation_is_accepted() -> None:
    source = "pavlovitch bonne passe on a franchi un premier rideau"
    english = "Pavlovitch, good pass. We have crossed the first curtain."
    assert translation_landed(source, english, target_language="en-us")


def test_words_the_recogniser_was_unsure_of_are_flagged_not_refused() -> None:
    """The clip is still made; the reviewer is told why it may be nonsense.

    This transcript produced "Very bad beauty glim this pure left lateral
    munitions shot" — a faithful translation of a bad transcription. Refusing
    outright would be wrong: sometimes it is the only audio there is, and the
    reviewer may want it anyway.
    """
    rough = ClipFacts(duration_sec=18.5, source_language="fr", word_count=40, mean_confidence=0.48)
    verdict = speakability(
        "tres mal a beaude glim cette frappe pure laterale gauche municois on va le revoir",
        rough,
        from_transcript=True,
    )
    assert verdict.ok
    assert any("unsure" in w for w in verdict.warnings)


# ── What must still get through ─────────────────────────────────────────────


def test_ordinary_commentary_speaks_without_comment() -> None:
    verdict = speakability(
        "What a strike from thirty yards out, the keeper had absolutely no chance.",
        FACTS,
        from_transcript=True,
    )
    assert verdict.ok
    assert verdict.warnings == []


def test_a_hand_written_script_is_trusted() -> None:
    """The reviewer's own words are held only to the checks that protect them."""
    verdict = speakability(
        "An extraordinary goal, and the crowd knows it.", FACTS, from_transcript=False
    )
    assert verdict.ok


@pytest.mark.parametrize(
    "line",
    [
        "Make no mistake, that is a wonderful finish from Kane.",
        "Change of pace from Diaz and suddenly the defence is in trouble.",
        "Keep the ball, keep the shape, and wait for the opening.",
    ],
)
def test_commentary_that_opens_like_an_instruction_is_not_refused(line: str) -> None:
    """The heuristic must not eat real commentary.

    Football commentary is full of imperatives. What separates an instruction is
    that it is *about the clip* — it names a watermark, a crop, the framing —
    not merely that it starts with a verb.
    """
    assert not is_probably_an_instruction(line)
    assert speakability(line, FACTS, from_transcript=True).ok


# ── The rest of the boilerplate family ──────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Thanks for watching!",
        "Thank you for watching.",
        "Subtitles by the Amara.org community",
        "[Applause]",
        "♪♪",
        "you you you you you you you you you",
    ],
)
def test_known_recogniser_boilerplate_is_caught(text: str) -> None:
    assert is_probably_hallucinated(text)


def test_silence_is_refused_rather_than_synthesised() -> None:
    assert not speakability("   ", FACTS, from_transcript=True).ok


def test_a_window_with_almost_no_speech_is_refused() -> None:
    """Two words is not a narration, it is a fragment read by a stranger."""
    verdict = speakability("Allez allez", FACTS, from_transcript=True)
    assert not verdict.ok
    assert verdict.refusal is not None
    assert "words were transcribed" in verdict.refusal


def test_an_empty_translation_never_counts_as_landed() -> None:
    assert not translation_landed("bonne passe de Kane", "", target_language="en-us")


def test_facts_know_when_there_is_nothing_to_say() -> None:
    assert not ClipFacts(duration_sec=10.0, word_count=3).has_speech
    assert ClipFacts(duration_sec=10.0, word_count=40).has_speech
