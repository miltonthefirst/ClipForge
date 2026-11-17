"""A song's background picture, and a song as long as its words (the owner, 2026-11-18)."""

from __future__ import annotations

import io
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from clipforge.config import Settings
from clipforge.media.comfy import PaintError, graph_for, sized
from clipforge.media.visualizer import FULL, SHORT, VisualSpec, build_command, palette_for
from clipforge.stages import artwork as artwork_module
from clipforge.stages.artwork import TALL, WIDE, ArtworkStage, art_prompt
from clipforge.stages.music_video import background_for, describe, headline
from clipforge.stages.pipeline import (
    GENERATE_MUSIC_PIPELINE,
    MUSIC_VIDEO_PIPELINE,
    new_generate_music_job,
    new_music_video_job,
)
from clipforge.synth.lyrics import song_seconds, target_seconds
from clipforge_contracts import (
    GenerateMusicOptions,
    MusicVideoOptions,
    StageName,
    Track,
    TrackArtwork,
    TrackStatus,
)

pytestmark = pytest.mark.unit

SONG = """[Intro]

[Verse 1]
one
two
three
four

[Chorus]
a
b
c
d

[Instrumental Break]

[Outro]"""


# ── Length from the words ────────────────────────────────────────────────────


def test_a_song_is_as_long_as_its_words_take_to_sing() -> None:
    # 8 sung lines x 2 bars + intro 4 + break 8 + outro 4 + 2 of air = 34 bars;
    # a bar at 120 bpm is 2 s: 68 s, rounded up to 70.
    assert song_seconds(SONG, 120, longest=480) == 70
    # Slower is longer.
    assert song_seconds(SONG, 60, longest=480) == 140


def test_never_longer_than_allowed_nor_shorter_than_half_a_minute() -> None:
    assert song_seconds(SONG, 60, longest=90) == 90
    assert song_seconds("[Verse]\nla", 180, longest=480) == 30


def test_the_writer_aims_for_the_exact_length_or_else_the_longest() -> None:
    def opts(**extra: Any) -> GenerateMusicOptions:
        return GenerateMusicOptions.model_validate(
            {"lyricsFrom": "IDEA", "style": "pop", "idea": "x", **extra}
        )

    assert target_seconds(opts()) == 240
    assert target_seconds(opts(maxSeconds=480)) == 480
    assert target_seconds(opts(seconds=90, maxSeconds=480)) == 90
    with pytest.raises(ValueError):
        opts(maxSeconds=600)  # eight minutes is what was measured on this card


# ── The picture ──────────────────────────────────────────────────────────────


def test_the_pipelines_paint_after_singing_and_before_drawing() -> None:
    assert [name for name, _ in GENERATE_MUSIC_PIPELINE][-1] is StageName.ARTWORK
    assert [name for name, _ in MUSIC_VIDEO_PIPELINE] == [StageName.ARTWORK, StageName.VISUALIZE]


def test_the_image_model_is_asked_for_the_scene_in_its_style_at_its_own_sizes() -> None:
    prompt = art_prompt("An empty rooftop at dusk, string lights.", "anime background art")
    assert prompt.startswith("An empty rooftop at dusk, string lights. Anime background art,")
    graph = graph_for(prompt, 1500, 850, seed=7)
    assert graph["4"]["inputs"]["text"] == prompt
    assert (graph["5"]["inputs"]["width"], graph["5"]["inputs"]["height"]) == sized(1500, 850)
    assert sized(1500, 850) == (1504, 864)
    assert graph["7"]["inputs"]["noise_seed"] == 7


def _png(width: int, height: int) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (40, 80, 120)).save(buffer, format="PNG")
    return buffer.getvalue()


