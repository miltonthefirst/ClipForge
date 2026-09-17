"""The MUSIC stage: the clip it writes, and what that clip says about itself.

The mix itself is `clipforge.media.scoring`'s business and is tested there. What
is left here is the record and the picture, and they went wrong together: a
scored clip is a *version* of the clip it was made from, and it was being
written as though it were a fresh one.

**It carries the corrections its parent had.** `AppliedRemake` is where the
window actually cut is written down, and `RemakeStage._cut` reads nothing else —
so a scored clip without it sent the next remake back through `candidateId` to
the original window, silently discarding the trim, the reframe and the narration
the reviewer had already approved. Two features that work alone and destroy each
other when combined is the reviewer's own report of this.

**And the picture has to deserve them.** Taking the captions off re-cuts from
the source, and the re-cut used the candidate's window with no framing and no
hidden rectangles while the record went on asserting the parent's — so the trim
and the reframe were destroyed in the pixels and claimed in the document at the
same time. The tests for that branch assert the ffmpeg request and the finished
file, because the record was the half that lied.

**It records what the mix was given, not only what came out.** Reproducing an
approved soundtrack on a re-cut needs every input to it — the level and the beat
alignment as well as the track and the offset — and a remake that has to guess
at them comes back at the stage's defaults, quietly undoing a correction that
had already been made.

Every test uses a local file as the track, so nothing reaches the network.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from clipforge.config import Settings
from clipforge.media.ffprobe import probe
from clipforge.media.render import render_clip
from clipforge.models.broker import ModelBroker
from clipforge.stages import music as music_module
from clipforge.stages.base import StageContext
from clipforge.stages.music import MusicStage, MusicStageError
from clipforge.store.blobs import BlobRef
from clipforge_contracts import (
    AppliedMusic,
    AppliedRemake,
    AppliedVoice,
    Candidate,
    Clip,
    ClipLocation,
    FramingMode,
    Job,
    JobStatus,
    JobType,
    Lane,
    MusicCaptions,
    MusicMode,
    MusicOptions,
    ObscureMethod,
    ObscureOptions,
    ObscureRegion,
    ReviewState,
    Source,
    SpeechMode,
    Stage,
    StageName,
    StageStatus,
    SubScores,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

pytestmark = pytest.mark.unit


# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeClipStore:
    def __init__(self, clip: Clip) -> None:
        self._clip = clip
        self.saved: list[Clip] = []

    def get(self, clip_id: str) -> Clip | None:
        return self._clip if self._clip.id == clip_id else None

    def save(self, clip: Clip, preview: object = None) -> None:
        self.saved.append(clip)


class FakeCandidateStore:
    """The candidate, which only the caption-free path reads — and only when the
    clip has no remake of its own to say where it was cut from."""

    def __init__(self, the_candidate: Candidate | None = None) -> None:
        self._candidate = the_candidate

    def get(self, candidate_id: str) -> Candidate | None:
        if self._candidate is not None and self._candidate.id == candidate_id:
            return self._candidate
        return None


class FakeSourceStore:
    def __init__(self, the_source: Source | None = None) -> None:
        self._source = the_source

    def get(self, source_id: str) -> Source | None:
        if self._source is not None and self._source.id == source_id:
            return self._source
        return None


class FakeBlobStore:
    """Records what was uploaded, and keeps a copy of it.

    The copy is not incidental. The stage unlinks the staged file the moment the
    upload returns, so by the time a test can look at the clip it saved, the
    file that clip describes is gone — and the assertion that matters for the
    picture is on the file itself.
    """

    def __init__(self, keep_in: Path) -> None:
        self.puts: list[tuple[str, Path]] = []
        self.kept: list[Path] = []
        self._keep_in = keep_in

    def put(
        self, key: str, source: Path, *, content_type: str | None = None, required: bool = False
    ) -> BlobRef:
        self.puts.append((key, source))
        self._keep_in.mkdir(parents=True, exist_ok=True)
        copy = self._keep_in / f"{len(self.kept)}-{source.name}"
        shutil.copy2(source, copy)
        self.kept.append(copy)
        return BlobRef(key=key, local_path=source, size_bytes=source.stat().st_size)


class FakeWorkspace:
    def __init__(self, root: Path) -> None:
        self.tmp_dir = root / "tmp"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        # Tracks live outside tmp so they survive a worker restart.
        self.music_dir = root / "music"
        self.music_dir.mkdir(parents=True, exist_ok=True)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def make_video(path: Path, *, seconds: int = 6, audio: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=256x144:rate=24:duration={seconds}",
    ]
    if audio:
        argv += ["-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}", "-c:a", "aac"]
    argv += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(argv, check=True, capture_output=True)  # noqa: S603 - fixed argv
    return path


def make_track(path: Path, *, seconds: float = 20.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-ar",
            "44100",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def clip(path: Path, **overrides: object) -> Clip:
    defaults: dict[str, object] = {
        "id": "clip-1",
        "uid": "user-1",
        "candidate_id": "cand-1",
        "source_id": "src-1",
        "job_id": "job-0",
        "location": ClipLocation.LOCAL,
        "local_path": str(path),
        "duration_sec": 6.0,
        "width_px": 256,
        "height_px": 144,
        "render_profile": "default:v1",
        "title": "a goal",
        "review": ReviewState.APPROVED,
        "created_at": NOW,
    }
    return Clip(**{**defaults, **overrides})


def job(options: MusicOptions) -> Job:
    return Job(
        id="job-1",
        uid="user-1",
        type=JobType.MUSIC,
        status=JobStatus.RUNNING,
        clip_id="clip-1",
        music_options=options,
        stages=[Stage(name=StageName.MUSIC, lane=Lane.CPU, status=StageStatus.RUNNING)],
        attempts=1,
        max_attempts=1,
        created_at=NOW,
        updated_at=NOW,
    )


def score(
    tmp_path: Path,
    the_clip: Clip,
    options: MusicOptions,
    *,
    the_candidate: Candidate | None = None,
    the_source: Source | None = None,
) -> tuple[Clip, FakeBlobStore]:
    """Score a clip, and hand back both halves of what it produced.

    The record and the file, because the two disagreeing is what this stage got
    wrong: the picture it wrote and the picture its document described were not
    the same picture.
    """
    settings = Settings(_env_file=None, video_encoder="libx264")
    clips = FakeClipStore(the_clip)
    blobs = FakeBlobStore(tmp_path / "uploaded")
    stage = MusicStage(
        settings=settings,
        clips=clips,  # type: ignore[arg-type]
        candidates=FakeCandidateStore(the_candidate),  # type: ignore[arg-type]
        sources=FakeSourceStore(the_source),  # type: ignore[arg-type]
        workspace=FakeWorkspace(tmp_path),  # type: ignore[arg-type]
        blobs=blobs,  # type: ignore[arg-type]
    )
    the_job = job(options)
    stage.run(
        StageContext(
            job=the_job,
            stage_name=StageName.MUSIC,
            checkpoint=None,
            settings=settings,
            broker=ModelBroker(reserve_mb=settings.vram_reserve_mb),
        )
    )
    assert clips.saved, "a MUSIC job that ran must write the clip it produced"
    return clips.saved[0], blobs


def run(tmp_path: Path, the_clip: Clip, options: MusicOptions, **kwargs: Any) -> Clip:
    """Score a clip and hand back what was written for it."""
    return score(tmp_path, the_clip, options, **kwargs)[0]


def cuts(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every render the stage asks for, in order, still really rendered.

    What the stage tells ffmpeg to draw IS the picture, and it is where the
    window and the reframe were being thrown away — so the request is asserted
    on directly rather than inferred from the file that comes out of it.
    """
    requests: list[Any] = []

    def recording(request: Any, *args: Any, **kwargs: Any) -> Any:
        requests.append(request)
        return render_clip(request, *args, **kwargs)

    monkeypatch.setattr(music_module, "render_clip", recording)
    return requests


