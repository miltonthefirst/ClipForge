"""The script prompt and the scene arithmetic: what is asked, who says what, and when.

The parts worth pinning are the quiet ones: a line handed to the wrong
speaker is said in the wrong voice, a person's words paraphrased are a lie
in their voice, and a scene timed early shows the trophy before the line
about winning. None of those error.
"""

from __future__ import annotations

import pytest
from clipforge.synth.scenes import (
    Line,
    SceneSpec,
    TimedLine,
    cast_from_response,
    full_script,
    palette_for,
    parse_dialogue,
    scenes_from_response,
    speaking_at,
    split_sentences,
    time_scenes,
    word_count,
)
from clipforge.synth.script import (
    SCRIPT_PROMPT_VERSION,
    build_script_prompt,
    scene_count_for,
    word_budget,
)
from clipforge.synth.voices import assign_voices, is_narrator, voice_kind_for_name
from clipforge_contracts import (
    ComposeOptions,
    LlmCharacter,
    LlmLine,
    LlmScene,
    LlmScriptResponse,
    SceneMood,
    StickActor,
    StickMood,
    StickPose,
    StickProp,
    VoiceKind,
)

pytestmark = pytest.mark.unit


def scene(
    *lines: tuple[str, str],
    props: tuple[StickProp, ...] = (),
    label: str | None = None,
    actors: tuple[StickActor, ...] | None = None,
) -> LlmScene:
    return LlmScene(
        lines=[LlmLine(speaker=speaker, text=text) for speaker, text in lines],
        actors=list(actors)
        if actors is not None
        else [StickActor(name="Ada", pose=StickPose.TALK, mood=StickMood.HAPPY)],
        props=list(props),
        mood=SceneMood.UPBEAT,
        label=label,
    )


def response(*scenes: LlmScene, cast: list[LlmCharacter] | None = None) -> LlmScriptResponse:
    return LlmScriptResponse(
        title="A title",
        cast=cast
        if cast is not None
        else [
            LlmCharacter(name="Ada", voice=VoiceKind.WOMAN_US),
            LlmCharacter(name="Ben", voice=VoiceKind.MAN_UK),
        ],
        scenes=list(scenes),
    )


# ── The prompt ───────────────────────────────────────────────────────────────


def test_a_written_prompt_asks_for_a_conversation_from_the_facts() -> None:
    options = ComposeOptions(
        topic="Arsenal's late winner",
        angle="Why one goal changed the title race",
        context="- Google Trends: 200K+ searches\n- r/soccer: #1 today",
        target_duration_sec=45,
    )
    prompt = build_script_prompt(options)
    assert "Topic: Arsenal's late winner" in prompt
    assert "Angle: Why one goal changed the title race" in prompt
    assert "r/soccer: #1 today" in prompt
    assert f"about {word_budget(45)} words" in prompt
    low, high = scene_count_for(45)
    assert f"in {low} to {high} scenes" in prompt
    assert "two or three characters" in prompt
    assert "Characters talk to each other" in prompt
    assert "MAN_UK" in prompt and "CELEBRATE" in prompt and "TROPHY" in prompt
    assert "never a real person" in prompt
    assert "Write for the ear" in prompt
    assert "Use only the facts above" in prompt


def test_a_staging_prompt_numbers_the_lines_with_their_speakers_and_fixes_them() -> None:
    options = ComposeOptions(topic="A thing", script="Ada: First line.\nBen: Second line!")
    prompt = build_script_prompt(options, lines=parse_dialogue(options.script or ""))
    assert "1. Ada: First line." in prompt and "2. Ben: Second line!" in prompt
    assert "spoken exactly as it is" in prompt
    assert "copying each line exactly" in prompt


def test_the_word_budget_follows_the_voice_and_scene_count_follows_length() -> None:
    assert word_budget(45) == 126
    assert word_budget(15) == 42
    assert scene_count_for(45) == (5, 9)
    assert scene_count_for(90) == (10, 16)
    assert scene_count_for(15) == (2, 3)
    assert SCRIPT_PROMPT_VERSION == "script-v2"


