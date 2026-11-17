"""A kept track drawn into a video and a Short (docs/MUSIC-PLAN.md, M4)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from clipforge.config import Settings
from clipforge.media.visualizer import (
    FULL,
    SHORT,
    VisualSpec,
    build_ass,
    build_command,
    palette_for,
    strongest_window,
)
from clipforge.stages.music_video import MusicVideoError, VisualizeStage, describe, headline
from clipforge_contracts import Track, TrackStatus

pytestmark = pytest.mark.unit


def track(**overrides: Any) -> Track:
    values: dict[str, Any] = {
        "id": "trk_1",
        "title": "Cinematic (180 s)",
        "prompt": "uplifting cinematic build, strings and piano, steady pulse, hopeful, instrumental",
        "seconds": 180,
        "instrumental": True,
        "model": "ACE-Step 1.5",
        "licence": "MIT",
        "local_path": "P:/nowhere/trk_1.wav",
        "status": TrackStatus.KEPT,
        "created_at": datetime(2026, 11, 17, tzinfo=UTC),
    }
    values.update(overrides)
    return Track(**values)


def test_the_short_is_the_loudest_stretch() -> None:
    rate = 100
    quiet, loud = np.full(rate * 30, 0.01), np.full(rate * 20, 0.8)
    samples = np.concatenate([quiet, loud, quiet]).astype(np.float32)

    assert strongest_window(samples, rate, 20) == 30.0


def test_a_track_shorter_than_the_short_starts_at_the_top() -> None:
    assert strongest_window(np.ones(100 * 10, dtype=np.float32), 100, 55) == 0.0


def test_the_titles_never_break_the_subtitle_format() -> None:
    ass = build_ass("Cinematic", "strings {and} piano", "footer", FULL)
    assert "PlayResX: 1920" in ass and "PlayResY: 1080" in ass
    assert "{and}" not in ass and "(and)" in ass


def test_the_command_fades_normalises_and_uses_the_series_colours(tmp_path: Path) -> None:
    spec = VisualSpec(
        audio=tmp_path / "a.wav",
        output=tmp_path / "o.mp4",
        titles=tmp_path / "t.ass",
        shape=SHORT,
        palette=palette_for("Cinematic"),
        start_sec=30,
        duration_sec=55,
    )
    command = " ".join(build_command(spec, encoder="h264_nvenc"))
    assert "s=1080x1920" in command
    assert "-ss 30.000" in command and "-t 55.000" in command
    assert "afade=t=out:st=53.500" in command
    assert "loudnorm=I=-14.0" in command
    assert palette_for("Cinematic").colors[0] in command


def test_an_unknown_series_still_has_colours() -> None:
    assert palette_for("Polka").colors == palette_for(None).colors


def test_titles_read_like_a_channel_and_a_short_says_so() -> None:
    assert headline(track()) == "Uplifting cinematic build · strings and piano"
    full_title, full_text, tags = describe(track(), FULL)
    short_title, short_text, _ = describe(track(), SHORT)
    assert full_title == "Uplifting cinematic build · strings and piano | Cinematic music"
    assert short_title.endswith("#Shorts") and "#Shorts" in short_text
    assert "AI-generated" in full_text
    assert "cinematic" in tags and all(len(t) <= 40 for t in tags)


class Tracks:
    def __init__(self, found: Track | None) -> None:
        self.found = found

    def get(self, _id: str) -> Track | None:
        return self.found


def context_for(options: dict[str, Any]) -> Any:
    from clipforge_contracts import Job

    class Context:
        settings = Settings(use_emulators=True)
        job = Job.model_validate(
            {
                "id": "j1",
                "uid": "alice",
                "type": "MUSIC_VIDEO",
                "status": "RUNNING",
                "musicVideoOptions": options,
                "stages": [{"name": "VISUALIZE", "lane": "CPU", "status": "RUNNING"}],
                "attempts": 0,
                "maxAttempts": 2,
                "createdAt": "2026-11-17T00:00:00Z",
                "updatedAt": "2026-11-17T00:00:00Z",
            }
        )

        def progress(self, _note: str) -> None:
            return None

    return Context()


@pytest.mark.parametrize(
    ("found", "says"),
    [
        (None, "does not exist"),
        (track(status=TrackStatus.NEW), "kept"),
        (track(status=TrackStatus.DISCARDED), "kept"),
        (track(), "not on this PC"),
    ],
)
def test_only_a_kept_track_on_this_pc_becomes_a_video(found: Track | None, says: str) -> None:
    stage = VisualizeStage(
        clips=None,  # type: ignore[arg-type]
        tracks=Tracks(found),  # type: ignore[arg-type]
        workspace=None,  # type: ignore[arg-type]
        blobs=None,  # type: ignore[arg-type]
    )
    with pytest.raises(MusicVideoError, match=says):
        stage.run(context_for({"trackId": "trk_1"}))