def candidate(start_sec: float = 0.0, end_sec: float = 20.0) -> Candidate:
    return Candidate(
        id="cand-1",
        uid="user-1",
        source_id="src-1",
        start_sec=start_sec,
        end_sec=end_sec,
        sub_scores=SubScores(
            hook=5, curiosity=5, standalone=5, emotion=5, pacing=5, shareability=5
        ),
        total=60,
        created_at=NOW,
    )


def source(path: Path | None) -> Source:
    return Source(
        id="src-1",
        uid="user-1",
        provider="youtube",
        external_id="abc",
        url="https://example.invalid/abc",
        local_path=str(path) if path else None,
        content_hash="deadbeef",
        created_at=NOW,
    )


def narrated(**overrides: object) -> AppliedVoice:
    """The narration record a re-voiced parent carries."""
    defaults: dict[str, object] = {
        "mode": SpeechMode.REPLACE,
        "voice": "af_heart",
        "language": "en",
        "engine": "stub-tts",
        "spoken_text": "Bayern are in behind.",
        "speech_duration_sec": 4.0,
    }
    return AppliedVoice(**{**defaults, **overrides})


def options(track: Path, **overrides: object) -> MusicOptions:
    defaults: dict[str, object] = {
        "source": str(track),
        "mode": MusicMode.BED,
        "captions": MusicCaptions.KEEP,
    }
    return MusicOptions(**{**defaults, **overrides})


