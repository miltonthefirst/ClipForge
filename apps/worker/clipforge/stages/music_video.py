"""VISUALIZE: a kept track becomes a video and a Shorts cut (docs/MUSIC-PLAN.md, M4).

The single stage of a MUSIC_VIDEO job, on the CPU lane: ffmpeg draws the picture
from the music (clipforge/media/visualizer.py), so the GPU is free for whatever
else is queued. Each shape asked for becomes a clip, PENDING review, and from
there it is reviewed and published like any other; the clip carries the track's
id, which is what makes publishing declare it as synthetic media.

Only a track a person KEPT is made into a video: the rules check that when the
job is created, and this checks it again, because the Admin SDK reads past rules.
"""

from __future__ import annotations

import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

from clipforge_contracts import (
    Clip,
    ClipLocation,
    ClipPreview,
    MusicVideoOptions,
    ReviewState,
    StageName,
    Track,
    TrackStatus,
)
from clipforge_contracts import Lane as LaneEnum

from clipforge.media.poster import PosterError, extract_poster
from clipforge.media.render import RenderError
from clipforge.media.visualizer import (
    FULL,
    SHORT,
    Shape,
    VisualSpec,
    build_ass,
    loudness_profile,
    palette_for,
    render,
    strongest_window,
)
from clipforge.media.workspace import Workspace
from clipforge.observability import get_logger
from clipforge.stages.base import StageContext, StageOutcome
from clipforge.store.blobs import BlobStore
from clipforge.store.firestore import ClipStore, TrackStore

log = get_logger(__name__)

__all__ = ["MusicVideoError", "VisualizeStage", "describe", "headline"]

FOOTER = "ClipForge Music · AI-generated (ACE-Step)"
DESCRIPTION_LIMIT = 2000
PROFILE = {"full": "music-16x9", "short": "music-short-9x16"}


class MusicVideoError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = "MUSIC_VIDEO"


def _phrases(prompt: str) -> list[str]:
    return [p.strip() for p in prompt.split(",") if p.strip() and p.strip() != "instrumental"]


def song_title(track: Track) -> str:
    """The title without a take number: 'Candle Light (take 2)' is 'Candle Light'."""
    return re.sub(r"\s*\(take \d+\)$", "", track.title).strip() or track.title


def headline(track: Track) -> str:
    """A song's own title; for an instrumental, 'Uplifting cinematic build · strings and piano'."""
    if not track.instrumental and track.lyrics:
        return song_title(track)
    phrases = _phrases(track.prompt)[:2] or [track.title]
    text = " · ".join(phrases)
    return text[:1].upper() + text[1:]


def series_of(track: Track) -> str:
    if track.series:
        return track.series
    return track.title.split(" (")[0].strip() or "Music"


def describe(track: Track, shape: Shape) -> tuple[str, str, list[str]]:
    """The clip's title, description and tags."""
    series = series_of(track)
    head = headline(track)
    title = f"{head} #Shorts" if shape is SHORT else f"{head} | {series} music"
    lines = [
        track.prompt[:1].upper() + track.prompt[1:] + ".",
        "",
        "Made on our own PC with ACE-Step 1.5, an open music model (MIT). "
        "No samples and no one else's song: AI-generated music.",
    ]
    if track.artwork:
        lines.append("Background picture: AI-generated with FLUX.2 klein (Apache 2.0).")
    if track.lyrics and shape is not SHORT:
        sung = [line for line in track.lyrics.splitlines() if not line.strip().startswith("[")]
        lines += ["", "Lyrics:", *[line.strip() for line in sung]]
    lines += ["", f"ClipForge Music · {series}"]
    if shape is SHORT:
        lines += ["", "#Shorts"]
    description = "\n".join(lines)
    # A clip's description holds 2000 characters; the end of the lyrics gives way first,
    # and the series line is kept.
    footer = f"\n\nClipForge Music · {series}" + ("\n\n#Shorts" if shape is SHORT else "")
    if len(description) > DESCRIPTION_LIMIT:
        body = description[: DESCRIPTION_LIMIT - len(footer) - 2].rsplit("\n", 1)[0]
        description = body + "\n…" + footer
    kind = "instrumental" if track.instrumental else "song"
    tags = [series.lower(), "ai music", kind, "background music", "clipforge music"]
    return title[:100], description, [t[:40] for t in tags]


def background_for(track: Track, shape: Shape) -> Path | None:
    """The track's painted picture in this shape, if it has one on this PC."""
    if track.artwork is None:
        return None
    path = Path(track.artwork.tall if shape is SHORT else track.artwork.wide)
    return path if path.is_file() else None


