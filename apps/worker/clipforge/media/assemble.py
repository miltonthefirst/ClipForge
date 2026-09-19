"""Stitching several rendered segments into one vertical clip.

Everything the ASSEMBLE stage asks ffmpeg to do, with the parts that decide
things kept pure so they can be asserted on without an encode: how long each
segment may be, what the title card says, and the filtergraph that joins them.
A filtergraph is wrong in ways you find after a two-minute encode, which is
exactly why `media/music.py` builds its plan as a value first — and this does
the same.

## Why the concat *filter* and not the concat demuxer

The demuxer wants every input to agree on codec parameters, and segments cut
from different sources do not: one is 25 fps and 44.1 kHz, the next is 60 fps
and 48 kHz. The filter negotiates a common frame rate, sample rate and layout
across its inputs on its own; the one thing it will not do is resize, and every
segment here is already 1080x1920 because `render_clip` made it so.

## Why the title card is a subtitle over a colour

Because libass is already a proven dependency on this machine and drawtext is
not: drawtext needs a font file path that differs per OS, and the full ffmpeg
build's fontconfig is the thing `doctor` does not check. An ASS document with
one centred line, burned onto a `color` source, uses exactly the path every
caption already takes.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from clipforge_contracts import CompileTransition

from clipforge.media.captions import _escape as escape_ass
from clipforge.media.profiles import OUTPUT_HEIGHT, OUTPUT_WIDTH, CaptionStyle, RenderProfile
from clipforge.media.render import SOFTWARE_ENCODER, RenderError, _escape_filter_path
from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = [
    "FADE_SEC",
    "TITLE_CARD_SEC",
    "AssembledClip",
    "build_concat_filter",
    "clamp_window",
    "concat_segments",
    "output_args",
    "render_title_card",
    "segment_budget_sec",
    "title_card_ass",
    "wrap_title",
]

#: How long the opening card stays up. Long enough to read a line, short enough
#: that a viewer who has already decided is not made to wait.
TITLE_CARD_SEC = 2.0
#: Dip-to-black on either side of a join.
FADE_SEC = 0.25
#: The card's background — the brand navy, so it reads as ClipForge's and not
#: as a broken frame.
_CARD_COLOUR = "0x0b1220"
_CARD_FPS = 30


@dataclass(frozen=True)
class AssembledClip:
    path: Path
    duration_sec: float
    size_bytes: int
    width: int = OUTPUT_WIDTH
    height: int = OUTPUT_HEIGHT


# ── Planning ─────────────────────────────────────────────────────────────────


def segment_budget_sec(count: int, *, target_sec: int, max_segment_sec: int) -> float:
    """How long each segment may run so the whole thing lands near the target.

    The target is divided evenly; the per-segment cap wins when it is smaller.
    Never below five seconds, because a segment shorter than that is a flash.
    """
    share = target_sec / max(count, 1)
    return max(5.0, min(float(max_segment_sec), share))


def clamp_window(start_sec: float, end_sec: float, *, max_sec: float) -> tuple[float, float, bool]:
    """Trim an operator-given window to the budget, from the end.

    From the end and not the middle: a person who typed a start time meant it.
    Returns whether anything was trimmed, so the stage can say so.
    """
    start = max(0.0, start_sec)
    end = max(start, end_sec)
    if end - start <= max_sec:
        return start, end, False
    return start, start + max_sec, True


def wrap_title(text: str, *, max_chars: int = 18, max_lines: int = 4) -> list[str]:
    """Break a theme into lines that fit a 1080-wide card at a large size.

    A theme longer than the card holds is cut with an ellipsis rather than
    shrunk: a card is read in two seconds, and a smaller font is not.
    """
    words = text.split()
    lines: list[str] = []
    current = ""
    used = 0
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars or not current:
            current = candidate
        else:
            if len(lines) == max_lines - 1:
                break
            lines.append(current)
            current = word
        used += 1
    if current and len(lines) < max_lines:
        lines.append(current)
    if used < len(words) and lines:
        lines[-1] = lines[-1][: max_chars - 1].rstrip() + "…"
    return lines or [text[:max_chars]]


def title_card_ass(
    text: str,
    *,
    style: CaptionStyle,
    duration_sec: float = TITLE_CARD_SEC,
    width: int = OUTPUT_WIDTH,
    height: int = OUTPUT_HEIGHT,
) -> str:
    """One centred line — or a few — as a complete ASS document.

    Alignment 5 is dead centre. The font is the caption font at a larger size,
    so a card and the captions that follow it look like one design.
    """
    size = int(style.font_size * 1.35)
    lines = wrap_title(text)
    body = "\\N".join(escape_ass(line) for line in lines)
    end = duration_sec
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Card,{style.font_name},{size},{style.primary_colour},{style.highlight_colour},{style.outline_colour},{style.back_colour},-1,0,0,0,100,100,0,0,1,{style.outline},{style.shadow},5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,{_timestamp(end)},Card,,0,0,0,,{{\\fad(200,200)}}{body}
"""