# ── The record the scored clip carries ───────────────────────────────────────


def test_the_scored_clip_keeps_the_corrections_its_parent_already_had(
    tmp_path: Path,
) -> None:
    """Adding music re-cuts nothing, so every correction still describes the clip.

    `AppliedRemake` is the only record of the window actually cut, and
    `RemakeStage._cut` reads nothing else. Dropped here, a remake of a scored
    clip resolves back through `candidateId` to the window the candidate chose
    and silently throws away the trim, the reframe and the narration — so adding
    music to a remade clip destroyed the remake, and remaking a scored clip
    destroyed the music.
    """
    corrected = clip(
        make_video(tmp_path / "clips" / "clip-1.mp4"),
        tags=["bayern", "champions league"],
        remake=AppliedRemake(framing_mode=FramingMode.FIT, start_sec=101.5, end_sec=107.5),
    )

    scored = run(tmp_path, corrected, options(make_track(tmp_path / "music" / "bed.wav")))

    assert scored.remake == corrected.remake
    assert [str(tag.root) for tag in scored.tags or []] == ["bayern", "champions league"]
    assert scored.derived_from_clip_id == corrected.id


def test_the_level_and_the_alignment_are_recorded_so_a_remake_can_reproduce_them(
    tmp_path: Path,
) -> None:
    """The music is baked into the rendered file, so a remake re-renders and has
    to mix the track again. Without these two numbers it comes back at the
    stage's own defaults, quietly undoing a correction already approved."""
    scored = run(
        tmp_path,
        clip(make_video(tmp_path / "clips" / "clip-1.mp4")),
        options(make_track(tmp_path / "music" / "bed.wav"), gain_db=-9.0, align_to_beat=False),
    )

    assert scored.music is not None
    assert scored.music.gain_db == -9.0
    assert scored.music.align_to_beat is False
    # The offset chosen here, so the same excerpt can be asked for by name later.
    assert scored.music.music_start_sec is not None


def test_a_bed_over_a_clip_with_no_audio_is_downgraded_rather_than_refused(
    tmp_path: Path,
) -> None:
    """The BED filtergraph names `[0:a]`, and against a video-only input ffmpeg
    refuses the whole invocation — "Stream specifier ':a' matches no streams",
    exit -22, nothing written. A silent segment is an ordinary thing to want
    music on, so the mode gives way instead, and the clip records the mode that
    ran rather than the one that was asked for."""
    silent = clip(make_video(tmp_path / "clips" / "clip-1.mp4", audio=False))

    scored = run(tmp_path, silent, options(make_track(tmp_path / "music" / "bed.wav")))

    assert scored.music is not None
    assert scored.music.mode is MusicMode.REPLACE