# ── Dialogue a person typed ──────────────────────────────────────────────────


def test_dialogue_rows_carry_their_speaker_and_continue_across_bare_rows() -> None:
    lines = parse_dialogue("Ada: We won.\nBen: Did we?\nIt was close.\n\nAda: Very.")
    assert lines == [
        Line("Ada", "We won."),
        Line("Ben", "Did we? It was close."),
        Line("Ada", "Very."),
    ]


def test_a_bare_first_row_is_the_narrator_s() -> None:
    lines = parse_dialogue("Last night in London.\nAda: We won.")
    assert lines[0] == Line("Narrator", "Last night in London.")
    assert is_narrator(lines[0].speaker)


def test_a_speech_too_long_for_one_line_is_split_at_sentences() -> None:
    long = "Ada: " + " ".join(f"Sentence number {i} is quite long enough." for i in range(1, 16))
    lines = parse_dialogue(long)
    assert len(lines) >= 2
    assert all(line.speaker == "Ada" for line in lines)
    assert all(len(line.text) <= 300 for line in lines)
    assert word_count(" ".join(line.text for line in lines)) == word_count(long) - 1


def test_sentences_split_on_ends_and_not_on_abbreviations_or_decimals() -> None:
    text = "Mr. Smith scored 3.5 million. Then the crowd went quiet! Was it over? Not yet."
    assert split_sentences(text) == [
        "Mr. Smith scored 3.5 million.",
        "Then the crowd went quiet!",
        "Was it over? Not yet.",
    ]


# ── Scenes from the model ────────────────────────────────────────────────────


def test_written_scenes_keep_the_model_s_lines_and_put_every_speaker_on_screen() -> None:
    scenes = scenes_from_response(
        response(
            scene(("Ada", "We won!"), ("Ben", "  Did   we? "), props=(StickProp.BALL,)),
            scene(("Narrator", "Later."), ("Ben", "Well.")),
        )
    )
    assert [[(line.speaker, line.text) for line in s.lines] for s in scenes] == [
        [("Ada", "We won!"), ("Ben", "Did we?")],
        [("Narrator", "Later."), ("Ben", "Well.")],
    ]
    # Ben spoke in the first scene but the model only drew Ada: he is added.
    assert [a.name for a in scenes[0].actors] == ["Ada", "Ben"]
    assert scenes[0].actors[1].pose is StickPose.TALK
    # The narrator is never drawn.
    assert [a.name for a in scenes[1].actors] == ["Ada", "Ben"]
    assert scenes[0].props == (StickProp.BALL,)
    assert full_script(scenes) == "Ada: We won!\nBen: Did we?\nNarrator: Later.\nBen: Well."


def test_a_given_dialogue_keeps_its_own_words_and_takes_the_model_s_grouping() -> None:
    given = parse_dialogue("Ada: One.\nBen: Two.\nAda: Three.\nBen: Four.\nAda: Five.")
    scenes = scenes_from_response(
        response(
            scene(("Ada", "Paraphrased!"), ("Ben", "Changed."), props=(StickProp.TROPHY,)),
            scene(("Ada", "Also changed.")),
        ),
        script=given,
    )
    assert [[line.text for line in s.lines] for s in scenes] == [
        ["One.", "Two."],
        ["Three."],
        ["Four.", "Five."],
    ]
    assert scenes[0].props == (StickProp.TROPHY,)
    # The leftover lines got a plain scene with their speakers on screen.
    assert [a.name for a in scenes[2].actors] == ["Ben", "Ada"]


def test_a_label_with_nowhere_to_write_it_gets_a_sign_and_duplicate_props_collapse() -> None:
    scenes = scenes_from_response(
        response(
            scene(
                ("Ada", "Goal."),
                props=(StickProp.BALL, StickProp.BALL, StickProp.STAR),
                label="  GOAL ",
            )
        )
    )
    assert scenes[0].label == "GOAL"
    assert scenes[0].props == (StickProp.BALL, StickProp.STAR, StickProp.SIGN)


