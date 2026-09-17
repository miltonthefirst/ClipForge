"""Reading a note that asks for something in the picture to be hidden.

The whole feature turns on this: three real notes asked for the Canal+ bug to
be blurred, and every one of them came back classified as impossible. Both
routes into `obscure` are tested, because the second exists precisely so that a
model which has not noticed the new field still produces the right result.
"""

from __future__ import annotations

import pytest
from clipforge.analysis.remake import apply_interpretation
from clipforge_contracts import (
    LlmRemakeNote,
    NoteAudio,
    NoteCaptions,
    NoteCrop,
    NoteFraming,
    NoteObscure,
    NoteTopic,
    ObscureOptions,
    ObscureRegion,
    RemakeOptions,
    SpeechMode,
    UnsupportedAsk,
    VoiceCaptions,
    VoiceOptions,
)

pytestmark = pytest.mark.unit


def reading(**overrides: object) -> LlmRemakeNote:
    base: dict[str, object] = {
        "topics": [],
        "framing_mode": NoteFraming.NOT_MENTIONED,
        "crop": NoteCrop.NOT_MENTIONED,
        "language": "NONE",
        "audio": NoteAudio.NOT_MENTIONED,
        "start_delta_sec": 0,
        "end_delta_sec": 0,
        "obscure": NoteObscure.NOT_MENTIONED,
        "captions": NoteCaptions.NOT_MENTIONED,
        "summary": "",
    }
    return LlmRemakeNote(**{**base, **overrides})


def test_a_note_asking_to_blur_turns_detection_on() -> None:
    applied = apply_interpretation(
        RemakeOptions(notes="Blur the canal+ on the video"),
        reading(topics=[NoteTopic.OBSCURE], obscure=NoteObscure.ANYWHERE),
    )
    assert applied.options.obscure is not None
    assert applied.options.obscure.auto is True
    assert applied.changes == ["looked for a fixed mark to hide"]


def test_a_watermark_filed_as_impossible_is_carried_out_instead() -> None:
    """Every model said it this way until the OBSCURE topic existed.

    All three of the reviewer's real notes came back as REMOVE_WATERMARK, so
    reading only the new field would have left the feature unreachable from the
    sentences people actually write.
    """
    applied = apply_interpretation(
        RemakeOptions(notes="Blur the canal+ in the screen"),
        reading(unsupported=[UnsupportedAsk.REMOVE_WATERMARK]),
    )
    assert applied.options.obscure is not None
    assert applied.options.obscure.auto is True
    assert UnsupportedAsk.REMOVE_WATERMARK in applied.absorbed


def test_an_absorbed_ask_is_not_also_refused() -> None:
    """Refusing it in the clip that carries it out is the old behaviour."""
    applied = apply_interpretation(
        RemakeOptions(notes="blur the logo"),
        reading(unsupported=[UnsupportedAsk.REMOVE_WATERMARK, UnsupportedAsk.SLOW_MOTION]),
    )
    assert applied.absorbed == [UnsupportedAsk.REMOVE_WATERMARK]
    assert UnsupportedAsk.SLOW_MOTION not in applied.absorbed


def test_burnt_in_text_is_absorbed_too() -> None:
    applied = apply_interpretation(
        RemakeOptions(notes="cover the French subtitles"),
        reading(unsupported=[UnsupportedAsk.REMOVE_OVERLAY_TEXT]),
    )
    assert applied.options.obscure is not None
    assert applied.options.obscure.auto is True


def test_a_corner_the_note_named_narrows_the_search() -> None:
    applied = apply_interpretation(
        RemakeOptions(notes="blur the logo in the top right"),
        reading(topics=[NoteTopic.OBSCURE], obscure=NoteObscure.TOP_RIGHT),
    )
    assert applied.obscure_where == "TOP_RIGHT"
    assert applied.changes == ["looked for a fixed mark to hide in the top right"]


