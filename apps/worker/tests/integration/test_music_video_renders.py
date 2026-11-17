"""The visualizer through real ffmpeg: a tone becomes a Short of the right shape."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from clipforge.media.visualizer import SHORT, VisualSpec, build_ass, palette_for, render

pytestmark = pytest.mark.integration


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_a_short_renders_at_nine_sixteen(tmp_path: Path) -> None:
    audio = tmp_path / "tone.wav"
    subprocess.run(  # noqa: S603 - a fixed argv, our own temp paths
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=6",
            str(audio),
        ],
        check=True,
    )
    titles = tmp_path / "t.ass"
    titles.write_text(build_ass("Test", "a tone", "footer", SHORT), encoding="utf-8")
    out = render(
        VisualSpec(
            audio=audio,
            output=tmp_path / "o.mp4",
            titles=titles,
            shape=SHORT,
            palette=palette_for("Lo-fi"),
            start_sec=0,
            duration_sec=4,
        ),
        encoder="libx264",
    )
    probe = subprocess.run(  # noqa: S603 - a fixed argv, our own temp paths
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=width,height,codec_type",
            "-of",
            "json",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    streams = json.loads(probe.stdout)["streams"]
    video = next(s for s in streams if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1080, 1920)
    assert any(s["codec_type"] == "audio" for s in streams)