# ── Cast and voices ──────────────────────────────────────────────────────────


def test_the_cast_is_the_model_s_plus_any_speaker_it_forgot_and_never_the_narrator() -> None:
    scenes = scenes_from_response(
        response(scene(("Ada", "Hi."), ("Cy", "Hello."), ("Narrator", "Later.")))
    )
    cast = cast_from_response(response(scene(("Ada", "Hi."))), scenes)
    assert cast[:2] == [("Ada", VoiceKind.WOMAN_US), ("Ben", VoiceKind.MAN_UK)]
    assert cast[2][0] == "Cy"
    assert all(not is_narrator(name) for name, _ in cast)


def test_every_character_gets_a_distinct_voice_of_its_kind_and_not_the_narrator_s() -> None:
    cast = [("Ada", VoiceKind.WOMAN_US), ("Bea", VoiceKind.WOMAN_US), ("Cy", VoiceKind.MAN_UK)]
    voices = assign_voices(cast, narrator_voice="af_heart", seed=0)
    assert voices["ada"] != voices["bea"]
    assert voices["ada"].startswith("af_") and voices["bea"].startswith("af_")
    assert voices["cy"].startswith("bm_")
    assert "af_heart" not in voices.values()
    # The same seed pairs the same way; another seed may not.
    assert assign_voices(cast, narrator_voice="af_heart", seed=0) == voices
    assert voice_kind_for_name("x", 0) is not voice_kind_for_name("y", 1)


# ── Timing ───────────────────────────────────────────────────────────────────


def specs(*groups: tuple[tuple[str, str], ...]) -> list[SceneSpec]:
    return [
        SceneSpec(
            index=i,
            lines=tuple(Line(s, t) for s, t in group),
            actors=(),
            props=(),
            mood=SceneMood.CALM,
        )
        for i, group in enumerate(groups)
    ]


def test_scenes_start_a_beat_before_their_first_line_and_run_to_the_next() -> None:
    scenes = specs((("Ada", "One."), ("Ben", "Two.")), (("Ada", "Three."),))
    lines = [
        TimedLine("Ada", "One.", scene=0, voice="af_heart", start_sec=0.0, end_sec=1.0),
        TimedLine("Ben", "Two.", scene=0, voice="bm_george", start_sec=1.35, end_sec=2.0),
        TimedLine("Ada", "Three.", scene=1, voice="af_heart", start_sec=2.6, end_sec=3.4),
    ]
    timed = time_scenes(scenes, lines=lines, total_sec=4.0)
    assert [(s.start_sec, s.end_sec) for s in timed] == [(0.0, 2.35), (2.35, 4.0)]
    assert timed[0].timed_lines == tuple(lines[:2])
    assert speaking_at(timed[0], 0.5) == "Ada"
    assert speaking_at(timed[0], 1.2) is None
    assert speaking_at(timed[0], 1.5) == "Ben"
    assert speaking_at(timed[1], 3.0) == "Ada"


def test_a_scene_with_nothing_spoken_borrows_the_next_start() -> None:
    scenes = specs((("Ada", "One."),), (), (("Ada", "Two."),))
    lines = [
        TimedLine("Ada", "One.", scene=0, start_sec=0.0, end_sec=1.0),
        TimedLine("Ada", "Two.", scene=2, start_sec=3.0, end_sec=4.0),
    ]
    timed = time_scenes(scenes, lines=lines, total_sec=5.0)
    assert [(s.start_sec, s.end_sec) for s in timed] == [(0.0, 2.75), (2.75, 2.75), (2.75, 5.0)]


def test_every_mood_has_a_palette() -> None:
    for mood in SceneMood:
        palette = palette_for(mood)
        assert len(palette.sky_top) == 3 and len(palette.ink) == 3
