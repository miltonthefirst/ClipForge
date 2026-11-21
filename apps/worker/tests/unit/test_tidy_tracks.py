"""The files of tracks deleted on the Music page go to the bin (docs/MUSIC-PLAN.md, M7)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from clipforge.media.trash import Trash
from clipforge.scheduler.tidy_tracks import tidy_deleted_tracks
from clipforge_contracts import Track

pytestmark = pytest.mark.unit

HOUR = 60 * 60


def track(path: Path, artwork: Path | None = None) -> Track:
    values: dict[str, Any] = {
        "id": path.stem,
        "title": "Sunday Light",
        "prompt": "amapiano",
        "seconds": 120,
        "instrumental": False,
        "model": "ACE-Step 1.5",
        "licence": "MIT",
        "localPath": str(path),
        "status": "KEPT",
        "createdAt": "2026-11-21T00:00:00Z",
    }
    if artwork:
        values["artwork"] = {
            "wide": str(artwork),
            "tall": str(artwork.with_name(artwork.name.replace("wide", "tall"))),
            "prompt": "a street",
            "model": "FLUX.2 klein",
            "createdAt": "2026-11-21T00:00:00Z",
        }
    return Track.model_validate(values)


def make(path: Path, age_s: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    then = path.stat().st_mtime - age_s
    os.utime(path, (then, then))
    return path


def test_only_files_no_track_names_go_to_the_bin(tmp_path: Path) -> None:
    music = tmp_path / "music"
    kept = make(music / "generated" / "trk_kept.wav", 2 * HOUR)
    gone = make(music / "generated" / "trk_gone.wav", 2 * HOUR)
    fresh = make(music / "generated" / "trk_singing.wav", 60)
    note = make(music / "generated" / "readme.txt", 2 * HOUR)
    wide = make(music / "artwork" / "job1-wide.png", 2 * HOUR)
    tall = make(music / "artwork" / "job1-tall.png", 2 * HOUR)
    orphan_art = make(music / "artwork" / "job2-wide.png", 2 * HOUR)
    trash = Trash(tmp_path / "trash")

    report = tidy_deleted_tracks(music, [track(kept, wide)], trash)

    assert sorted(report.binned) == ["job2-wide.png", "trk_gone.wav"]
    assert not gone.exists() and not orphan_art.exists()
    assert all(p.exists() for p in (kept, fresh, note, wide, tall))
    # In the bin, not destroyed: it can still be restored.
    restored = trash.restore(next(i.id for i in trash.items() if i.name == "trk_gone.wav"))
    assert restored == gone.resolve() and restored.exists()


def test_no_tracks_at_all_bins_nothing(tmp_path: Path) -> None:
    music = tmp_path / "music"
    lonely = make(music / "generated" / "trk_one.wav", 2 * HOUR)
    report = tidy_deleted_tracks(music, [], Trash(tmp_path / "trash"))
    assert report.binned == [] and lonely.exists()
