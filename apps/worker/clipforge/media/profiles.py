"""Render profiles: everything about how a clip looks, as data.

Style is **never** hardcoded in the render pipeline. A profile is a named,
versioned bundle of crop mode, caption styling, safe margins and encoder
settings, and the profile name is stamped on every clip — so "why does this one
look different" is answerable six months later, and changing the look is a data
edit rather than a code change.

Profiles ship as TOML in `assets/profiles/` and are loaded by name. The built-in
defaults are defined here as well, so a missing or malformed file degrades to
something that renders rather than failing a job at the last stage of a
twenty-minute pipeline.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = ["CaptionStyle", "RenderProfile", "load_profile", "profile_dir"]

CropMode = Literal["centre", "left", "right"]

# Vertical 1080x1920. Not configurable: it is what every short-form platform
# expects, and a profile that changed it would produce clips that get letterboxed
# on upload, which looks worse than any styling choice could compensate for.
OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920


@dataclass(frozen=True)
class CaptionStyle:
    """How burned-in captions look.

    Burned in rather than a sidecar track because short-form platforms do not
    reliably render uploaded subtitles, and a clip whose captions do not appear
    is a clip nobody watches with the sound off — which is most of them.
    """

    font_name: str = "Arial"
    font_size: int = 72
    primary_colour: str = "&H00FFFFFF"  # ASS is &HAABBGGRR, not RGB.
    highlight_colour: str = "&H0000D7FF"
    outline_colour: str = "&H00000000"
    back_colour: str = "&HA0000000"
    outline: int = 4
    shadow: int = 2
    bold: bool = True
    # Percentage of frame height from the bottom. Kept clear of the platform's
    # own overlay furniture, which sits lower than people expect.
    margin_v_pct: float = 22.0
    margin_h_pct: float = 8.0
    max_chars_per_line: int = 22
    max_lines: int = 2
    karaoke: bool = True


@dataclass(frozen=True)
class RenderProfile:
    """A complete description of how to turn a candidate into a clip."""

    name: str = "default"
    version: str = "v1"
    crop: CropMode = "centre"
    captions: CaptionStyle = CaptionStyle()
    video_bitrate: str = "6M"
    audio_bitrate: str = "192k"
    # EBU R128. -14 LUFS is what the major short-form platforms normalise to;
    # delivering louder just means they turn it down, with the dynamics already
    # squashed.
    loudness_lufs: float = -14.0
    loudness_true_peak: float = -1.5
    loudness_range: float = 11.0
    fps: int | None = None

    @property
    def identifier(self) -> str:
        """What gets stamped on the clip. Includes the version, so a profile
        edit is visible in the artefact rather than silently changing output."""
        return f"{self.name}:{self.version}"


DEFAULT_PROFILE = RenderProfile()


def profile_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "assets" / "profiles"


def load_profile(name: str, *, directory: Path | None = None) -> RenderProfile:
    """Load a profile by name, falling back to the built-in default.

    Deliberately forgiving. RENDER is the last stage of a pipeline that has
    already spent GPU minutes on transcription and analysis; failing the whole
    job because a styling file has a typo would throw away work that is entirely
    fine. A warning plus a clip that renders is the better trade.
    """
    directory = directory or profile_dir()
    path = directory / f"{name}.toml"

    if not path.is_file():
        if name != DEFAULT_PROFILE.name:
            log.warning("profile.missing", profile=name, falling_back_to="default")
        return DEFAULT_PROFILE

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning("profile.unreadable", profile=name, error=str(exc))
        return DEFAULT_PROFILE

    return _from_mapping(name, raw)


def _from_mapping(name: str, raw: dict[str, Any]) -> RenderProfile:
    caption_raw = raw.get("captions")
    captions = DEFAULT_PROFILE.captions
    if isinstance(caption_raw, dict):
        known = {f: caption_raw[f] for f in _fields(CaptionStyle) if f in caption_raw}
        captions = replace(DEFAULT_PROFILE.captions, **known)

    known_profile = {
        f: raw[f] for f in _fields(RenderProfile) if f in raw and f not in {"captions", "name"}
    }
    return replace(DEFAULT_PROFILE, name=name, captions=captions, **known_profile)


def _fields(cls: type) -> tuple[str, ...]:
    return tuple(cls.__dataclass_fields__)  # type: ignore[attr-defined]