def _timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    centis = round((seconds - int(seconds)) * 100)
    if centis == 100:
        centis = 0
        secs += 1
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def build_concat_filter(
    durations: Sequence[float],
    *,
    transition: CompileTransition,
    fade_sec: float = FADE_SEC,
) -> str:
    """The filtergraph that joins N inputs, in order, into one video and one audio.

    Each input is re-timed from zero and, for FADE, dipped to black at both
    ends. The fades are applied per input rather than as an `xfade` between
    pairs: a dip-to-black is a cut that reads as deliberate, and `xfade` needs
    every input's frame rate to already agree, which is the thing this filter
    is being asked to fix.
    """
    if not durations:
        raise ValueError("nothing to concatenate")
    parts: list[str] = []
    labels: list[str] = []
    for index, duration in enumerate(durations):
        video = [f"[{index}:v]setpts=PTS-STARTPTS"]
        audio = [f"[{index}:a]asetpts=PTS-STARTPTS"]
        if transition is CompileTransition.FADE and duration > fade_sec * 2:
            out_at = max(0.0, duration - fade_sec)
            video.append(f"fade=t=in:st=0:d={fade_sec:.2f}")
            video.append(f"fade=t=out:st={out_at:.3f}:d={fade_sec:.2f}")
            audio.append(f"afade=t=in:st=0:d={fade_sec:.2f}")
            audio.append(f"afade=t=out:st={out_at:.3f}:d={fade_sec:.2f}")
        parts.append(",".join(video) + f"[v{index}]")
        parts.append(",".join(audio) + f"[a{index}]")
        labels.append(f"[v{index}][a{index}]")
    parts.append(f"{''.join(labels)}concat=n={len(durations)}:v=1:a=1[v][a]")
    return ";".join(parts)


# ── ffmpeg ───────────────────────────────────────────────────────────────────


def output_args(profile: RenderProfile, encoder: str) -> list[str]:
    """The encoder arguments every segment here is written with.

    Public because the cartoon renderer writes segments that this module then
    joins, and two copies of the same list is how a concat filter comes to be
    fed two different pixel formats.
    """
    return [
        "-c:v",
        encoder,
        "-b:v",
        profile.video_bitrate,
        "-c:a",
        "aac",
        "-b:a",
        profile.audio_bitrate,
        "-ac",
        "2",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-map_metadata",
        "-1",
        "-fflags",
        "+bitexact",
        "-f",
        "mp4",
    ]


