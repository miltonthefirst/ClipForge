"""Cutting one stretch out of a finished clip, frame-accurately.

Re-encoded rather than stream-copied. A copy can only start on a keyframe, and a
rendered clip has one every couple of seconds, so "35 seconds" would really mean
"somewhere up to two seconds before 35" — and the part before it would repeat
that stretch. The settings are the ones every other re-encode of a finished clip
in this codebase uses (narration, music), so a part looks like its siblings.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

__all__ = ["CUT_TIMEOUT_S", "SplitCutError", "build_cut_args", "cut_part"]

#: Generous for a clip of a minute or two on the CPU encoder. A hung ffmpeg is
#: the one failure here a retry fixes, so it is raised as a TimeoutError.
CUT_TIMEOUT_S = 600


class SplitCutError(RuntimeError):
    """ffmpeg could not cut the part."""


def build_cut_args(
    *,
    source: str,
    destination: str,
    start_sec: float,
    end_sec: float,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """The whole invocation, separate from running it so it can be asserted on.

    `-ss` before `-i` seeks the input, which with a re-encode is exact: ffmpeg
    decodes from the keyframe before the mark and discards what precedes it.
    `-t` rather than `-to`, because after an input seek the output timeline
    starts at zero and `-to` would be read against that.
    """
    args = [ffmpeg, "-nostdin", "-v", "error", "-y"]
    args += ["-ss", f"{max(0.0, start_sec):.3f}", "-i", source]
    args += ["-t", f"{max(0.0, end_sec - max(0.0, start_sec)):.3f}"]
    # The audio is optional: a clip whose voice was replaced with silence, or a
    # source that had none, still has a picture worth cutting.
    args += ["-map", "0:v:0", "-map", "0:a:0?"]
    args += ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]
    args += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    args += ["-movflags", "+faststart", destination]
    return args


def cut_part(
    source: Path,
    destination: Path,
    *,
    start_sec: float,
    end_sec: float,
    ffmpeg_bin: str = "ffmpeg",
    timeout_s: int = CUT_TIMEOUT_S,
) -> Path:
    """Write `source` from `start_sec` to `end_sec` to `destination`."""
    argv = build_cut_args(
        source=str(source),
        destination=str(destination),
        start_sec=start_sec,
        end_sec=end_sec,
        ffmpeg=ffmpeg_bin,
    )
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"cutting a part timed out after {timeout_s}s") from exc
    if done.returncode != 0 or not destination.is_file():
        raise SplitCutError(f"the part would not cut: {done.stderr.strip()[:400]}")
    return destination
