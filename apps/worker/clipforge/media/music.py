"""Putting a track under a clip, or in place of its audio.

The arithmetic of *where* to start the music lives in :mod:`clipforge.media.beats`;
this decides what ffmpeg is asked to do with it. Both halves are pure — the plan
and the filtergraph are values, asserted on in unit tests without running
anything — because an ffmpeg filter chain is exactly the sort of string that is
wrong in a way you only discover after a two-minute encode.

## The two modes

**BED** keeps the clip's own audio and sits the music underneath it, ducked by a
sidechain compressor keyed on the speech. Ducking rather than a fixed low volume
because a fixed level is either audible under silence or masking under speech,
and there is no single number that is both.

**REPLACE** drops the original audio entirely and the track becomes the whole
soundtrack, normalised to the same loudness target a clip's own audio gets.

## What is deliberately *not* done

The clip's video is never re-cut to land on a beat. The music moves to meet the
clip instead: it is started on one of its own beats so the pulse arrives with
the first frame. A listener cannot tell the difference, and it means the
published clip is frame-identical to the one that was reviewed — and needs no
video re-encode at all when captions are being kept.
"""

from __future__ import annotations

from dataclasses import dataclass

from clipforge_contracts import MusicMode

from clipforge.media.beats import BeatAnalysis

__all__ = ["MusicPlan", "build_audio_filter", "plan_music"]

# Where a bed sits relative to speech. Measured in dB of attenuation applied
# before the ducker, which then takes it further down whenever anyone talks.
DEFAULT_BED_GAIN_DB = -14.0

# A replacement track is the whole soundtrack, so it is not attenuated at all —
# loudnorm sets the final level.
DEFAULT_REPLACE_GAIN_DB = 0.0

# The clip's own loudness target, matched so a scored clip and an ordinary one
# sound the same next to each other in a feed.
TARGET_LUFS = -14.0
TARGET_TRUE_PEAK = -1.5

FADE_IN_SEC = 0.4
FADE_OUT_SEC = 1.2


@dataclass(frozen=True)
class MusicPlan:
    """What to take from the track, and how loud to make it."""

    music_start_sec: float
    duration_sec: float
    mode: MusicMode
    gain_db: float
    tempo_bpm: float | None
    loop: bool
    """Whether the track is shorter than the clip and has to repeat to cover it."""


def plan_music(
    analysis: BeatAnalysis,
    *,
    clip_duration_sec: float,
    mode: MusicMode,
    gain_db: float | None = None,
    align_to_beat: bool = True,
) -> MusicPlan:
    """Choose the excerpt and the level.

    The start is the most energetic window of the right length — a track's
    opening bars are usually the least interesting part of it — moved back onto
    a beat when there is a tempo to move it onto.
    """
    from clipforge.media.beats import pick_section

    start = pick_section(analysis, clip_duration_sec)

    if align_to_beat and analysis.beat_times:
        # Onto the nearest beat, in either direction: the excerpt has already
        # been chosen, and this is a sub-beat adjustment to its phase.
        start = min(analysis.beat_times, key=lambda t: abs(t - start))
        # Never past the point where the excerpt would overrun the track.
        start = min(start, max(0.0, analysis.duration_sec - clip_duration_sec))

    default_gain = DEFAULT_BED_GAIN_DB if mode is MusicMode.BED else DEFAULT_REPLACE_GAIN_DB

    return MusicPlan(
        music_start_sec=round(start, 3),
        duration_sec=round(clip_duration_sec, 3),
        mode=mode,
        gain_db=default_gain if gain_db is None else float(gain_db),
        tempo_bpm=analysis.tempo_bpm,
        loop=analysis.duration_sec < clip_duration_sec,
    )


def _music_chain(plan: MusicPlan) -> str:
    """Trim, level and fade the track. Shared by both modes."""
    fade_out_at = max(0.0, plan.duration_sec - FADE_OUT_SEC)
    steps = [
        # `atrim` after any looping, so the excerpt is measured in the looped
        # stream's own time.
        f"atrim=start={plan.music_start_sec}:duration={plan.duration_sec}",
        # Timestamps restart at zero or the mix begins with a silence as long as
        # the offset into the track.
        "asetpts=PTS-STARTPTS",
        f"volume={plan.gain_db}dB",
        f"afade=t=in:d={FADE_IN_SEC}",
        f"afade=t=out:st={round(fade_out_at, 3)}:d={FADE_OUT_SEC}",
    ]
    return ",".join(steps)


def build_audio_filter(plan: MusicPlan) -> str:
    """The `-filter_complex` for this plan.

    Input 0 is the clip, input 1 the track. The output is always labelled
    ``[aout]`` so the caller's mapping does not depend on the mode.
    """
    music = _music_chain(plan)

    if plan.mode is MusicMode.REPLACE:
        # No original audio in the graph at all — not mixed silently, absent.
        # loudnorm last, so the target applies to what will actually be heard.
        return f"[1:a]{music},loudnorm=I={TARGET_LUFS}:TP={TARGET_TRUE_PEAK}:LRA=11[aout]"

    # BED. The speech is used twice: once as an input to the mix, and once as
    # the sidechain key that pushes the music down whenever it is present.
    return (
        f"[1:a]{music}[music];"
        "[0:a]asplit=2[speech][key];"
        # threshold/ratio chosen so ordinary speech takes the bed down by
        # roughly 9 dB and it recovers over a third of a second — fast enough to
        # fill a pause, slow enough not to pump between words.
        "[music][key]sidechaincompress="
        "threshold=0.03:ratio=6:attack=15:release=350:makeup=1[ducked];"
        # `normalize=0` because amix's default halves every input to avoid
        # clipping, which would quietly undo the level this stage just chose.
        "[speech][ducked]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
    )


def build_ffmpeg_args(
    plan: MusicPlan,
    *,
    clip_path: str,
    music_path: str,
    output_path: str,
    reencode_video: bool,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """The whole invocation.

    ``reencode_video`` is False whenever the video is unchanged, which is the
    common case: keeping the captions means the picture is already exactly
    right, and copying the stream is both instant and lossless. It is True only
    when the caller has re-rendered the picture for some other reason.
    """
    args = [ffmpeg, "-nostdin", "-v", "error", "-y"]

    args += ["-i", clip_path]

    # `-stream_loop` belongs to the input it precedes, so it has to sit here
    # rather than in the filtergraph — and looping a short track is the only way
    # a thirty-second clip gets thirty seconds of music out of a ten-second loop.
    if plan.loop:
        args += ["-stream_loop", "-1"]
    args += ["-i", music_path]

    args += ["-filter_complex", build_audio_filter(plan)]
    args += ["-map", "0:v:0", "-map", "[aout]"]

    if reencode_video:
        args += ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]
    else:
        args += ["-c:v", "copy"]

    args += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    # The looped music input is infinite; without this the output would be too.
    args += ["-t", str(plan.duration_sec)]
    args += ["-movflags", "+faststart", output_path]
    return args
