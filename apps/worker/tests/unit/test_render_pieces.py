"""Captions, profiles and the filtergraph — the parts of render that are pure.

The filtergraph is built as a separate function precisely so it can be asserted
on here: filter *ordering* is the thing most likely to be wrong and the least
visible from looking at the output. A clip whose captions were burned before the
scale looks subtly wrong in a way that is hard to attribute.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from clipforge.media.captions import build_ass, group_into_cues
from clipforge.media.ffprobe import MediaInfo
from clipforge.media.profiles import CaptionStyle, RenderProfile, load_profile
from clipforge.media.render import build_filtergraph
from clipforge_contracts import ObscureOptions, ObscureRegion, TranscriptWord

STYLE = CaptionStyle()


def words(*spec: tuple[str, float, float]) -> list[TranscriptWord]:
    return [TranscriptWord(text=t, start_sec=s, end_sec=e) for t, s, e in spec]


def landscape(width: int = 1920, height: int = 1080) -> MediaInfo:
    return MediaInfo(
        duration_sec=600.0,
        width=width,
        height=height,
        has_video=True,
        has_audio=True,
        format_name="mov,mp4",
        size_bytes=1,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Captions
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_cue_times_are_relative_to_the_clip_not_the_source() -> None:
    """The renderer cuts first and burns captions onto the cut, so source-timed
    captions would be offset by exactly the clip's start."""
    cues = group_into_cues(
        words(("hello", 100.0, 100.5), ("world", 100.5, 101.0)),
        style=STYLE,
        clip_start_sec=100.0,
        clip_end_sec=130.0,
    )
    assert cues[0].start_sec == pytest.approx(0.0)
    assert cues[0].end_sec == pytest.approx(1.0)


@pytest.mark.unit
def test_words_outside_the_clip_are_dropped() -> None:
    cues = group_into_cues(
        words(("before", 5.0, 5.5), ("inside", 12.0, 12.5), ("after", 40.0, 40.5)),
        style=STYLE,
        clip_start_sec=10.0,
        clip_end_sec=20.0,
    )
    assert [w.text for cue in cues for w in cue.words] == ["inside"]


@pytest.mark.unit
def test_a_word_straddling_the_boundary_is_clamped_not_dropped() -> None:
    cues = group_into_cues(
        words(("straddling", 9.5, 10.5)),
        style=STYLE,
        clip_start_sec=10.0,
        clip_end_sec=20.0,
    )
    assert cues[0].start_sec == pytest.approx(0.0)
    assert cues[0].words[0].end_sec == pytest.approx(0.5)


@pytest.mark.unit
def test_cues_are_grouped_by_character_budget_not_word_count() -> None:
    """'and' and 'extraordinarily' occupy very different amounts of a 1080-pixel
    line."""
    tight = CaptionStyle(max_chars_per_line=10, max_lines=1)
    long_words = words(*[(f"extraordinarily{i}", i * 1.0, i * 1.0 + 0.9) for i in range(4)])

    cues = group_into_cues(long_words, style=tight, clip_start_sec=0.0, clip_end_sec=10.0)

    assert len(cues) == 4, "each long word should get its own cue"


@pytest.mark.unit
def test_a_pause_breaks_a_cue_even_when_the_characters_would_fit() -> None:
    """Reading across a silence feels wrong even when it fits."""
    cues = group_into_cues(
        words(("one", 0.0, 0.3), ("two", 0.3, 0.6), ("three", 5.0, 5.3)),
        style=STYLE,
        clip_start_sec=0.0,
        clip_end_sec=10.0,
    )
    assert len(cues) == 2


@pytest.mark.unit
def test_karaoke_durations_are_centiseconds() -> None:
    """Milliseconds here is a factor-of-ten desync that grows across the clip and
    looks like a transcription problem."""
    cues = group_into_cues(
        words(("half", 0.0, 0.5)), style=STYLE, clip_start_sec=0.0, clip_end_sec=5.0
    )
    ass = build_ass(cues, style=STYLE)
    assert "{\\k50}half" in ass, "half a second should be 50 centiseconds"


