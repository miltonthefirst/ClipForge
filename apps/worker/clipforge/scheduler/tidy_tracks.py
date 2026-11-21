"""Binning the files of tracks a person deleted (docs/MUSIC-PLAN.md, M7).

Deleting a track on the Music page removes its record at once, from any
device; the file is on the PC, which the phone cannot reach. So, as with
clips (:mod:`clipforge.scheduler.tidy`), the record and the file go
separately: this pass, on the worker, moves every song file and painted
background that no track names any more into the bin, where it can still be
restored by hand (:mod:`clipforge.media.trash`).

## What keeps it safe

- A file younger than an hour is left alone: SING copies a take into place
  before it writes the record, and ARTWORK paints before it names the picture.
- No tracks at all means nothing is binned. A library emptied on purpose keeps
  its last files until one track exists again; a query pointed at the wrong
  project, or one that came back empty for any other reason, never empties
  the folder.
- Only the two folders tracks live in, and only audio and pictures.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from clipforge_contracts import Track

from clipforge.media.trash import Trash, TrashError
from clipforge.observability import get_logger
from clipforge.tracks import AUDIO_SUFFIXES

log = get_logger(__name__)

__all__ = ["TrackTidyReport", "referenced_paths", "tidy_deleted_tracks"]

# Older than this, a file with no track is a deleted track's.
MIN_AGE_S = 60 * 60
PICTURE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})


@dataclass
class TrackTidyReport:
    binned: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def _key(path: Path | str) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def referenced_paths(tracks: Iterable[Track]) -> set[str]:
    """Every file a track names: its audio, and both shapes of its picture."""
    keys: set[str] = set()
    for track in tracks:
        keys.add(_key(track.local_path))
        if track.artwork is not None:
            keys.add(_key(track.artwork.wide))
            if track.artwork.tall:
                keys.add(_key(track.artwork.tall))
    return keys


def tidy_deleted_tracks(
    music_dir: Path,
    tracks: list[Track],
    trash: Trash,
    *,
    now: float | None = None,
    min_age_s: float = MIN_AGE_S,
) -> TrackTidyReport:
    """Move the files no track names into the bin. Never raises for one file."""
    report = TrackTidyReport()
    if not tracks:
        return report
    named = referenced_paths(tracks)
    clock = time.time() if now is None else now
    folders = ((music_dir / "generated", AUDIO_SUFFIXES), (music_dir / "artwork", PICTURE_SUFFIXES))
    for folder, suffixes in folders:
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            if _key(path) in named or clock - path.stat().st_mtime < min_age_s:
                continue
            kind = "track" if folder.name == "generated" else "artwork"
            try:
                trash.put(path, kind=kind, record_id=path.stem)
                report.binned.append(path.name)
            except TrashError as exc:
                report.failures.append(f"{path.name}: {exc}")
    if report.binned or report.failures:
        log.info("tidy.tracks", binned=len(report.binned), failures=len(report.failures))
    return report
