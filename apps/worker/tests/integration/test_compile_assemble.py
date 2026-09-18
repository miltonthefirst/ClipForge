"""ASSEMBLE, with real ffmpeg: two sources become one clip with a card in front.

In the integration tier because ffmpeg is what it needs, and CI has ffmpeg. The
sources are generated rather than committed — `testsrc2` reproduces them on any
machine — and deliberately differ in frame rate and sample rate, because that
mismatch is exactly what the concat filter exists to absorb.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from clipforge.config import Settings
from clipforge.media.assemble import concat_segments, render_title_card
from clipforge.media.ffprobe import probe
from clipforge.media.profiles import load_profile
from clipforge.models.broker import ModelBroker
from clipforge.stages.base import StageContext
from clipforge.stages.compile import AssembleStage, CompileError
from clipforge.store.blobs import BlobRef
from clipforge_contracts import (
    Clip,
    ClipPreview,
    CompileItem,
    CompileOptions,
    CompileTransition,
    Job,
    JobStatus,
    JobType,
    Lane,
    Source,
    SourceProvider,
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


def make_source(path: Path, *, seconds: int, fps: int, rate: int) -> Path:
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
            f"testsrc2=size=1280x720:rate={fps}:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate={rate}:duration={seconds}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


@pytest.fixture(scope="module")
def sources(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("compile-src")
    return {
        "src-a": make_source(root / "a.mp4", seconds=30, fps=25, rate=44100),
        "src-b": make_source(root / "b.mp4", seconds=30, fps=30, rate=48000),
    }


# ── Pieces ───────────────────────────────────────────────────────────────────


def test_the_title_card_is_two_seconds_of_vertical_video_with_silence(tmp_path: Path) -> None:
    card = render_title_card(
        "Best goals of the week",
        tmp_path / "card.mp4",
        profile=load_profile("default"),
        work_dir=tmp_path,
        encoder=SOFTWARE,
    )
    info = probe(card.path)
    assert info.width == 1080 and info.height == 1920
    assert info.has_audio
    assert info.duration_sec == pytest.approx(2.0, abs=0.15)


def test_segments_with_different_rates_join_into_one_file(
    tmp_path: Path, sources: dict[str, Path]
) -> None:
    profile = load_profile("default")
    from clipforge.media.render import RenderRequest, render_clip

    parts: list[tuple[Path, float]] = []
    for name, (start, end) in {"src-a": (2.0, 7.0), "src-b": (10.0, 16.0)}.items():
        media = probe(sources[name])
        result = render_clip(
            RenderRequest(
                source=sources[name],
                destination=tmp_path / f"{name}.mp4",
                start_sec=start,
                end_sec=end,
                profile=profile,
                encoder=SOFTWARE,
            ),
            media,
        )
        parts.append((result.path, result.duration_sec))

    joined = concat_segments(
        [p for p, _ in parts],
        tmp_path / "joined.mp4",
        durations=[d for _, d in parts],
        transition=CompileTransition.FADE,
        profile=profile,
        encoder=SOFTWARE,
    )
    info = probe(joined.path)
    assert info.width == 1080 and info.height == 1920
    assert info.duration_sec == pytest.approx(11.0, abs=0.3)
    assert joined.duration_sec == pytest.approx(11.0)


# ── The stage ────────────────────────────────────────────────────────────────


class FakeSourceStore:
    def __init__(self, paths: dict[str, Path]) -> None:
        self._paths = paths
        self.touched: list[str] = []

    def get(self, source_id: str) -> Source | None:
        path = self._paths.get(source_id)
        if path is None:
            return None
        return Source(
            id=source_id,
            uid="user-1",
            provider=SourceProvider.LOCAL,
            title=f"Source {source_id}",
            channel="Test",
            url=None,
            local_path=str(path),
            created_at=NOW,
        )

    def touch(self, source_id: str) -> None:
        self.touched.append(source_id)


class FakeClipStore:
    def __init__(self) -> None:
        self.saved: list[tuple[Clip, ClipPreview | None]] = []

    def save(self, clip: Clip, *, preview: ClipPreview | None = None) -> None:
        self.saved.append((clip, preview))


class FakeBlobStore:
    def __init__(self, keep_in: Path) -> None:
        self._keep_in = keep_in
        self.kept: list[Path] = []

    def put(
        self, key: str, source: Path, *, content_type: str | None = None, required: bool = False
    ) -> BlobRef:
        self._keep_in.mkdir(parents=True, exist_ok=True)
        copy = self._keep_in / source.name
        shutil.copy2(source, copy)
        self.kept.append(copy)
        return BlobRef(key=key, local_path=copy, size_bytes=copy.stat().st_size)


class FakeArchive:
    def load(self, content_hash: str, model_version: str) -> None:
        return None


class FakeWorkspace:
    def __init__(self, root: Path) -> None:
        self.tmp_dir = root / "tmp"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)


def job(options: CompileOptions, segments: dict[str, dict[str, Any]]) -> Job:
    return Job(
        id="job-compile",
        uid="user-1",
        type=JobType.COMPILE,
        status=JobStatus.RUNNING,
        compile_options=options,
        stages=[
            Stage(
                name=StageName.GATHER,
                lane=Lane.CPU,
                status=StageStatus.DONE,
                checkpoint={"items": []},
            ),
            Stage(
                name=StageName.SELECT,
                lane=Lane.GPU,
                status=StageStatus.DONE,
                checkpoint={"segments": segments},
            ),
            Stage(name=StageName.ASSEMBLE, lane=Lane.CPU, status=StageStatus.PENDING),
        ],
        attempts=0,
        max_attempts=2,
        created_at=NOW,
        updated_at=NOW,
    )


def context(the_job: Job) -> StageContext:
    settings = Settings(_env_file=None, video_encoder=SOFTWARE)
    return StageContext(
        job=the_job,
        stage_name=StageName.ASSEMBLE,
        checkpoint=None,
        settings=settings,
        broker=ModelBroker(reserve_mb=settings.vram_reserve_mb),
        should_stop=Event(),
    )


def segment(key: str, source_id: str, start: float, end: float, **over: Any) -> dict[str, Any]:
    return {
        "submission": f"file:///{source_id}.mp4",
        "sourceId": source_id,
        "startSec": start,
        "endSec": end,
        "chosenBy": "model",
        "candidateId": f"cand-{key}",
        **over,
    }


def test_assemble_writes_one_clip_that_records_every_segment(
    tmp_path: Path, sources: dict[str, Path]
) -> None:
    clips = FakeClipStore()
    blobs = FakeBlobStore(tmp_path / "bucket")
    store = FakeSourceStore(sources)
    stage = AssembleStage(
        sources=store,
        clips=clips,
        archive=FakeArchive(),
        workspace=FakeWorkspace(tmp_path),
        blobs=blobs,
    )  # type: ignore[arg-type]
    options = CompileOptions(
        theme="Best goals of the week",
        items=[
            CompileItem(submission="file:///src-a.mp4"),
            CompileItem(submission="file:///src-b.mp4"),
        ],
        title_card=True,
    )

    outcome = stage.run(
        context(
            job(
                options,
                {
                    "0": segment("0", "src-a", 2.0, 7.0),
                    "1": segment("1", "src-b", 10.0, 16.0, trimmed=True),
                },
            )
        )
    )

    assert len(clips.saved) == 1
    clip, preview = clips.saved[0]
    assert clip.compile is not None
    assert clip.compile.theme == "Best goals of the week"
    assert [s.source_id for s in clip.compile.segments] == ["src-a", "src-b"]
    assert clip.compile.segments[0].title == "Source src-a"
    assert clip.compile.segments[1].duration_sec == pytest.approx(6.0)
    assert clip.compile.title_card is True
    assert clip.compile.transition is CompileTransition.FADE
    assert any("trimmed" in w.root for w in (clip.compile.warnings or []))
    assert clip.candidate_id == "cand-0"
    assert clip.source_id is None
    assert clip.lineage_id == clip.id
    assert clip.title == "Best goals of the week"
    assert "Compiled from 2 videos" in (clip.description or "")
    assert clip.duration_sec == pytest.approx(13.0, abs=0.1)
    assert preview is not None and preview.width_px > 0
    assert Path(clip.local_path).is_file()
    assert store.touched == ["src-a", "src-b"]
    assert outcome.checkpoint is not None
    assert outcome.checkpoint["clipId"] == clip.id
    assert outcome.checkpoint["segments"] == 2
    # The scratch directory is gone with the job.
    assert not any((tmp_path / "tmp").iterdir())


def test_a_segment_whose_source_is_gone_is_skipped_and_written_down(
    tmp_path: Path, sources: dict[str, Path]
) -> None:
    clips = FakeClipStore()
    stage = AssembleStage(
        sources=FakeSourceStore(sources),
        clips=clips,
        archive=FakeArchive(),
        workspace=FakeWorkspace(tmp_path),
        blobs=FakeBlobStore(tmp_path / "bucket"),
    )  # type: ignore[arg-type]
    options = CompileOptions(
        theme="x",
        items=[CompileItem(submission="a"), CompileItem(submission="b")],
        title_card=False,
        transition=CompileTransition.CUT,
    )

    stage.run(
        context(
            job(
                options,
                {"0": segment("0", "src-a", 2.0, 6.0), "1": segment("1", "src-gone", 1.0, 5.0)},
            )
        )
    )

    clip, _ = clips.saved[0]
    assert clip.compile is not None
    assert [s.source_id for s in clip.compile.segments] == ["src-a"]
    assert clip.compile.skipped is not None
    assert clip.compile.skipped[0].submission == "file:///src-gone.mp4"
    assert "no longer recorded" in clip.compile.skipped[0].reason
    assert any("only one segment" in w.root for w in (clip.compile.warnings or []))
    assert clip.compile.title_card is False
    assert clip.duration_sec == pytest.approx(4.0, abs=0.1)


def test_assemble_fails_when_no_segment_can_be_rendered(tmp_path: Path) -> None:
    stage = AssembleStage(
        sources=FakeSourceStore({}),
        clips=FakeClipStore(),
        archive=FakeArchive(),
        workspace=FakeWorkspace(tmp_path),
        blobs=FakeBlobStore(tmp_path / "bucket"),
    )  # type: ignore[arg-type]
    options = CompileOptions(
        theme="x",
        items=[CompileItem(submission="a"), CompileItem(submission="b")],
        title_card=False,
    )
    with pytest.raises(CompileError) as caught:
        stage.run(
            context(
                job(
                    options,
                    {"0": segment("0", "src-a", 2.0, 6.0), "1": segment("1", "src-b", 1.0, 5.0)},
                )
            )
        )
    assert caught.value.code == "COMPILE_RENDER"