def test_no_corner_is_the_usual_answer_and_carries_no_hint() -> None:
    applied = apply_interpretation(
        RemakeOptions(notes="blur the canal+"),
        reading(topics=[NoteTopic.OBSCURE], obscure=NoteObscure.ANYWHERE),
    )
    assert applied.obscure_where is None


def test_a_note_about_language_does_not_reach_for_the_blur() -> None:
    """The topic gate, on the field most likely to be volunteered.

    A model answering every field will happily name a corner for a note about
    Spanish, and a remake that silently blurs a corner nobody complained about
    is worse than one that does nothing.
    """
    applied = apply_interpretation(
        RemakeOptions(notes="can we have this in english"),
        reading(topics=[NoteTopic.LANGUAGE], language="en-us", obscure=NoteObscure.TOP_LEFT),
    )
    assert applied.options.obscure is None


def test_a_rectangle_the_reviewer_drew_is_not_overruled() -> None:
    """They pointed at it. Turning on a search as well would be second-guessing."""
    drawn = RemakeOptions(
        notes="blur that",
        obscure=ObscureOptions(regions=[ObscureRegion(x_pct=1, y_pct=1, w_pct=5, h_pct=5)]),
    )
    applied = apply_interpretation(
        drawn, reading(topics=[NoteTopic.OBSCURE], obscure=NoteObscure.ANYWHERE)
    )
    assert applied.options.obscure is not None
    assert applied.options.obscure.auto is False
    assert applied.changes == []


def test_a_control_nobody_touched_does_not_outvote_the_note() -> None:
    """An untouched checkbox sends `auto: false`, which is not a decision."""
    untouched = RemakeOptions(notes="blur the canal+", obscure=ObscureOptions(auto=False))
    applied = apply_interpretation(
        untouched, reading(topics=[NoteTopic.OBSCURE], obscure=NoteObscure.ANYWHERE)
    )
    assert applied.options.obscure is not None
    assert applied.options.obscure.auto is True


def test_a_note_that_says_nothing_about_hiding_changes_nothing() -> None:
    applied = apply_interpretation(
        RemakeOptions(notes="it cuts in three seconds late"),
        reading(topics=[NoteTopic.TIMING], start_delta_sec=-3),
    )
    assert applied.options.obscure is None
    assert applied.absorbed == []


# ── The two gates the first real note went through ───────────────────────────


def test_the_note_s_own_words_let_a_reading_through_a_missing_topic() -> None:
    """Measured on the reviewer's real note, against the real model.

    "Blur the canal+ in the screen" came back with `obscure: ANYWHERE` and a
    summary saying so — and `topics: [FRAMING]`, so the topic gate discarded
    the one field that was right and the clip came back with the logo on it for
    the fourth time. Requiring the word makes the gate a conjunction of a
    deterministic signal and the model's reading, the same shape the crop gate
    already had.
    """
    applied = apply_interpretation(
        RemakeOptions(notes="Blur the canal+ in the screen"),
        reading(topics=[NoteTopic.FRAMING], obscure=NoteObscure.ANYWHERE),
    )
    assert applied.options.obscure is not None
    assert applied.options.obscure.auto is True


def test_the_word_alone_is_not_enough() -> None:
    """It stays a conjunction: the model must have read it that way too.

    A note complaining that the blurred background behind a FIT picture is too
    strong contains the word and is not a request to hide anything.
    """
    applied = apply_interpretation(
        RemakeOptions(notes="the blur behind the picture is too strong"),
        reading(topics=[NoteTopic.FRAMING], framing_mode=NoteFraming.FIT),
    )
    assert applied.options.obscure is None


