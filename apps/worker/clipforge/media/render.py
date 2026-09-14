"""The ffmpeg render: a candidate window becomes a finished vertical clip.

The pipeline, in order, and why each step is where it is:

1. **Seek and cut, re-encoding.** The seek goes *before* `-i` so ffmpeg jumps
   rather than decoding from zero, and the cut re-encodes rather than
   stream-copying. Stream copy can only cut on keyframes, which on a typical
   YouTube source means being wrong by up to several seconds — which would
   discard all of Phase 5's boundary snapping.
2. **Crop to 9:16, then scale.** Cropping first means the scale operates on
   fewer pixels, and crucially it means the crop is expressed in *source*
   coordinates where the choice of left/centre/right is meaningful.
3. **Burn in captions.** After scaling, so font sizes in the profile are in
   output pixels and mean the same thing regardless of source resolution.
4. **Normalise loudness.** EBU R128 to -14 LUFS, which is what the short-form
   platforms normalise to anyway — delivering louder just means they turn it
   down with the dynamics already squashed.
5. **Encode.** NVENC by default, with x264 available by configuration.

Determinism matters: re-rendering the same candidate with the same profile must
produce byte-identical output, so `-map_metadata -1` and a fixed encoder
configuration strip the timestamps that would otherwise differ per run.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from clipforge_contracts import ObscureOptions

from clipforge.media.ffprobe import MediaInfo
from clipforge.media.framing import build_video_chain
from clipforge.media.profiles import OUTPUT_HEIGHT, OUTPUT_WIDTH, RenderProfile
from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = [
    "SOFTWARE_ENCODER",
    "RenderError",
    "RenderRequest",
    "RenderResult",
    "build_filtergraph",
    "render_clip",
]

# The fallback when a hardware encoder will not initialise. Always available: it
# is built into every ffmpeg worth having, and the reference machine's i9 renders
# a 60-second clip on it comfortably.
SOFTWARE_ENCODER = "libx264"

# Substrings that mean "this encoder cannot run here", as opposed to "this clip
# is wrong". Matched loosely on purpose: the exact wording varies by ffmpeg
# version, and a missed match costs a whole pipeline run.
_ENCODER_INIT_FAILURES = (
    "cannot load nvcuda",
    "driver does not support",
    "minimum required nvidia driver",
    "no capable devices found",
    "openencodesessionex failed",
    "error while opening encoder",
    "unknown encoder",
    "not implemented",
)


class RenderError(RuntimeError):
    """ffmpeg could not produce the clip."""


@dataclass(frozen=True)
class RenderRequest:
    source: Path
    destination: Path
    start_sec: float
    end_sec: float
    profile: RenderProfile
    subtitles: Path | None = None
    encoder: str = "h264_nvenc"
    # A prepared video filtergraph, overriding the one this module would build.
    # REMAKE resolves framing and captions together — a tracked crop and the
    # subtitle filter have to compose into one graph — and passes the result
    # here rather than handing over the pieces and hoping they recombine the
    # same way. None keeps the ordinary path, which is every CLIP render.
    video_filter: str | None = None
    # Regions to hide, in source coordinates. Set by RENDER from the source's
    # own `obscure` setting — a channel bug is a property of the channel, so a
    # clip cut from it should arrive with the bug already gone rather than
    # arrive wrong and cost a correction. Ignored when `video_filter` is set,
    # because REMAKE has already folded its own regions into that graph.
    obscure: ObscureOptions | None = None

    @property
    def duration_sec(self) -> float:
        return max(0.0, self.end_sec - self.start_sec)


@dataclass(frozen=True)
class RenderResult:
    path: Path
    duration_sec: float
    size_bytes: int
    width: int = OUTPUT_WIDTH
    height: int = OUTPUT_HEIGHT


def build_filtergraph(
    media: MediaInfo,
    profile: RenderProfile,
    subtitles: Path | None,
    obscure: ObscureOptions | None = None,
) -> str:
    """The video filter chain, as one string.

    Built separately from the ffmpeg invocation so it can be asserted on in a
    unit test without running anything — the ordering of these filters is the
    part most likely to be wrong, and the least obvious from the output.

    The reframing itself lives in `clipforge.media.framing`, which grew out of
    this function when a fixed crop stopped being the only answer. This is the
    no-framing-request case of that one: pass `framing=None` and you get the
    fixed 9:16 window the profile asks for, which is exactly what every clip got
    before REMAKE existed. Keeping one implementation matters more than the
    indirection costs — two crop expressions that are supposed to agree are two
    crop expressions that will eventually not.
    """
    return build_video_chain(
        media=media,
        profile=profile,
        framing=None,
        subtitles_expr=(
            # Captions are burned after scaling so profile font sizes are in
            # output pixels and mean the same thing on any source resolution.
            f"subtitles='{_escape_filter_path(subtitles)}'" if subtitles is not None else None
        ),
        obscure=obscure,
    )


def _escape_filter_path(path: Path) -> str:
    """ffmpeg's filter parser needs Windows paths escaped twice over.

    `C:\\x` becomes `C\\:/x`: the colon separates filter arguments and the
    backslash is the filter escape character. Getting this wrong produces
    "No such filter" rather than anything about paths.
    """
    text = str(path.resolve()).replace("\\", "/")
    return text.replace(":", "\\:")


def render_clip(
    request: RenderRequest,
    media: MediaInfo,
    *,
    ffmpeg_bin: str = "ffmpeg",
    timeout_s: float = 1800.0,
) -> RenderResult:
    """Cut, reframe, caption, normalise and encode one clip."""
    if request.duration_sec <= 0:
        raise RenderError(f"clip has no duration: {request.start_sec} to {request.end_sec}")

    request.destination.parent.mkdir(parents=True, exist_ok=True)
    staging = request.destination.with_suffix(request.destination.suffix + ".partial")

    loudnorm = (
        f"loudnorm=I={request.profile.loudness_lufs}"
        f":TP={request.profile.loudness_true_peak}"
        f":LRA={request.profile.loudness_range}"
    )

    argv = [
        ffmpeg_bin,
        "-y",
        "-v",
        "error",
        # Before -i: ffmpeg seeks rather than decoding from zero. On a 60-minute
        # source that is the difference between seconds and minutes.
        "-ss",
        f"{request.start_sec:.3f}",
        "-to",
        f"{request.end_sec:.3f}",
        "-i",
        str(request.source),
        "-vf",
        request.video_filter
        or build_filtergraph(media, request.profile, request.subtitles, request.obscure),
        "-af",
        loudnorm,
        "-c:v",
        request.encoder,
        "-b:v",
        request.profile.video_bitrate,
        "-c:a",
        "aac",
        "-b:a",
        request.profile.audio_bitrate,
        "-ac",
        "2",
        "-pix_fmt",
        "yuv420p",
        # Required for iOS Safari to play the file at all: without it the moov
        # atom sits at the end and Safari refuses to start.
        "-movflags",
        "+faststart",
        # Strip source metadata and encoder timestamps, so re-rendering the same
        # candidate produces byte-identical output.
        "-map_metadata",
        "-1",
        "-fflags",
        "+bitexact",
        "-f",
        "mp4",
        str(staging),
    ]

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except FileNotFoundError as exc:
        staging.unlink(missing_ok=True)
        raise RenderError(f"{ffmpeg_bin} not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        staging.unlink(missing_ok=True)
        raise RenderError(f"ffmpeg timed out after {timeout_s}s") from exc

    if completed.returncode != 0:
        staging.unlink(missing_ok=True)
        # A hardware encoder that cannot initialise is an environment problem,
        # not a problem with this clip — and it fails identically for every
        # subsequent one. Falling back to software costs encode time; failing
        # costs the entire pipeline run that produced this candidate.
        if _is_encoder_init_failure(completed.stderr) and request.encoder != SOFTWARE_ENCODER:
            log.warning(
                "render.encoder_unavailable",
                encoder=request.encoder,
                falling_back_to=SOFTWARE_ENCODER,
                hint=_encoder_hint(completed.stderr),
            )
            return render_clip(
                replace(request, encoder=SOFTWARE_ENCODER),
                media,
                ffmpeg_bin=ffmpeg_bin,
                timeout_s=timeout_s,
            )
        raise RenderError(f"ffmpeg failed to render the clip: {completed.stderr.strip()[:600]}")

    if not staging.is_file() or staging.stat().st_size == 0:
        staging.unlink(missing_ok=True)
        raise RenderError("ffmpeg reported success but produced no output")

    staging.replace(request.destination)
    log.info(
        "render.done",
        duration_sec=round(request.duration_sec, 2),
        size_mb=round(request.destination.stat().st_size / (1024 * 1024), 2),
        encoder=request.encoder,
    )
    return RenderResult(
        path=request.destination,
        duration_sec=request.duration_sec,
        size_bytes=request.destination.stat().st_size,
    )


def _is_encoder_init_failure(stderr: str) -> bool:
    """Distinguish "this encoder cannot run here" from "this clip is wrong".

    Falling back on a genuine parameter error would hide a real bug behind a
    slower encode, so the match is on encoder-initialisation wording only.
    """
    lowered = stderr.lower()
    return any(needle in lowered for needle in _ENCODER_INIT_FAILURES)


def _encoder_hint(stderr: str) -> str:
    """The most useful line of ffmpeg's complaint, for the log."""
    for line in stderr.splitlines():
        lowered = line.lower()
        if "driver" in lowered or "nvenc" in lowered:
            return line.strip()[:200]
    return stderr.strip().splitlines()[0][:200] if stderr.strip() else ""