# ── The picture the caption-free re-cut actually produces ────────────────────
#
# `captions=REMOVE` is the one branch that re-renders, and it used to re-render
# the wrong thing: `candidate.start_sec`-`candidate.end_sec` with no framing, no
# window path and no hidden rectangles, while the clip was saved carrying the
# parent's `AppliedRemake` anyway. So the reviewer's trim, their reframe and
# their blur boxes were destroyed in the pixels and asserted in the document.
# These tests look at the request ffmpeg was given and at the file that came
# back, because the record was the half that lied.


def remade(**overrides: object) -> AppliedRemake:
    """A parent that has already been corrected: trimmed, reframed and blurred."""
    defaults: dict[str, object] = {
        "framing_mode": FramingMode.FIT,
        "start_sec": 4.0,
        "end_sec": 10.0,
        "obscured": [
            ObscureRegion(
                x_pct=2.0,
                y_pct=2.0,
                w_pct=20.0,
                h_pct=12.0,
                method=ObscureMethod.BOX,
                label="channel bug, top left",
            )
        ],
    }
    return AppliedRemake(**{**defaults, **overrides})


def test_removing_the_captions_keeps_the_window_the_reviewer_trimmed_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's own report, in the pixels.

    A 6.0-second clip trimmed to 4.0-10.0 of its source came back as the
    candidate's whole 20.0-second window, and the document went on saying
    4.0-10.0. The candidate is deliberately a different window from the remake,
    so a re-cut reading the wrong one cannot accidentally pass.
    """
    media = make_video(tmp_path / "src" / "source.mp4", seconds=24)
    corrected = clip(make_video(tmp_path / "clips" / "clip-1.mp4"), remake=remade())
    requests = cuts(monkeypatch)

    scored, blobs = score(
        tmp_path,
        corrected,
        options(make_track(tmp_path / "music" / "bed.wav"), captions=MusicCaptions.REMOVE),
        the_candidate=candidate(0.0, 20.0),
        the_source=source(media),
    )

    assert [(r.start_sec, r.end_sec) for r in requests] == [(4.0, 10.0)]
    # The file, not only the request: `-t` on the mix bounds the copied video
    # stream too, so the length that survives the whole stage is the assertion.
    assert probe(blobs.kept[0]).duration_sec == pytest.approx(6.0, abs=0.3)
    assert scored.duration_sec == pytest.approx(6.0, abs=0.3)
    assert scored.remake is not None
    assert (scored.remake.start_sec, scored.remake.end_sec) == (4.0, 10.0)


def test_removing_the_captions_keeps_the_reframe_and_the_boxes_that_were_drawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other two things the re-cut threw away.

    A FIT clip came back centre-cropped to its middle third, and a watermark its
    parent had blurred was sharp again in the child — while `framingMode: FIT`
    and the rectangle both stayed on the record. Asserted on the filtergraph,
    because that is the instruction that draws the picture: a 1080x1920 output
    is 1080x1920 whichever way it was framed.
    """
    media = make_video(tmp_path / "src" / "source.mp4", seconds=24)
    corrected = clip(make_video(tmp_path / "clips" / "clip-1.mp4"), remake=remade())
    requests = cuts(monkeypatch)

    scored, _ = score(
        tmp_path,
        corrected,
        options(make_track(tmp_path / "music" / "bed.wav"), captions=MusicCaptions.REMOVE),
        the_candidate=candidate(0.0, 20.0),
        the_source=source(media),
    )

    graph = requests[0].video_filter
    assert graph is not None
    assert "split=2[fitbg][fitfg]" in graph, "the whole frame was fitted, not centre-cropped"
    assert "drawbox=" in graph and "[obscured]" in graph, "the box the reviewer drew is gone"
    assert "subtitles" not in graph, "removing the captions is the one thing this branch is for"
    assert scored.remake is not None
    assert scored.remake.framing_mode is FramingMode.FIT
    assert [r.label for r in scored.remake.obscured or []] == ["channel bug, top left"]