def track(**overrides: Any) -> Track:
    values: dict[str, Any] = {
        "id": "trk_1",
        "title": "Candle Light (take 2)",
        "prompt": "amapiano, log drum, female vocal",
        "seconds": 150,
        "instrumental": False,
        "lyrics": "[Verse]\nWe lost the lights\n[Chorus]\nCandle light",
        "model": "ACE-Step",
        "licence": "MIT",
        "local_path": "P:/x.wav",
        "status": TrackStatus.NEW,
        "created_at": datetime(2026, 11, 18, tzinfo=UTC),
    }
    values.update(overrides)
    return Track(**values)


class Tracks:
    def __init__(self, *found: Track) -> None:
        self.found = {t.id: t for t in found}
        self.painted: dict[str, TrackArtwork] = {}

    def get(self, track_id: str) -> Track | None:
        return self.found.get(track_id)

    def set_artwork(self, track_id: str, artwork: TrackArtwork) -> None:
        self.painted[track_id] = artwork


class Workspace:
    def __init__(self, root: Path) -> None:
        self.music_dir = root / "music"


class Lease:
    def observe(self) -> int:
        return 0


class Broker:
    def __init__(self) -> None:
        self.models: list[str] = []

    @contextmanager
    def acquire(self, model: str, _mb: int) -> Any:
        self.models.append(model)
        yield Lease()


def context_for(job: Any) -> Any:
    class Context:
        settings = Settings(use_emulators=True, comfyui_url="")

        def __init__(self) -> None:
            self.job = job
            self.broker = Broker()
            self.notes: list[str] = []

        def progress(self, note: str) -> None:
            self.notes.append(note)

        def stopping(self) -> bool:
            return False

    return Context()


def _song_job() -> Any:
    job = new_generate_music_job(
        uid="alice",
        options=GenerateMusicOptions.model_validate(
            {"lyricsFrom": "IDEA", "style": "amapiano", "idea": "x"}
        ),
        job_id="job1",
    )
    job.stages[0].checkpoint = {"scene": "an empty street at dusk", "artStyle": "digital painting"}
    job.stages[1].checkpoint = {"trackIds": ["trk_1", "trk_2"]}
    return job


def test_every_take_gets_the_songs_two_pictures(tmp_path: Path) -> None:
    painted: list[tuple[int, int, str]] = []

    def painter(_url: str, prompt: str, *, width: int, height: int, **_: Any) -> bytes:
        painted.append((width, height, prompt))
        return _png(width, height)

    tracks = Tracks(track(), track(id="trk_2", title="Candle Light"))
    stage = ArtworkStage(tracks=tracks, workspace=Workspace(tmp_path), painter=painter)  # type: ignore[arg-type]
    context = context_for(_song_job())
    checkpoint = stage.run(context).checkpoint

    assert checkpoint is not None and checkpoint["tracks"] == ["trk_1", "trk_2"]
    assert [(w, h) for w, h, _ in painted] == [WIDE, TALL]
    assert painted[0][2].startswith("an empty street at dusk. Digital painting,")
    art = tracks.painted["trk_1"]
    assert art == tracks.painted["trk_2"]
    assert Path(art.wide).is_file() and Path(art.tall).is_file()
    assert art.preview_base64 and len(art.preview_base64) < 40000
    assert context.broker.models == ["comfyui:flux2-klein-4b"]


def test_no_image_model_is_no_picture_not_a_failed_song(tmp_path: Path) -> None:
    def painter(*_a: Any, **_k: Any) -> bytes:
        raise PaintError("ComfyUI is not answering")

    tracks = Tracks(track(), track(id="trk_2"))
    stage = ArtworkStage(tracks=tracks, workspace=Workspace(tmp_path), painter=painter)  # type: ignore[arg-type]
    checkpoint = stage.run(context_for(_song_job())).checkpoint

    assert checkpoint is not None and checkpoint["artwork"] is None
    assert tracks.painted == {}