def render_title_card(
    text: str,
    destination: Path,
    *,
    profile: RenderProfile,
    work_dir: Path,
    encoder: str = "h264_nvenc",
    duration_sec: float = TITLE_CARD_SEC,
    ffmpeg_bin: str = "ffmpeg",
    timeout_s: float = 300.0,
) -> AssembledClip:
    """Two seconds of the theme on a plain card, as a segment like any other."""
    work_dir.mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    subtitles = work_dir / "title-card.ass"
    subtitles.write_text(
        title_card_ass(text, style=profile.captions, duration_sec=duration_sec),
        encoding="utf-8",
        newline="\n",
    )
    staging = destination.with_suffix(destination.suffix + ".partial")
    argv = [
        ffmpeg_bin,
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c={_CARD_COLOUR}:s={OUTPUT_WIDTH}x{OUTPUT_HEIGHT}:r={_CARD_FPS}:d={duration_sec:.2f}",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=48000:cl=stereo",
        "-t",
        f"{duration_sec:.2f}",
        "-vf",
        f"subtitles='{_escape_filter_path(subtitles)}'",
        *output_args(profile, encoder),
        str(staging),
    ]
    try:
        _run(argv, staging, timeout_s=timeout_s, what="the title card")
    except _EncoderUnavailableError:
        if encoder == SOFTWARE_ENCODER:
            raise RenderError("ffmpeg could not initialise any encoder") from None
        log.warning(
            "assemble.encoder_unavailable", encoder=encoder, falling_back_to=SOFTWARE_ENCODER
        )
        return render_title_card(
            text,
            destination,
            profile=profile,
            work_dir=work_dir,
            encoder=SOFTWARE_ENCODER,
            duration_sec=duration_sec,
            ffmpeg_bin=ffmpeg_bin,
            timeout_s=timeout_s,
        )
    staging.replace(destination)
    return AssembledClip(
        path=destination, duration_sec=duration_sec, size_bytes=destination.stat().st_size
    )


def concat_segments(
    segments: Sequence[Path],
    destination: Path,
    *,
    durations: Sequence[float],
    transition: CompileTransition,
    profile: RenderProfile,
    encoder: str = "h264_nvenc",
    ffmpeg_bin: str = "ffmpeg",
    timeout_s: float = 1800.0,
) -> AssembledClip:
    """Join rendered segments, in order, into one file."""
    if len(segments) != len(durations):
        raise ValueError("every segment needs a duration")
    if not segments:
        raise RenderError("nothing to assemble")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_suffix(destination.suffix + ".partial")

    argv = [ffmpeg_bin, "-y", "-v", "error"]
    for segment in segments:
        argv += ["-i", str(segment)]
    argv += [
        "-filter_complex",
        build_concat_filter(durations, transition=transition),
        "-map",
        "[v]",
        "-map",
        "[a]",
        *output_args(profile, encoder),
        str(staging),
    ]
    try:
        _run(argv, staging, timeout_s=timeout_s, what="the compilation")
    except _EncoderUnavailableError:
        if encoder == SOFTWARE_ENCODER:
            raise RenderError("ffmpeg could not initialise any encoder") from None
        log.warning(
            "assemble.encoder_unavailable", encoder=encoder, falling_back_to=SOFTWARE_ENCODER
        )
        return concat_segments(
            segments,
            destination,
            durations=durations,
            transition=transition,
            profile=profile,
            encoder=SOFTWARE_ENCODER,
            ffmpeg_bin=ffmpeg_bin,
            timeout_s=timeout_s,
        )
    staging.replace(destination)
    total = float(sum(durations))
    log.info(
        "assemble.done",
        segments=len(segments),
        duration_sec=round(total, 2),
        size_mb=round(destination.stat().st_size / (1024 * 1024), 2),
        encoder=encoder,
    )
    return AssembledClip(
        path=destination, duration_sec=total, size_bytes=destination.stat().st_size
    )


class _EncoderUnavailableError(RuntimeError):
    """The hardware encoder cannot run here; the caller retries in software."""


def _run(argv: list[str], staging: Path, *, timeout_s: float, what: str) -> None:
    from clipforge.media.render import _is_encoder_init_failure

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except FileNotFoundError as exc:
        staging.unlink(missing_ok=True)
        raise RenderError(f"{argv[0]} not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        staging.unlink(missing_ok=True)
        raise RenderError(f"ffmpeg timed out after {timeout_s}s rendering {what}") from exc

    if completed.returncode != 0:
        staging.unlink(missing_ok=True)
        if _is_encoder_init_failure(completed.stderr):
            raise _EncoderUnavailableError(completed.stderr.strip()[:300])
        raise RenderError(f"ffmpeg failed to render {what}: {completed.stderr.strip()[:600]}")
    if not staging.is_file() or staging.stat().st_size == 0:
        staging.unlink(missing_ok=True)
        raise RenderError(f"ffmpeg reported success but produced no output for {what}")
