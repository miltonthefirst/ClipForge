"""Where ffmpeg and ffprobe actually are.

## Why this needs a module at all

Because `"ffmpeg"` is not a path, and exactly one caller in this codebase has to
know that.

Everywhere else the binaries are invoked through `subprocess`, which resolves a
bare command name against `PATH` for us. `Settings.ffmpeg_bin` defaults to the
bare name `"ffmpeg"` on that basis and it is correct: the shell knows where the
program is, so we do not have to.

yt-dlp does not work that way. Handed an `ffmpeg_location`, it treats the value
as a filesystem path and checks it with `os.path.exists` — and on failure it
does not fall back to `PATH`, it gives up on **both** ffmpeg and ffprobe::

    if location is None:
        return {p: p for p in programs}     # unset: resolved against PATH
    if not os.path.exists(location):
        self.report_warning('ffmpeg-location ... does not exist!')
        return {}                           # neither ffmpeg NOR ffprobe

`os.path.exists("ffmpeg")` is false, so passing the default setting through was
strictly worse than passing nothing at all. That is how a machine with a working
ffmpeg — one that had rendered clips minutes earlier — produced *"ffprobe and
ffmpeg not found"* on every MUSIC job while every video download kept working.
Downloads never set the option.

So the rule this module encodes: **never hand a bare command name to something
that will path-check it.** Resolve it first, or say nothing and let the other
side search `PATH` itself.

## Why the directory, and why sometimes nothing

yt-dlp accepts either a directory or a full path, and derives the *other* tool
from the same directory. That is right for the normal install, where ffmpeg and
ffprobe are siblings, and wrong for a split install, where naming the directory
would hide whichever one lives elsewhere. In that case the honest answer is to
say nothing: `PATH` can find two tools in two places, and a directory cannot.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = [
    "Toolchain",
    "ToolchainError",
    "find_executable",
    "resolve_toolchain",
    "yt_dlp_location",
]


class ToolchainError(RuntimeError):
    """A tool this machine needs is not installed, or not where it was said to be.

    Never retryable, and that is the point of declaring it: no amount of waiting
    installs ffprobe. A job that fails this way should say so once and stop,
    rather than spend its remaining attempts rediscovering the same absence.
    """

    retryable = False
    code = "TOOLCHAIN_MISSING"


@dataclass(frozen=True)
class Toolchain:
    """Where the media tools resolved to, and what was asked for.

    Both fields are ``None``-able because "not installed" is a real answer that
    callers have to be able to act on. The requested spellings are kept so an
    error message can say *what was looked for* as well as what was not found —
    ``CLIPFORGE_FFPROBE_BIN=/opt/ff/ffprobe`` failing is a different problem
    from ffprobe simply not being installed, and the operator needs to know
    which one they have.
    """

    ffmpeg: Path | None
    ffprobe: Path | None
    requested_ffmpeg: str
    requested_ffprobe: str

    @property
    def complete(self) -> bool:
        return self.ffmpeg is not None and self.ffprobe is not None

    @property
    def missing(self) -> tuple[str, ...]:
        """The requested spellings of whatever could not be found, in order."""
        absent: list[str] = []
        if self.ffmpeg is None:
            absent.append(self.requested_ffmpeg)
        if self.ffprobe is None:
            absent.append(self.requested_ffprobe)
        return tuple(absent)

    def require(self, what: str) -> None:
        """Raise unless both tools are present, naming the ones that are not."""
        if self.complete:
            return
        names = ", ".join(self.missing)
        raise ToolchainError(
            f"{what} needs ffmpeg and ffprobe, and this machine has no {names}. "
            "Install them and make sure they are on PATH, or point "
            "CLIPFORGE_FFMPEG_BIN and CLIPFORGE_FFPROBE_BIN at them."
        )


def find_executable(name_or_path: str) -> Path | None:
    """Resolve a command name or a path to the program it names.

    `shutil.which` is doing the work and is doing both jobs: given a bare name
    it searches `PATH`, and given something with a directory component it checks
    only that. On Windows it also tries the `PATHEXT` suffixes, so `"ffmpeg"`
    finds `ffmpeg.exe` — which is the whole reason this is not a manual
    `Path.is_file()` check.
    """
    candidate = str(Path(name_or_path).expanduser()) if name_or_path else ""
    if not candidate:
        return None
    found = shutil.which(candidate)
    return Path(found).resolve() if found else None


@lru_cache(maxsize=8)
def resolve_toolchain(ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> Toolchain:
    """Find both tools, once per distinct pair of settings.

    Cached because this runs on a path that is otherwise a network download, and
    because the answer does not change while the process lives. `cache_clear`
    exists for tests, which do change it.
    """
    resolved = Toolchain(
        ffmpeg=find_executable(ffmpeg),
        ffprobe=find_executable(ffprobe),
        requested_ffmpeg=ffmpeg,
        requested_ffprobe=ffprobe,
    )
    if not resolved.complete:
        log.warning(
            "toolchain.incomplete",
            missing=list(resolved.missing),
            ffmpeg=str(resolved.ffmpeg) if resolved.ffmpeg else None,
            ffprobe=str(resolved.ffprobe) if resolved.ffprobe else None,
        )
    return resolved


def yt_dlp_location(toolchain: Toolchain) -> str | None:
    """What to give yt-dlp as `ffmpeg_location`, or ``None`` to leave it unset.

    ``None`` is a real answer and the safe default — it makes yt-dlp resolve
    both tools against `PATH`, which is what works on an ordinary install. A
    directory is returned only when it is strictly more informative than that:
    both tools found, and both in the same place. Anything else would tell
    yt-dlp about one tool at the cost of the other.
    """
    if toolchain.ffmpeg is None or toolchain.ffprobe is None:
        return None
    directory = toolchain.ffmpeg.parent
    if toolchain.ffprobe.parent != directory:
        return None
    return str(directory)