class VisualizeStage:
    name = StageName.VISUALIZE
    lane = LaneEnum.CPU

    def __init__(
        self, *, clips: ClipStore, tracks: TrackStore, workspace: Workspace, blobs: BlobStore
    ) -> None:
        self._clips = clips
        self._tracks = tracks
        self._workspace = workspace
        self._blobs = blobs

    def run(self, context: StageContext) -> StageOutcome:
        options: MusicVideoOptions | None = context.job.music_video_options
        if options is None:
            raise MusicVideoError("a MUSIC_VIDEO job needs musicVideoOptions")
        track = self._tracks.get(options.track_id)
        if track is None:
            raise MusicVideoError(f"track {options.track_id} does not exist")
        if track.status is not TrackStatus.KEPT:
            raise MusicVideoError("only a track that was kept is made into a video")
        audio = Path(track.local_path)
        if not audio.is_file():
            raise MusicVideoError(f"the track's file is not on this PC any more: {audio}")

        settings = context.settings
        shapes = [s for s, wanted in ((FULL, options.full), (SHORT, options.short)) if wanted]
        if not shapes:
            raise MusicVideoError("nothing to make: neither the full video nor the Short")

        scratch = self._workspace.tmp_dir / f"music-video-{context.job.id}"
        scratch.mkdir(parents=True, exist_ok=True)
        made: dict[str, str] = {}
        try:
            for shape in shapes:
                context.progress(f"Drawing the {'Short' if shape is SHORT else 'full video'}")
                if shape is SHORT:
                    seconds = float(min(options.short_seconds or 55, track.seconds))
                    samples = loudness_profile(audio, ffmpeg_bin=settings.ffmpeg_bin)
                    start = strongest_window(samples, 8000, seconds)
                else:
                    seconds, start = float(track.seconds), 0.0
                titles = scratch / f"{shape.name}.ass"
                titles.write_text(
                    # A song is known by its title; an instrumental by its series.
                    build_ass(headline(track), series_of(track), FOOTER, shape)
                    if track.lyrics
                    else build_ass(series_of(track), headline(track), FOOTER, shape),
                    encoding="utf-8",
                    newline="\n",
                )
                output = scratch / f"{shape.name}.mp4"
                try:
                    render(
                        VisualSpec(
                            audio=audio,
                            output=output,
                            titles=titles,
                            shape=shape,
                            palette=palette_for(series_of(track)),
                            start_sec=start,
                            duration_sec=seconds,
                            background=background_for(track, shape),
                        ),
                        encoder=settings.video_encoder,
                        ffmpeg_bin=settings.ffmpeg_bin,
                    )
                    images = extract_poster(
                        output,
                        duration_sec=seconds,
                        work_dir=scratch,
                        ffmpeg_bin=settings.ffmpeg_bin,
                    )
                except (RenderError, PosterError) as exc:
                    raise MusicVideoError(str(exc), retryable=True) from exc

                # One id per job and shape, so a retry replaces rather than adds.
                clip_id = f"{context.job.id}-{shape.name}"
                context.progress("Storing the video")
                ref = self._blobs.put(
                    f"clips/{context.job.uid}/{clip_id}.mp4", output, content_type="video/mp4"
                )
                title, description, tags = describe(track, shape)
                now = datetime.now(UTC)
                clip = Clip(
                    id=clip_id,
                    uid=context.job.uid,
                    candidate_id=f"track-{track.id}",
                    source_id=None,
                    job_id=context.job.id,
                    lineage_id=clip_id,
                    version=1,
                    location=(
                        ClipLocation.REMOTE
                        if (ref.storage_path or ref.playback_url)
                        else ClipLocation.LOCAL
                    ),
                    local_path=str(ref.local_path),
                    playback_url=ref.playback_url,
                    storage_path=ref.storage_path,
                    playback_expires_at=ref.expires_at,
                    duration_sec=round(seconds, 3),
                    width_px=shape.width,
                    height_px=shape.height,
                    size_bytes=ref.size_bytes,
                    render_profile=PROFILE[shape.name],
                    title=title,
                    description=description,
                    tags=tags,
                    review=ReviewState.PENDING,
                    track_id=track.id,
                    created_at=now,
                )
                usable = images.width_px > 0 and images.height_px > 0
                self._clips.save(
                    clip,
                    preview=ClipPreview(
                        clip_id=clip_id,
                        poster_base64=images.poster_base64,
                        filmstrip_base64=images.filmstrip_base64,
                        width_px=images.width_px,
                        height_px=images.height_px,
                        byte_size=images.byte_size,
                        created_at=now,
                    )
                    if usable
                    else None,
                )
                made[shape.name] = clip_id
                log.info("music_video.made", shape=shape.name, clip_id=clip_id, seconds=seconds)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

        return StageOutcome(
            checkpoint={"clips": made},
            detail=f"made {len(made)} video{'s' if len(made) != 1 else ''} from {track.title}",
        )
