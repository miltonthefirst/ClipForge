"""DRAW and ASSEMBLE with real ffmpeg: two drawn scenes and a voice become one clip.

In the integration tier because ffmpeg is what it needs, and CI has ffmpeg.
The narration is a generated tone rather than a real voice, so the test
proves the plumbing — frames piped in, a card in front, the voice delayed past
it, captions burned from word timings — without needing Kokoro's weights.
"""

# The fakes below stand in for the stores by shape, not by type. Said once here
# rather than on every argument, because the formatter moves a trailing comment
# off the line it was written for.
# mypy: disable-error-code="arg-type"

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from clipforge.config import Settings
from clipforge.media.ffprobe import probe
from clipforge.models.broker import ModelBroker
from clipforge.stages.base import StageContext
from clipforge.stages.compose import ComposeAssembleStage, DrawStage
from clipforge.store.blobs import BlobRef
from clipforge_contracts import (
    Clip,
    ClipPreview,
    ComposeOptions,
    Job,
    JobStatus,
    JobType,
    Lane,
    Stage,
    StageName,
    StageStatus,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg on PATH"),
]

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
SOFTWARE = "libx264"


class FakeClipStore:
    def __init__(self) -> None:
        self.saved: list[tuple[Clip, ClipPreview | None]] = []

    def save(self, clip: Clip, *, preview: ClipPreview | None = None) -> None:
        self.saved.append((clip, preview))


class FakeBlobs:
    """Keeps the file, as the real store does: the stage clears its scratch afterwards."""

    def __init__(self, root: Path) -> None:
        self._root = root / "blobs"

    def put(self, key: str, path: Path, *, content_type: str) -> BlobRef:
        kept = self._root / Path(key).name
        kept.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, kept)
        return BlobRef(key=key, local_path=kept, size_bytes=kept.stat().st_size)


class FakeWorkspace:
    def __init__(self, root: Path) -> None:
        self.tmp_dir = root / "tmp"


def tone(path: Path, *, seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603 - ffmpeg is a hard dependency, resolved via PATH
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=24000:duration={seconds}",
            str(path),
        ],
        check=True,
    )


SCRIPT_CP: dict[str, Any] = {
    "title": "Two drawn scenes",
    "script": "One figure waves at the crowd. Two figures argue over a chart.",
    "scenes": [
        {
            "index": 0,
            "line": "One figure waves at the crowd.",
            "actors": [{"name": "a fan", "pose": "WAVE", "mood": "HAPPY"}],
            "props": ["SUN"],
            "mood": "UPBEAT",
            "label": None,
        },
        {
            "index": 1,
            "line": "Two figures argue over a chart.",
            "actors": [
                {"name": "the boss", "pose": "POINT", "mood": "ANGRY"},
                {"name": "an analyst", "pose": "SHRUG", "mood": "WORRIED"},
            ],
            "props": ["CHART_DOWN", "SCREEN"],
            "mood": "TENSE",
            "label": "-12%",
        },
    ],
    "scriptBy": "OPERATOR",
    "modelVersion": None,
    "promptVersion": None,
}
ALIGN_CP: dict[str, Any] = {
    "words": [
        {"text": t, "startSec": s, "endSec": s + 0.25}
        for t, s in [
            ("One", 0.1),
            ("figure", 0.4),
            ("waves", 0.7),
            ("at", 1.0),
            ("the", 1.2),
            ("crowd", 1.4),
            ("Two", 2.2),
            ("figures", 2.5),
            ("argue", 2.9),
            ("over", 3.2),
            ("a", 3.4),
            ("chart", 3.6),
        ]
    ],
    "count": 12,
}


def job(checkpoints: dict[StageName, dict[str, Any]]) -> Job:
    names = [
        StageName.SCRIPT,
        StageName.NARRATE,
        StageName.ALIGN,
        StageName.DRAW,
        StageName.ASSEMBLE,
    ]
    lanes = [Lane.GPU, Lane.CPU, Lane.GPU, Lane.CPU, Lane.CPU]
    return Job(
        id="job-compose",
        uid="user-1",
        type=JobType.COMPOSE,
        status=JobStatus.RUNNING,
        compose_options=ComposeOptions(topic="Two drawn scenes", seed=3, trend_id="t1"),
        stages=[
            Stage(
                name=n,
                lane=lane,
                status=StageStatus.DONE if n in checkpoints else StageStatus.PENDING,
                checkpoint=checkpoints.get(n),
            )
            for n, lane in zip(names, lanes, strict=True)
        ],
        attempts=0,
        max_attempts=2,
        created_at=NOW,
        updated_at=NOW,
    )