@pytest.mark.unit
def test_a_gap_before_a_word_is_held_on_the_previous_highlight() -> None:
    """Otherwise the karaoke sweep runs ahead of the speaker during a pause."""
    cues = group_into_cues(
        words(("one", 0.0, 0.2), ("two", 0.5, 0.7)),
        style=STYLE,
        clip_start_sec=0.0,
        clip_end_sec=5.0,
        max_gap_sec=1.0,
    )
    ass = build_ass(cues, style=STYLE)
    # 0.3s gap + 0.2s word = 0.5s = 50cs
    assert "{\\k50}two" in ass


@pytest.mark.unit
def test_braces_in_a_transcript_do_not_become_override_blocks() -> None:
    """ASS treats braces as style overrides. A transcript containing one and
    silently swallowing the rest of the line is the kind of bug that only shows
    up on someone else's video."""
    cues = group_into_cues(
        words(("{weird}", 0.0, 0.5)), style=STYLE, clip_start_sec=0.0, clip_end_sec=5.0
    )
    ass = build_ass(cues, style=STYLE)
    assert "\\{weird\\}" in ass


@pytest.mark.unit
def test_the_ass_document_declares_the_output_resolution() -> None:
    """Font sizes are in PlayRes units; a mismatch scales every caption wrongly."""
    ass = build_ass([], style=STYLE)
    assert "PlayResX: 1080" in ass
    assert "PlayResY: 1920" in ass


@pytest.mark.unit
def test_timestamps_use_ass_centisecond_format() -> None:
    cues = group_into_cues(
        words(("x", 61.5, 62.0)), style=STYLE, clip_start_sec=0.0, clip_end_sec=120.0
    )
    assert "0:01:01.50" in build_ass(cues, style=STYLE)


# ─────────────────────────────────────────────────────────────────────────────
# Profiles
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_the_shipped_default_profile_loads() -> None:
    profile = load_profile("default")
    assert profile.name == "default"
    assert profile.captions.karaoke is True


@pytest.mark.unit
def test_the_shipped_bold_profile_differs_from_default() -> None:
    assert load_profile("bold").captions.font_size != load_profile("default").captions.font_size


@pytest.mark.unit
def test_a_missing_profile_degrades_to_the_default(tmp_path: Path) -> None:
    """RENDER is the last stage of a pipeline that already spent GPU minutes.
    Failing the whole job over a styling typo would throw away work that is
    entirely fine."""
    profile = load_profile("nonexistent", directory=tmp_path)
    assert profile.name == "default"


@pytest.mark.unit
def test_a_malformed_profile_degrades_to_the_default(tmp_path: Path) -> None:
    (tmp_path / "broken.toml").write_text("this is not = valid toml [[[", encoding="utf-8")
    assert load_profile("broken", directory=tmp_path).name == "default"


@pytest.mark.unit
def test_the_profile_identifier_carries_its_version(tmp_path: Path) -> None:
    """Stamped on every clip, so 'why does this one look different' stays
    answerable after a profile edit."""
    (tmp_path / "custom.toml").write_text('version = "v7"\n', encoding="utf-8")
    assert load_profile("custom", directory=tmp_path).identifier == "custom:v7"


# ─────────────────────────────────────────────────────────────────────────────
# The filtergraph
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_the_filter_order_is_crop_then_scale_then_captions(tmp_path: Path) -> None:
    """Captions are burned AFTER scaling, so profile font sizes are in output
    pixels and mean the same thing on any source resolution."""
    subs = tmp_path / "captions.ass"
    subs.write_text("", encoding="utf-8")

    graph = build_filtergraph(landscape(), RenderProfile(), subs)
    parts = graph.split(",")

    crop = next(i for i, p in enumerate(parts) if p.startswith("crop="))
    scale = next(i for i, p in enumerate(parts) if p.startswith("scale="))
    subtitles = next(i for i, p in enumerate(parts) if p.startswith("subtitles="))
    assert crop < scale < subtitles