def test_a_clip_that_render_made_is_still_cut_from_its_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback, and the only case the old code got right. A clip with no
    remake has nowhere else to learn where it was cut from, and the candidate is
    the sole record of it."""
    media = make_video(tmp_path / "src" / "source.mp4", seconds=24)
    requests = cuts(monkeypatch)

    scored, _ = score(
        tmp_path,
        clip(make_video(tmp_path / "clips" / "clip-1.mp4")),
        options(make_track(tmp_path / "music" / "bed.wav"), captions=MusicCaptions.REMOVE),
        the_candidate=candidate(3.0, 9.0),
        the_source=source(media),
    )

    assert [(r.start_sec, r.end_sec) for r in requests] == [(3.0, 9.0)]
    assert scored.remake is None, "there was no correction to claim"


def test_a_channel_bug_hidden_at_render_is_hidden_again_by_the_re_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RENDER hides whatever the source says is always there and writes nothing
    down, so a clip it made carries no record of the rectangle. Reading only the
    parent's `AppliedRemake` therefore brought the bug back on a job whose whole
    request was to take the captions off."""
    media = make_video(tmp_path / "src" / "source.mp4", seconds=24)
    channel = source(media)
    channel.obscure = ObscureOptions(
        regions=[
            ObscureRegion(x_pct=70.0, y_pct=4.0, w_pct=22.0, h_pct=10.0, method=ObscureMethod.BOX)
        ]
    )
    requests = cuts(monkeypatch)

    score(
        tmp_path,
        clip(make_video(tmp_path / "clips" / "clip-1.mp4")),
        options(make_track(tmp_path / "music" / "bed.wav"), captions=MusicCaptions.REMOVE),
        the_candidate=candidate(3.0, 9.0),
        the_source=channel,
    )

    assert "drawbox=" in (requests[0].video_filter or "")


def test_a_reframe_that_cannot_be_reproduced_is_given_up_and_written_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pan with no points cannot be replayed, and there is no honest way to
    guess where the window was. The fixed crop of the same window plus a record
    that says AS_RENDERED is the answer; carrying `framingMode: PAN` over a
    centred crop is the one option that cannot be true."""
    media = make_video(tmp_path / "src" / "source.mp4", seconds=24)
    corrected = clip(
        make_video(tmp_path / "clips" / "clip-1.mp4"),
        remake=AppliedRemake(
            framing_mode=FramingMode.PAN, keyframes=[], start_sec=4.0, end_sec=10.0
        ),
    )
    requests = cuts(monkeypatch)

    scored, _ = score(
        tmp_path,
        corrected,
        options(make_track(tmp_path / "music" / "bed.wav"), captions=MusicCaptions.REMOVE),
        the_candidate=candidate(0.0, 20.0),
        the_source=source(media),
    )

    assert "crop='" in (requests[0].video_filter or ""), "a fixed window, since none was recorded"
    assert scored.remake is not None
    assert scored.remake.framing_mode is FramingMode.AS_RENDERED
    assert any("recorded no window positions" in str(w.root) for w in scored.remake.warnings or [])
    # The trim it could reproduce is still reproduced.
    assert (requests[0].start_sec, requests[0].end_sec) == (4.0, 10.0)


# ── What the record may say about the audio ──────────────────────────────────


def test_a_caption_free_re_cut_drops_the_narration_it_no_longer_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`AppliedRemake.voice` is a claim about AUDIO, and a re-cut takes the
    source video's own sound — so the narration the parent was given is not in
    this file whatever the mix mode is. A production scored clip measured
    -40.9 dB against its parent's narration while still recording one."""
    media = make_video(tmp_path / "src" / "source.mp4", seconds=24)
    corrected = clip(make_video(tmp_path / "clips" / "clip-1.mp4"), remake=remade(voice=narrated()))
    cuts(monkeypatch)

    scored, _ = score(
        tmp_path,
        corrected,
        options(make_track(tmp_path / "music" / "bed.wav"), captions=MusicCaptions.REMOVE),
        the_candidate=candidate(0.0, 20.0),
        the_source=source(media),
    )

    assert scored.remake is not None
    assert scored.remake.voice is None
    assert any("source's own sound" in str(w.root) for w in scored.remake.warnings or [])
    # The picture half is untouched by any of that.
    assert scored.remake.framing_mode is FramingMode.FIT


