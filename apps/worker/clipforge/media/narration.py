"""Putting a synthesised voice onto a finished clip.

The audio half of a remake. It mirrors `clipforge.media.music` closely on
purpose — the same two-mode shape, the same sidechain duck, the same habit of
labelling the mix `[aout]` so the caller's mapping does not depend on which mode
ran — because they are the same problem with a different second input, and two
different-looking solutions to one problem is how the second one rots.

## The one rule that is not negotiable

**The picture is never retimed to fit the narration.** A translated script is
routinely 20-30% longer than the original — German and Spanish especially — and
the obvious fix, stretching the video or nudging the clip's out-point, produces
a published clip that is not the clip anybody approved. So narration that
overruns is reported as an overrun and trimmed at the boundary, and the reviewer
decides whether to shorten the script, raise the speed or let it end early.
Silently changing the video to accommodate the audio is the one behaviour that
would make the review step meaningless.
"""

from __future__ import annotations

from dataclasses import dataclass

from clipforge_contracts import SpeechMode

__all__ = [
    "DEFAULT_DUCK_DB",
    "NarrationPlan",
    "build_audio_filter",
    "build_ffmpeg_args",
    "plan_narration",
]

# Where the clip's own audio sits under the voice when it is ducked. -12 dB
# keeps a crowd present and a ball audible without either competing with a
# sentence.
DEFAULT_DUCK_DB = -12.0

# The short fade that stops a narration ending on a hard cut, which reads as a
# dropout rather than as an ending.
FADE_OUT_SEC = 0.25

TARGET_LUFS = -14.0
TARGET_TRUE_PEAK = -1.5


@dataclass(frozen=True)
class NarrationPlan:
    """What the mix will do, decided before any of it runs."""

    mode: SpeechMode
    clip_duration_sec: float
    speech_duration_sec: float
    gain_db: float
    duck_db: float
    has_original_audio: bool

    @property
    def overruns_by_sec(self) -> float:
        """How far the narration runs past the end of the picture.

        Zero when it fits. Surfaced rather than corrected — see the module
        docstring — and carried onto the clip so the UI can say so before
        anybody watches to the end and finds out.
        """
        return max(0.0, self.speech_duration_sec - self.clip_duration_sec)

    @property
    def fits(self) -> bool:
        return self.overruns_by_sec <= 0.05


def plan_narration(
    *,
    mode: SpeechMode,
    clip_duration_sec: float,
    speech_duration_sec: float,
    gain_db: float | None,
    duck_db: float | None,
    has_original_audio: bool,
) -> NarrationPlan:
    """Settle every number the mix needs.

    Separated from the ffmpeg call so the decisions are assertable without
    running anything — which matters here because the interesting cases are
    arithmetic (an overrun, a silent source) rather than encoding.
    """
    effective_mode = mode
    if mode is SpeechMode.BED and not has_original_audio:
        # Nothing to bed the voice under. Falling back keeps the clip usable
        # instead of failing at the last step of a remake over something the
        # reviewer could not have known about the source.
        effective_mode = SpeechMode.REPLACE

    return NarrationPlan(
        mode=effective_mode,
        clip_duration_sec=max(0.0, clip_duration_sec),
        speech_duration_sec=max(0.0, speech_duration_sec),
        # 0 dB by default: the loudnorm at the end of the chain is what sets the
        # delivered level, so a default trim here would be fighting it.
        gain_db=0.0 if gain_db is None else float(gain_db),
        duck_db=DEFAULT_DUCK_DB if duck_db is None else float(duck_db),
        has_original_audio=has_original_audio,
    )


def build_audio_filter(plan: NarrationPlan) -> str:
    """The `-filter_complex` for this plan.

    Input 0 is the clip, input 1 the narration. The output is always `[aout]`.
    """
    fade_at = max(0.0, plan.clip_duration_sec - FADE_OUT_SEC)
    voice = (
        f"[1:a]asetpts=PTS-STARTPTS,volume={plan.gain_db}dB,"
        # Padded, then cut at the clip's length: a narration shorter than the
        # picture must not end the mix early, and one longer than it must not
        # extend the picture. Both are handled here rather than by `-t` alone,
        # which would only address the second.
        f"apad,atrim=duration={plan.clip_duration_sec},"
        f"afade=t=out:st={round(fade_at, 3)}:d={FADE_OUT_SEC}"
    )

    if plan.mode is SpeechMode.REPLACE:
        # The clip's own audio is not in the graph at all — absent, not mixed
        # silently, so there is no chance of it leaking back at some level.
        return f"{voice},loudnorm=I={TARGET_LUFS}:TP={TARGET_TRUE_PEAK}:LRA=11[aout]"

    # BED. The voice is used twice: once as an input to the mix, and once as the
    # sidechain key that pushes the original down while it is speaking. Dynamic
    # rather than a flat trim because a flat one is either audible under silence
    # or masking under speech, and no single number is both.
    return (
        f"{voice}[voice_src];"
        "[voice_src]asplit=2[voice][key];"
        f"[0:a]asetpts=PTS-STARTPTS[bed];"
        f"[bed][key]sidechaincompress="
        f"threshold=0.03:ratio={_ratio_for(plan.duck_db)}:attack=15:release=350:makeup=1[ducked];"
        # `normalize=0` because amix's default halves every input to avoid
        # clipping, which would quietly undo the levels chosen above.
        "[voice][ducked]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
        f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TRUE_PEAK}:LRA=11[aout]"
    )


def _ratio_for(duck_db: float) -> float:
    """Turn a wanted reduction into a compressor ratio.

    An approximation, and worth naming as one: the reduction a compressor
    actually applies depends on how far the key signal sits above the threshold,
    which depends on the narration's own level. This maps the range a person
    would reasonably ask for (-6 to -24 dB) onto ratios that produce roughly
    that much duck on speech at a normalised level, and clamps outside it.
    Precise enough for the purpose, and the alternative — measuring the key and
    solving for the ratio — would be a second loudness pass for a difference
    nobody can hear.
    """
    wanted = abs(float(duck_db))
    return round(max(1.5, min(20.0, 1.0 + wanted / 2.0)), 2)


def build_ffmpeg_args(
    plan: NarrationPlan,
    *,
    clip_path: str,
    speech_path: str,
    output_path: str,
    reencode_video: bool,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """The whole invocation.

    `reencode_video` is False whenever the picture is untouched, which is the
    case for a voice-only remake: the video stream is copied, which is instant
    and lossless. A remake that also reframes has already re-encoded the picture
    by the time this runs, and passes its result in as `clip_path`.
    """
    args = [ffmpeg, "-nostdin", "-v", "error", "-y", "-i", clip_path, "-i", speech_path]
    args += ["-filter_complex", build_audio_filter(plan)]
    args += ["-map", "0:v:0", "-map", "[aout]"]

    if reencode_video:
        args += ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]
    else:
        args += ["-c:v", "copy"]

    args += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    # The picture's length is the clip's length, whatever the narration does.
    args += ["-t", f"{plan.clip_duration_sec:.3f}"]
    args += ["-movflags", "+faststart", output_path]
    return args