def context(the_job: Job, *, stage: StageName) -> StageContext:
    settings = Settings(
        _env_file=None,
        video_encoder=SOFTWARE,
        compose_fps=12,
        compose_supersample=1,
    )
    return StageContext(
        job=the_job,
        stage_name=stage,
        checkpoint=None,
        settings=settings,
        broker=ModelBroker(reserve_mb=settings.vram_reserve_mb),
        should_stop=Event(),
    )


def test_two_drawn_scenes_and_a_voice_become_one_captioned_clip(tmp_path: Path) -> None:
    workspace = FakeWorkspace(tmp_path)
    narration = workspace.tmp_dir / "compose" / "job-compose" / "narration.wav"
    tone(narration, seconds=4.0)
    narrate_cp = {
        "path": str(narration),
        "durationSec": 4.0,
        "voice": "af_heart",
        "engine": "fake",
        "language": "en-us",
    }

    drawn = DrawStage(workspace=workspace).run(
        context(
            job(
                {
                    StageName.SCRIPT: SCRIPT_CP,
                    StageName.NARRATE: narrate_cp,
                    StageName.ALIGN: ALIGN_CP,
                }
            ),
            stage=StageName.DRAW,
        )
    )
    assert drawn.checkpoint is not None
    scenes = drawn.checkpoint["scenes"]
    assert sorted(scenes) == ["0", "1"]
    for entry in scenes.values():
        assert Path(entry["path"]).is_file()
    assert scenes["0"]["durationSec"] == pytest.approx(2.2, abs=0.1)
    assert scenes["1"]["durationSec"] == pytest.approx(1.8, abs=0.1)

    clips = FakeClipStore()
    stage = ComposeAssembleStage(clips=clips, workspace=workspace, blobs=FakeBlobs(tmp_path))
    the_job = job(
        {
            StageName.SCRIPT: SCRIPT_CP,
            StageName.NARRATE: narrate_cp,
            StageName.ALIGN: ALIGN_CP,
            StageName.DRAW: drawn.checkpoint,
        }
    )
    outcome = stage.run(context(the_job, stage=StageName.ASSEMBLE))

    assert outcome.checkpoint is not None
    assert outcome.checkpoint["scenes"] == 2
    assert outcome.checkpoint["captions"] is True
    assert len(clips.saved) == 1
    clip, preview = clips.saved[0]
    assert clip.compose is not None
    assert clip.compose.script == SCRIPT_CP["script"]
    assert [s.line for s in clip.compose.scenes] == [
        SCRIPT_CP["scenes"][0]["line"],
        SCRIPT_CP["scenes"][1]["line"],
    ]
    # The card comes first, so every scene is two seconds later than its voice.
    assert clip.compose.scenes[0].start_sec == pytest.approx(2.0, abs=0.01)
    assert clip.compose.trend_id == "t1"
    assert clip.compose.seed == 3
    assert clip.source_id is None
    assert clip.candidate_id == "compose-job-compose"
    assert preview is not None and preview.width_px > 0

    media = probe(Path(clip.local_path))
    assert media.width == 1080 and media.height == 1920
    assert media.duration_sec == pytest.approx(6.0, abs=0.3)
    assert media.has_audio
    # Everything between stages is cleared once the clip exists.
    assert not (workspace.tmp_dir / "compose" / "job-compose").exists()


def test_the_same_scene_draws_the_same_bytes(tmp_path: Path) -> None:
    from clipforge.media.cartoon import render_scene
    from clipforge.media.profiles import load_profile
    from clipforge.synth.scenes import TimedScene
    from clipforge_contracts import SceneMood, StickActor, StickMood, StickPose

    scene = TimedScene(
        index=1,
        line="x",
        actors=(StickActor(name="a fan", pose=StickPose.WALK, mood=StickMood.NEUTRAL),),
        props=(),
        mood=SceneMood.CALM,
        start_sec=0.0,
        end_sec=1.0,
    )
    profile = load_profile("default")
    first = render_scene(
        scene, tmp_path / "a.mp4", profile=profile, seed=9, fps=12, supersample=1, encoder=SOFTWARE
    )
    second = render_scene(
        scene, tmp_path / "b.mp4", profile=profile, seed=9, fps=12, supersample=1, encoder=SOFTWARE
    )
    assert (tmp_path / "a.mp4").read_bytes() == (tmp_path / "b.mp4").read_bytes()
    assert first.duration_sec == second.duration_sec == 1.0
