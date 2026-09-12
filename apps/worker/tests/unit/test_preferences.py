"""Learning from a correction without becoming a nag.

The rules that matter here are all about restraint. A standing preference
shapes every later clip and the reviewer has no reason to suspect it is there,
so the expensive mistake is proposing a wrong one, not missing a right one.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from clipforge.analysis.preferences import (
    apply_to_options,
    build_prompt,
    dedupe,
    standing_guidance,
)
from clipforge_contracts import (
    AppliedRemake,
    AppliedVoice,
    CropAnchor,
    FramingMode,
    NoteTopic,
    Preference,
    PreferenceScope,
    PreferenceStatus,
    RemakeDefaults,
    SpeechMode,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def preference(**overrides: object) -> Preference:
    defaults = {
        "id": "pref-1",
        "uid": "user-1",
        "scope": PreferenceScope.SOURCE,
        "source_id": "src-1",
        "category": NoteTopic.FRAMING,
        "lesson": "this channel's wide shots lose the ball unless the window follows it",
        "status": PreferenceStatus.ACCEPTED,
        "created_at": NOW,
    }
    return Preference(**{**defaults, **overrides})


# ── Never propose the same thing twice ──────────────────────────────────────


def test_a_preference_already_held_is_dropped() -> None:
    held = preference()
    assert dedupe([preference(id="pref-2")], [held]) == []


def test_a_rejected_preference_never_comes_back() -> None:
    """The rejection is itself the thing worth remembering.

    Deduping only against accepted preferences would mean the same suggestion
    returned on every correction of the same kind of clip — which is the
    difference between a system that learns and one that nags.
    """
    turned_down = preference(status=PreferenceStatus.REJECTED)
    assert dedupe([preference(id="pref-2")], [turned_down]) == []


def test_wording_differences_do_not_sneak_a_duplicate_through() -> None:
    held = preference(lesson="Follow the ball on this channel's wide shots.")
    again = preference(id="pref-2", lesson="follow the ball on this channels wide shots")
    assert dedupe([again], [held]) == []


def test_a_genuinely_new_preference_survives() -> None:
    held = preference()
    fresh = preference(id="pref-2", lesson="commentary here is French; English is wanted")
    assert dedupe([fresh], [held]) == [fresh]


def test_two_proposals_that_say_the_same_thing_collapse() -> None:
    a = preference(id="a", lesson="always follow the ball")
    b = preference(id="b", lesson="Always follow the ball!")
    assert len(dedupe([a, b], [])) == 1


# ── A preference is a default, not an override ──────────────────────────────


def test_a_preference_fills_a_setting_the_reviewer_left_open() -> None:
    accepted = [preference(defaults=RemakeDefaults(framing_mode=FramingMode.TRACK))]
    filled = apply_to_options(accepted, framing_set=False, voice_set=False)
    assert filled.framing_mode is FramingMode.TRACK


def test_a_preference_never_overrides_a_choice_made_for_this_clip() -> None:
    """A default that beats a choice is not a default.

    The same precedence the note layer uses. The reviewer's silence is the
    opening; a framing they picked on this remake closes it.
    """
    accepted = [preference(defaults=RemakeDefaults(framing_mode=FramingMode.TRACK))]
    filled = apply_to_options(accepted, framing_set=True, voice_set=False)
    assert filled.framing_mode is None


def test_the_most_recent_preference_wins_where_two_disagree() -> None:
    """Teaching something twice is changing your mind, not a conflict."""
    accepted = [
        preference(id="old", defaults=RemakeDefaults(framing_mode=FramingMode.FIT)),
        preference(id="new", defaults=RemakeDefaults(framing_mode=FramingMode.TRACK)),
    ]
    assert apply_to_options(accepted, framing_set=False, voice_set=False).framing_mode is (
        FramingMode.TRACK
    )


def test_a_preference_with_no_settings_fills_nothing() -> None:
    """Plenty of what a reviewer teaches fills no box and is still worth holding."""
    accepted = [preference(defaults=None)]
    filled = apply_to_options(accepted, framing_set=False, voice_set=False)
    assert filled.framing_mode is None
    assert filled.language is None


def test_language_is_filled_only_when_no_voice_was_asked_for() -> None:
    accepted = [preference(defaults=RemakeDefaults(language="en-us"))]
    assert apply_to_options(accepted, framing_set=False, voice_set=False).language == "en-us"
    assert apply_to_options(accepted, framing_set=False, voice_set=True).language is None


# ── What the model is told ──────────────────────────────────────────────────


def test_the_prompt_shows_rejected_preferences_too() -> None:
    """So it does not propose one that has already been turned down.

    Deduping catches it afterwards, but a model that keeps suggesting rejected
    ideas wastes the one proposal slot per correction that might have held
    something new.
    """
    known = [
        preference(id="a", lesson="keep the crowd noise", status=PreferenceStatus.REJECTED),
        preference(id="b", lesson="follow the ball", status=PreferenceStatus.ACCEPTED),
    ]
    prompt = build_prompt(
        note="follow the ball again",
        applied=AppliedRemake(framing_mode=FramingMode.TRACK, start_sec=0, end_sec=10),
        source_title="Champions League highlights",
        known=known,
    )
    assert "keep the crowd noise" in prompt
    assert "REJECTED" in prompt
    assert "Champions League highlights" in prompt


def test_the_prompt_carries_what_could_not_be_done() -> None:
    """A refusal is a strong signal about what this reviewer keeps wanting."""
    applied = AppliedRemake(
        framing_mode=FramingMode.FIT,
        start_sec=0,
        end_sec=10,
        refusals=["removing a watermark is not something ClipForge can do"],
    )
    prompt = build_prompt(note="lose the watermark", applied=applied, source_title=None, known=[])
    assert "could not do" in prompt
    assert "watermark" in prompt


def test_the_prompt_survives_a_remake_that_had_no_note() -> None:
    prompt = build_prompt(
        note="",
        applied=AppliedRemake(framing_mode=FramingMode.AS_RENDERED, start_sec=0, end_sec=8),
        source_title=None,
        known=[],
    )
    assert "no note" in prompt
    assert "(none yet)" in prompt


def test_standing_guidance_reads_as_instructions() -> None:
    """Fed to the note reader so a reviewer need not repeat themselves."""
    lines = standing_guidance(
        [
            preference(category=NoteTopic.FRAMING, lesson="follow the ball"),
            preference(id="b", category=NoteTopic.LANGUAGE, lesson="English, always"),
        ]
    )
    assert lines == ["framing: follow the ball", "language: English, always"]


def test_a_voice_preference_is_described_for_the_model() -> None:
    applied = AppliedRemake(
        framing_mode=FramingMode.TRACK,
        start_sec=0,
        end_sec=10,
        voice=AppliedVoice(
            mode=SpeechMode.REPLACE,
            voice="af_heart",
            language="en-us",
            engine="kokoro-v1.0-onnx",
            translated=True,
        ),
    )
    prompt = build_prompt(note="in English", applied=applied, source_title=None, known=[])
    assert "af_heart in en-us" in prompt
    assert "translated" in prompt


def test_a_crop_preference_round_trips() -> None:
    held = preference(defaults=RemakeDefaults(crop=CropAnchor.LEFT))
    assert held.defaults is not None
    assert held.defaults.crop is CropAnchor.LEFT