def test_a_track_that_replaces_the_audio_drops_the_narration_it_replaced(
    tmp_path: Path,
) -> None:
    """The captions were kept, so the picture is the parent's own file and every
    word about it still holds. `MusicMode.REPLACE` drops `[0:a]` from the graph
    entirely, and `[0:a]` is where the narration was."""
    corrected = clip(
        make_video(tmp_path / "clips" / "clip-1.mp4"),
        remake=AppliedRemake(
            framing_mode=FramingMode.FIT, start_sec=4.0, end_sec=10.0, voice=narrated()
        ),
    )

    scored = run(
        tmp_path,
        corrected,
        options(make_track(tmp_path / "music" / "bed.wav"), mode=MusicMode.REPLACE),
    )

    assert scored.remake is not None
    assert scored.remake.voice is None
    assert any("replaced this clip's audio" in str(w.root) for w in scored.remake.warnings or [])
    assert (scored.remake.start_sec, scored.remake.end_sec) == (4.0, 10.0)


def test_a_bed_over_the_kept_picture_leaves_the_narration_where_it_was(tmp_path: Path) -> None:
    """The one path the whole record survives: nothing re-cut the picture, so
    `[0:a]` is still the parent's audio, and BED is the mode that keeps it."""
    corrected = clip(
        make_video(tmp_path / "clips" / "clip-1.mp4"),
        remake=AppliedRemake(
            framing_mode=FramingMode.FIT, start_sec=4.0, end_sec=10.0, voice=narrated()
        ),
    )

    scored = run(
        tmp_path, corrected, options(make_track(tmp_path / "music" / "bed.wav"), mode=MusicMode.BED)
    )

    assert scored.remake == corrected.remake


def test_a_bed_downgraded_to_replace_still_drops_the_narration(tmp_path: Path) -> None:
    """A BED against a segment with no audio is downgraded to REPLACE, and the
    downgrade takes the narration with it. The mode that RAN decides this, which
    is why it is read off the plan rather than off the request."""
    silent = clip(
        make_video(tmp_path / "clips" / "clip-1.mp4", audio=False),
        remake=AppliedRemake(
            framing_mode=FramingMode.AS_RENDERED, start_sec=4.0, end_sec=10.0, voice=narrated()
        ),
    )

    scored = run(tmp_path, silent, options(make_track(tmp_path / "music" / "bed.wav")))

    assert scored.music is not None and scored.music.mode is MusicMode.REPLACE
    assert scored.remake is not None
    assert scored.remake.voice is None


# ── Adding a second track over the first ─────────────────────────────────────


def test_a_clip_that_already_has_a_soundtrack_is_refused(tmp_path: Path) -> None:
    """A track is mixed into the audio rather than kept beside it, so a second
    one leaves both playing and cannot be un-mixed afterwards. The clip page
    gates its Add-a-track panel on `music` being null and nothing server-side
    backed that up, so a stale tab was all it took."""
    already = clip(
        make_video(tmp_path / "clips" / "clip-1.mp4"),
        music=AppliedMusic(
            mode=MusicMode.BED,
            captions=MusicCaptions.KEEP,
            source="C:/tracks/slow-burn.m4a",
            track_title="Slow Burn",
        ),
    )

    with pytest.raises(MusicStageError, match="already has a soundtrack") as caught:
        run(tmp_path, already, options(make_track(tmp_path / "music" / "bed.wav")))

    assert "Slow Burn" in str(caught.value)
