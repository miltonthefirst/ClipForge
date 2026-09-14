"""Putting a synthesised voice onto a clip.

The invariant worth most of these tests is negative: **the picture is never
retimed to fit the audio.** A translated script routinely runs 20-30% longer
than the original, and the obvious accommodations — stretching the video,
nudging the out-point — produce a published clip that is not the clip anybody
approved. An overrun is reported, not absorbed.
"""

from __future__ import annotations

import pytest
from clipforge.media.narration import (
    DEFAULT_DUCK_DB,
    NarrationPlan,
    build_audio_filter,
    build_ffmpeg_args,
    plan_narration,
)
from clipforge_contracts import SpeechMode

# Every test in this file is the unit tier. Without this marker CI's
# `pytest -m unit` silently deselects the whole file — the tests pass locally,
# run nowhere, and protect nothing.
pytestmark = pytest.mark.unit


def plan(**overrides: object) -> NarrationPlan:
    defaults = {
        "mode": SpeechMode.REPLACE,
        "clip_duration_sec": 30.0,
        "speech_duration_sec": 28.0,
        "gain_db": None,
        "duck_db": None,
        "has_original_audio": True,
    }
    return plan_narration(**{**defaults, **overrides})  # type: ignore[arg-type]


# ── Overruns ─────────────────────────────────────────────────────────────────


def test_narration_that_fits_is_not_an_overrun() -> None:
    assert plan(speech_duration_sec=28.0).fits
    assert plan(speech_duration_sec=28.0).overruns_by_sec == 0


def test_narration_that_runs_long_is_reported_in_seconds() -> None:
    long = plan(speech_duration_sec=39.0)
    assert not long.fits
    assert long.overruns_by_sec == pytest.approx(9.0)


def test_the_picture_length_is_the_clip_length_however_long_the_speech_is() -> None:
    """The rule the whole module exists to hold.

    `-t` is the clip's duration in both cases; nothing about the narration is
    allowed to change it.
    """
    short = build_ffmpeg_args(
        plan(speech_duration_sec=5.0),
        clip_path="c.mp4",
        speech_path="n.wav",
        output_path="o.mp4",
        reencode_video=False,
    )
    over = build_ffmpeg_args(
        plan(speech_duration_sec=90.0),
        clip_path="c.mp4",
        speech_path="n.wav",
        output_path="o.mp4",
        reencode_video=False,
    )
    assert short[short.index("-t") + 1] == "30.000"
    assert over[over.index("-t") + 1] == "30.000"


def test_a_short_narration_is_padded_rather_than_ending_the_mix_early() -> None:
    assert "apad" in build_audio_filter(plan(speech_duration_sec=5.0))


def test_a_long_narration_is_trimmed_at_the_picture_s_end() -> None:
    assert "atrim=duration=30.0" in build_audio_filter(plan(speech_duration_sec=90.0))


# ── Modes ────────────────────────────────────────────────────────────────────


def test_replace_leaves_the_original_audio_out_of_the_graph_entirely() -> None:
    """Absent, not mixed silently.

    A muted input is one bad edit away from being audible; an absent one is
    not.
    """
    graph = build_audio_filter(plan(mode=SpeechMode.REPLACE))
    assert "[0:a]" not in graph
    assert graph.endswith("[aout]")


def test_bed_ducks_the_original_under_the_voice() -> None:
    graph = build_audio_filter(plan(mode=SpeechMode.BED))
    assert "[0:a]" in graph
    assert "sidechaincompress" in graph
    assert "amix=inputs=2" in graph


def test_the_duck_is_dynamic_rather_than_a_flat_trim() -> None:
    """A flat trim is either audible under silence or masking under speech.

    Keying the compressor on the narration means the crowd comes back between
    sentences, which is the entire reason BED is worth having over REPLACE.
    """
    graph = build_audio_filter(plan(mode=SpeechMode.BED))
    assert "asplit=2[voice][key]" in graph
    assert "[bed][key]sidechaincompress" in graph


def test_a_deeper_duck_compresses_harder() -> None:
    shallow = build_audio_filter(plan(mode=SpeechMode.BED, duck_db=-6.0))
    deep = build_audio_filter(plan(mode=SpeechMode.BED, duck_db=-24.0))
    assert shallow != deep
    assert "ratio=4.0" in shallow
    assert "ratio=13.0" in deep


def test_the_default_duck_is_the_documented_one() -> None:
    assert plan(mode=SpeechMode.BED).duck_db == DEFAULT_DUCK_DB


def test_bedding_a_voice_under_silence_becomes_a_replacement() -> None:
    """There is nothing to bed it under.

    Falling back keeps the clip usable rather than failing at the last step of
    a remake, over a property of the source the reviewer could not have known.
    """
    fallen_back = plan(mode=SpeechMode.BED, has_original_audio=False)
    assert fallen_back.mode is SpeechMode.REPLACE
    assert "[0:a]" not in build_audio_filter(fallen_back)


# ── Levels ───────────────────────────────────────────────────────────────────


def test_the_delivered_level_is_set_by_loudnorm_not_by_the_gain() -> None:
    """So the default gain is 0, rather than a number fighting the normaliser."""
    assert plan(gain_db=None).gain_db == 0.0
    for mode in (SpeechMode.REPLACE, SpeechMode.BED):
        assert "loudnorm=I=-14.0" in build_audio_filter(plan(mode=mode))


def test_a_stated_gain_is_honoured() -> None:
    assert "volume=-3.0dB" in build_audio_filter(plan(gain_db=-3.0))


def test_the_mix_does_not_let_amix_halve_the_levels_it_just_chose() -> None:
    assert "normalize=0" in build_audio_filter(plan(mode=SpeechMode.BED))


# ── The invocation ───────────────────────────────────────────────────────────


def test_an_unchanged_picture_is_copied_rather_than_re_encoded() -> None:
    """A voice-only remake should cost seconds, and lose no quality."""
    args = build_ffmpeg_args(
        plan(),
        clip_path="c.mp4",
        speech_path="n.wav",
        output_path="o.mp4",
        reencode_video=False,
    )
    assert args[args.index("-c:v") + 1] == "copy"


def test_a_changed_picture_is_re_encoded() -> None:
    args = build_ffmpeg_args(
        plan(),
        clip_path="c.mp4",
        speech_path="n.wav",
        output_path="o.mp4",
        reencode_video=True,
    )
    assert args[args.index("-c:v") + 1] == "libx264"


def test_the_output_mapping_does_not_depend_on_the_mode() -> None:
    for mode in (SpeechMode.REPLACE, SpeechMode.BED):
        args = build_ffmpeg_args(
            plan(mode=mode),
            clip_path="c.mp4",
            speech_path="n.wav",
            output_path="o.mp4",
            reencode_video=False,
        )
        assert "[aout]" in args
        assert "0:v:0" in args


def test_faststart_is_set_so_the_clip_plays_on_a_phone() -> None:
    args = build_ffmpeg_args(
        plan(),
        clip_path="c.mp4",
        speech_path="n.wav",
        output_path="o.mp4",
        reencode_video=False,
    )
    assert args[args.index("-movflags") + 1] == "+faststart"
