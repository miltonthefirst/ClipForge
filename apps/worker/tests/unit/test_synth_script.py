"""The script prompt and the scene arithmetic: what is asked, and what is drawn when.

The timing is the part worth pinning. A scene that starts a second early shows
the trophy before the line about winning, and nothing errors.
"""

from __future__ import annotations

import pytest
from clipforge.synth.scenes import (
    SceneSpec,
    full_script,
    palette_for,
    scenes_from_response,
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
from clipforge_contracts import (
    ComposeOptions,
    LlmScene,
    LlmScriptResponse,
    SceneMood,
    StickActor,
    StickMood,
    StickPose,
    StickProp,
    TranscriptWord,
)

pytestmark = pytest.mark.unit


def scene(line: str, *props: StickProp, label: str | None = None) -> LlmScene:
    return LlmScene(
        line=line,
        actors=[StickActor(name="the fan", pose=StickPose.CELEBRATE, mood=StickMood.HAPPY)],
        props=list(props),
        mood=SceneMood.UPBEAT,
        label=label,
    )


# ── The prompt ───────────────────────────────────────────────────────────────


def test_a_written_prompt_carries_the_facts_the_budget_and_the_vocabulary() -> None:
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
    assert "CELEBRATE" in prompt and "TROPHY" in prompt and "URGENT" in prompt
    assert "never a likeness" in prompt
    assert "Use only the facts above" in prompt


def test_a_prompt_with_no_facts_says_so_rather_than_leaving_a_blank() -> None:
    prompt = build_script_prompt(ComposeOptions(topic="Something"))
    assert "Nothing beyond the topic itself" in prompt
    assert "Angle: none given." in prompt


def test_a_drawing_prompt_numbers_the_sentences_and_fixes_them() -> None:
    options = ComposeOptions(topic="A thing", script="First line. Second line!")
    prompt = build_script_prompt(options, sentences=["First line.", "Second line!"])
    assert "1. First line." in prompt and "2. Second line!" in prompt
    assert "spoken exactly as it is" in prompt
    assert "about" not in prompt.split("Return one scene")[0].split("Topic")[1]


def test_the_word_budget_follows_the_voice_and_scene_count_follows_length() -> None:
    assert word_budget(45) == 135
    assert word_budget(15) == 45
    assert scene_count_for(45) == (6, 11)
    assert scene_count_for(90) == (12, 16)
    assert scene_count_for(15) == (2, 3)
    assert SCRIPT_PROMPT_VERSION == "script-v1"


# ── Sentences ────────────────────────────────────────────────────────────────


def test_sentences_split_on_ends_and_not_on_abbreviations_or_decimals() -> None:
    text = "Mr. Smith scored 3.5 million. Then the crowd went quiet! Was it over? Not yet."
    assert split_sentences(text) == [
        "Mr. Smith scored 3.5 million.",
        "Then the crowd went quiet!",
        "Was it over? Not yet.",
    ]


def test_a_short_fragment_joins_the_sentence_before_it() -> None:
    assert split_sentences("It was a long night for everyone involved. Really.") == [
        "It was a long night for everyone involved. Really."
    ]


def test_too_many_sentences_are_joined_rather_than_dropped() -> None:
    text = " ".join(f"Sentence number {i} is here now." for i in range(1, 21))
    pieces = split_sentences(text)
    assert len(pieces) == 16
    assert "Sentence number 20" in pieces[-1]
    assert word_count(" ".join(pieces)) == word_count(text)


# ── Scenes from the model ────────────────────────────────────────────────────


def test_scenes_take_the_model_s_lines_when_it_wrote_them() -> None:
    response = LlmScriptResponse(
        title="A title",
        scenes=[scene("First. "), scene("  Second   line "), scene("Third", StickProp.BALL)],
    )
    scenes = scenes_from_response(response)
    assert [s.line for s in scenes] == ["First.", "Second line", "Third"]
    assert scenes[2].props == (StickProp.BALL,)
    assert scenes[0].actors[0].name == "the fan"
    assert full_script(scenes) == "First. Second line Third"


def test_a_given_script_keeps_its_own_words_whatever_the_model_returned() -> None:
    response = LlmScriptResponse(
        title="A title", scenes=[scene("Paraphrased!", StickProp.TROPHY), scene("Also changed.")]
    )
    scenes = scenes_from_response(response, script="Exact words. And more of them here.")
    assert [s.line for s in scenes] == ["Exact words. And more of them here."]
    # The model's first staging is kept for the first sentence.
    assert scenes[0].props == (StickProp.TROPHY,)


def test_a_sentence_the_model_did_not_stage_gets_a_thinking_narrator() -> None:
    response = LlmScriptResponse(title="t", scenes=[scene("only one")])
    scenes = scenes_from_response(
        response, script="The first sentence is here. The second one is here too."
    )
    assert len(scenes) == 2
    assert scenes[1].actors[0].pose is StickPose.THINK
    assert scenes[1].actors[0].name == "the narrator"


def test_a_label_with_nowhere_to_write_it_gets_a_sign_and_duplicate_props_collapse() -> None:
    response = LlmScriptResponse(
        title="t",
        scenes=[scene("Line", StickProp.BALL, StickProp.BALL, StickProp.STAR, label="  GOAL ")],
    )
    scenes = scenes_from_response(response)
    assert scenes[0].label == "GOAL"
    assert scenes[0].props == (StickProp.BALL, StickProp.STAR, StickProp.SIGN)


# ── Timing ───────────────────────────────────────────────────────────────────


def specs(*lines: str) -> list[SceneSpec]:
    return [
        SceneSpec(index=i, line=line, actors=(), props=(), mood=SceneMood.CALM)
        for i, line in enumerate(lines)
    ]


def words(*timed: tuple[str, float]) -> list[TranscriptWord]:
    return [
        TranscriptWord(text=text, start_sec=start, end_sec=start + 0.3) for text, start in timed
    ]


def test_scenes_start_where_their_first_word_is_heard() -> None:
    scenes = specs("One two three.", "Four five.", "Six.")
    heard = words(
        ("One", 0.2), ("two", 0.6), ("three", 1.0), ("Four", 2.5), ("five", 2.9), ("Six", 4.4)
    )
    timed = time_scenes(scenes, words=heard, total_sec=5.0)
    assert [(s.start_sec, s.end_sec) for s in timed] == [(0.0, 2.5), (2.5, 4.4), (4.4, 5.0)]


def test_without_timings_time_is_shared_by_word_count() -> None:
    scenes = specs("One two three four.", "Five six.")
    timed = time_scenes(scenes, words=None, total_sec=6.0)
    assert [(s.start_sec, s.end_sec) for s in timed] == [(0.0, 4.0), (4.0, 6.0)]


def test_too_few_heard_words_fall_back_to_proportion() -> None:
    scenes = specs("One two three four five six.", "Seven eight nine ten.")
    timed = time_scenes(scenes, words=words(("One", 0.1)), total_sec=10.0)
    assert [(s.start_sec, s.end_sec) for s in timed] == [(0.0, 6.0), (6.0, 10.0)]


def test_a_flash_of_a_scene_is_given_a_second() -> None:
    scenes = specs("Hi.", "A much longer second sentence follows the short one.")
    heard = words(("Hi", 0.0), ("A", 0.2), ("much", 0.4))
    timed = time_scenes(scenes, words=heard, total_sec=8.0)
    assert timed[0].end_sec == 1.0
    assert timed[1].start_sec == 1.0
    assert timed[-1].end_sec == 8.0


def test_every_mood_has_a_palette() -> None:
    for mood in SceneMood:
        palette = palette_for(mood)
        assert len(palette.sky_top) == 3 and len(palette.ink) == 3