def test_a_track_that_has_its_picture_is_not_painted_again(tmp_path: Path) -> None:
    wide, tall = tmp_path / "w.png", tmp_path / "t.png"
    wide.write_bytes(_png(8, 8))
    tall.write_bytes(_png(8, 8))
    art = TrackArtwork(
        wide=str(wide), tall=str(tall), prompt="p", model="m", created_at=datetime.now(UTC)
    )
    job = new_music_video_job(uid="alice", options=MusicVideoOptions(track_id="trk_1"))
    stage = ArtworkStage(
        tracks=Tracks(track(artwork=art)),  # type: ignore[arg-type]
        workspace=Workspace(tmp_path),  # type: ignore[arg-type]
        painter=lambda *_a, **_k: pytest.fail("painted again"),
    )
    assert stage.run(context_for(job)).skipped


def test_an_older_track_gets_a_scene_written_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clipforge.stages.artwork import SceneDraft

    class Client:
        model = "qwen3.5:4b"

        def is_available(self) -> bool:
            return True

        def generate_structured(self, **kwargs: Any) -> SceneDraft:
            assert "Candle Light" in kwargs["prompt"]
            return SceneDraft(scene="a candlelit window at night", art_style="watercolour painting")

    prompts: list[str] = []

    def painter(_url: str, prompt: str, *, width: int, height: int, **_: Any) -> bytes:
        prompts.append(prompt)
        return _png(width, height)

    job = new_music_video_job(uid="alice", options=MusicVideoOptions(track_id="trk_1"))
    stage = ArtworkStage(
        tracks=Tracks(track()),  # type: ignore[arg-type]
        workspace=Workspace(tmp_path),  # type: ignore[arg-type]
        client_factory=lambda _c: Client(),  # type: ignore[arg-type, return-value]
        painter=painter,
    )
    context = context_for(job)
    stage.run(context)
    assert prompts[0].startswith("a candlelit window at night. Watercolour painting")
    assert context.broker.models == ["ollama:qwen3.5:4b", "comfyui:flux2-klein-4b"]
    assert artwork_module.WIDE == WIDE


# ── The video ────────────────────────────────────────────────────────────────


def test_the_video_drifts_over_the_picture_instead_of_the_gradient(tmp_path: Path) -> None:
    picture = tmp_path / "wide.png"
    picture.write_bytes(_png(16, 9))
    spec = VisualSpec(
        audio=tmp_path / "a.wav",
        output=tmp_path / "o.mp4",
        titles=tmp_path / "t.ass",
        shape=FULL,
        palette=palette_for("Amapiano"),
        start_sec=0,
        duration_sec=480,
        background=picture,
    )
    command = build_command(spec, encoder="h264_nvenc")
    joined = " ".join(command)
    assert command[command.index("-loop") : command.index("-loop") + 2] == ["-loop", "1"]
    assert str(picture) in command
    assert "gradients=" not in joined
    assert "crop=1920:1080" in joined and "sin(2*PI*t/" in joined


def test_a_song_video_is_titled_by_the_song_and_carries_its_lyrics(tmp_path: Path) -> None:
    wide, tall = tmp_path / "w.png", tmp_path / "t.png"
    wide.write_bytes(_png(8, 8))
    tall.write_bytes(_png(8, 8))
    art = TrackArtwork(
        wide=str(wide), tall=str(tall), prompt="p", model="m", created_at=datetime.now(UTC)
    )
    song = track(artwork=art)
    assert headline(song) == "Candle Light"
    title, text, tags = describe(song, FULL)
    assert title.startswith("Candle Light |")
    assert "Lyrics:\nWe lost the lights\nCandle light" in text and "[Verse]" not in text
    assert "FLUX.2 klein" in text and "song" in tags and "instrumental" not in tags
    assert background_for(song, SHORT) == tall

    long_song = track(lyrics="[Verse]\n" + "\n".join(["a long line of lyrics here"] * 120))
    _, long_text, _ = describe(long_song, FULL)
    assert len(long_text) <= 2000 and long_text.endswith("ClipForge Music · Candle Light")