def test_a_bare_as_rendered_is_not_a_framing_instruction() -> None:
    """It is what the model answers when the note says nothing about framing.

    The field is required and AS_RENDERED reads like "leave it", so a 4B
    answers it constantly — it did on the reviewer's note about a logo. Acting
    on it is not harmless: applied to a clip rendered with TRACK it silently
    un-tracks it, and the reviewer gets a differently-framed clip in answer to
    a note about a watermark.
    """
    applied = apply_interpretation(
        RemakeOptions(notes="Blur the canal+ in the screen"),
        reading(
            topics=[NoteTopic.FRAMING, NoteTopic.OBSCURE],
            framing_mode=NoteFraming.AS_RENDERED,
            obscure=NoteObscure.ANYWHERE,
        ),
    )
    assert applied.options.framing is None
    assert applied.changes == ["looked for a fixed mark to hide"]


def test_as_rendered_with_an_anchor_is_still_an_instruction() -> None:
    """Naming a side is a decision about where the window points."""
    applied = apply_interpretation(
        RemakeOptions(notes="keep it to the left of frame"),
        reading(
            topics=[NoteTopic.FRAMING],
            framing_mode=NoteFraming.AS_RENDERED,
            crop=NoteCrop("left"),
        ),
    )
    assert applied.options.framing is not None
    assert applied.options.framing.crop == "left"


def test_a_real_framing_mode_is_untouched_by_the_bare_as_rendered_rule() -> None:
    applied = apply_interpretation(
        RemakeOptions(notes="it keeps losing the ball"),
        reading(topics=[NoteTopic.FRAMING], framing_mode=NoteFraming.TRACK),
    )
    assert applied.options.framing is not None
    assert applied.options.framing.mode == "TRACK"


# ─────────────────────────────────────────────────────────────────────────────
# Captions: the note that was read correctly and then went nowhere
# ─────────────────────────────────────────────────────────────────────────────


def test_a_note_asking_for_the_captions_off_takes_them_off() -> None:
    """The regression, and the reviewer's own words.

    "Remove caption" was understood — the recorded reading said so in as many
    words — and then reported that it had "set the language to NONE", because
    there was no captions field to put the answer in and a required schema
    leaves a model nowhere to say nothing. The clip came back with its captions.
    """
    applied = apply_interpretation(
        RemakeOptions(notes="Remove caption"),
        reading(topics=[NoteTopic.CAPTIONS], captions=NoteCaptions.REMOVE),
    )

    assert applied.options.captions is VoiceCaptions.REMOVE
    assert applied.changes == ["removed the captions"]
    # Emphatically not a voice change: the reviewer said nothing about the sound.
    assert applied.options.voice is None


def test_a_note_about_captions_is_ignored_when_it_was_not_the_topic() -> None:
    """The same rule every other field here obeys: scope is declared first."""
    applied = apply_interpretation(
        RemakeOptions(notes="Make it wider"),
        reading(topics=[NoteTopic.FRAMING], captions=NoteCaptions.REMOVE),
    )

    assert applied.options.captions is None


def test_the_captions_follow_a_new_voice_rather_than_the_note() -> None:
    """A language change has already decided what the captions must say.

    REBUILD is the only answer that keeps them in sync with narration that does
    not exist yet, so the note loses — but it is recorded as a conflict, because
    being overruled silently is how a reviewer loses an argument they did not
    know they were having.
    """
    applied = apply_interpretation(
        RemakeOptions(
            notes="Remove caption",
            voice=VoiceOptions(
                voice="af_heart",
                language="es",
                mode=SpeechMode.REPLACE,
                captions=VoiceCaptions.REBUILD,
            ),
        ),
        reading(topics=[NoteTopic.CAPTIONS], captions=NoteCaptions.REMOVE),
    )

    assert applied.options.voice is not None
    assert applied.options.voice.captions is VoiceCaptions.REBUILD
    assert any("captions" in c for c in applied.conflicts)


def test_what_the_reviewer_set_is_never_reinterpreted() -> None:
    applied = apply_interpretation(
        RemakeOptions(notes="Remove caption", captions=VoiceCaptions.KEEP),
        reading(topics=[NoteTopic.CAPTIONS], captions=NoteCaptions.REMOVE),
    )

    assert applied.options.captions is VoiceCaptions.KEEP
