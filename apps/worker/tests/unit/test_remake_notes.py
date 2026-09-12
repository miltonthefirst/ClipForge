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
    NoteCrop,
    NoteFraming,
    NoteObscure,
    NoteTopic,
    ObscureOptions,
    ObscureRegion,
    RemakeOptions,
    UnsupportedAsk,
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