@pytest.mark.unit
def test_the_output_is_always_1080_by_1920() -> None:
    assert "scale=1080:1920" in build_filtergraph(landscape(), RenderProfile(), None)


@pytest.mark.unit
@pytest.mark.parametrize("crop", ["centre", "left", "right"])
def test_each_crop_mode_produces_a_distinct_window(crop: str) -> None:
    graph = build_filtergraph(landscape(), RenderProfile(crop=crop), None)  # type: ignore[arg-type]
    assert graph.startswith("crop=")


@pytest.mark.unit
def test_the_three_crop_modes_differ_from_one_another() -> None:
    graphs = {
        mode: build_filtergraph(landscape(), RenderProfile(crop=mode), None)
        for mode in ("centre", "left", "right")
    }
    assert len(set(graphs.values())) == 3


@pytest.mark.unit
def test_an_already_vertical_source_is_not_pillarboxed() -> None:
    """Cropping width on a 9:16 source would leave black bars where there is
    perfectly good picture.

    Asserted on what the crop expression computes rather than on how it is
    written: the reframing moved into `clipforge.media.framing`, which clamps
    the window to the frame instead of branching on the source's shape, and the
    old `crop=iw:` prefix went with it. The guarantee is unchanged.
    """
    graph = build_filtergraph(landscape(1080, 1920), RenderProfile(), None)
    width = graph[len("crop='") :].split("':'")[0]
    scope = {"iw": 1080, "ih": 1920, "min": min}
    kept = eval(width, {"__builtins__": {}}, scope)  # noqa: S307
    assert kept == 1080, "the full width of a 9:16 source must survive the crop"


@pytest.mark.unit
def test_no_subtitles_filter_when_there_are_no_captions() -> None:
    assert "subtitles=" not in build_filtergraph(landscape(), RenderProfile(), None)


@pytest.mark.unit
def test_a_windows_path_is_escaped_for_the_filter_parser(tmp_path: Path) -> None:
    """ffmpeg's filter parser treats a colon as an argument separator, so an
    unescaped drive letter produces 'No such filter' — which says nothing about
    paths."""
    subs = tmp_path / "captions.ass"
    subs.write_text("", encoding="utf-8")

    graph = build_filtergraph(landscape(), RenderProfile(), subs)

    assert "\\:" in graph or ":" not in str(subs.resolve())
    assert "\\\\" not in graph, "backslashes should have become forward slashes"


# ── What a channel always hides ──────────────────────────────────────────────


def test_a_plain_render_has_no_hiding_in_it() -> None:
    """Every clip cut before this existed must still produce the same graph."""
    assert "delogo" not in build_filtergraph(landscape(), RenderProfile(), None)
    assert "overlay" not in build_filtergraph(landscape(), RenderProfile(), None)


def test_a_sources_standing_marks_are_applied_as_it_cuts() -> None:
    """The difference between a feature and a chore.

    A broadcaster's bug is in the same place on every video it publishes, so a
    clip should arrive already clean rather than arrive wrong and cost a
    correction.
    """
    bug = ObscureRegion(x_pct=83.8, y_pct=4.4, w_pct=13.8, h_pct=8.9)
    graph = build_filtergraph(landscape(), RenderProfile(), None, ObscureOptions(regions=[bug]))
    assert "delogo=x=1608:y=48:w=264:h=96" in graph


def test_hiding_comes_before_the_crop_on_the_render_path_too() -> None:
    bug = ObscureRegion(x_pct=83.8, y_pct=4.4, w_pct=13.8, h_pct=8.9)
    graph = build_filtergraph(landscape(), RenderProfile(), None, ObscureOptions(regions=[bug]))
    assert graph.index("delogo=") < graph.index("crop=")


def test_a_source_with_nothing_to_hide_changes_nothing() -> None:
    plain = build_filtergraph(landscape(), RenderProfile(), None)
    empty = build_filtergraph(landscape(), RenderProfile(), None, ObscureOptions())
    assert plain == empty
