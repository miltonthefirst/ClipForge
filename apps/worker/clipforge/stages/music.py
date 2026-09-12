"""The MUSIC stage: score a finished clip, and produce a new one.

## Why this is its own job

For the same reason PUBLISH is. Adding music is a decision someone makes while
watching a clip, on the far side of a render that may have happened hours
earlier — it cannot be a stage of CLIP, because CLIP has already finished by the
time anyone has an opinion about the soundtrack.

## Why it produces a new clip rather than editing one

Because the clip that was reviewed is the clip that was reviewed. Overwriting it
would mean the thing published is not the thing anyone approved, and there would
be no way back to the original if the music was a mistake. The new clip carries
``derivedFromClipId``, so the two are relatable without being confusable.

## Captions

Captions are burned into pixels by RENDER, so KEEP and REMOVE are not two
settings of one operation — they are two different operations:

* **KEEP** touches the picture not at all. The video stream is copied, which is
  instant and lossless, and only the audio is rebuilt.
* **REMOVE** has to re-render the segment from the original source without the
  subtitle filter, which needs the source media to still be on this machine. It
  usually is; when the workspace GC has taken it, this fails and says so rather
  than silently keeping captions the operator asked to remove.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from clipforge_contracts import (
    AppliedMusic,
    Clip,
    ClipLocation,
    Lane,
    MusicCaptions,
    ReviewState,
    StageName,
)

from clipforge.config import Settings
from clipforge.media.beats import analyse
from clipforge.media.music import build_ffmpeg_args, plan_music
from clipforge.media.sources import resolve_audio_source
from clipforge.media.workspace import Workspace
from clipforge.observability import get_logger
from clipforge.stages.base import StageContext, StageOutcome
from clipforge.store.blobs import BlobStore
from clipforge.store.firestore import CandidateStore, ClipStore, SourceStore

log = get_logger(__name__)

__all__ = ["MusicStage", "MusicStageError"]


class MusicStageError(RuntimeError):
    """Something about the request cannot work. Never retryable.

    Every failure here is a property of the inputs — a track with no audio, a
    source that has been garbage-collected, a clip that does not exist. Retrying
    produces the identical error twice more and leaves the operator watching a
    job that looks merely slow.
    """


class MusicStage:
    """Add a track to a clip, and write the result as a new clip."""

    name = StageName.MUSIC
    lane = Lane.CPU

    def __init__(
        self,
        *,
        settings: Settings,
        clips: ClipStore,
        candidates: CandidateStore,
        sources: SourceStore,
        workspace: Workspace,
        blobs: BlobStore,
    ) -> None:
        self._settings = settings
        self._clips = clips
        self._candidates = candidates
        self._sources = sources
        self._workspace = workspace
        self._blobs = blobs

    def run(self, context: StageContext) -> StageOutcome:
        job = context.job
        options = job.music_options
        if options is None or not job.clip_id:
            raise MusicStageError("a MUSIC job needs a clipId and musicOptions")

        original = self._clips.get(job.clip_id)
        if original is None:
            raise MusicStageError(f"no such clip: {job.clip_id}")

        source_path = Path(original.local_path)
        if not source_path.is_file():
            raise MusicStageError(
                f"the clip's file is gone from this machine ({source_path.name}). "
                "Music is added to the rendered file, which lives on the worker."
            )

        # ── The track ────────────────────────────────────────────────────────
        track = resolve_audio_source(
            options.source, self._workspace.tmp_dir, ffmpeg=self._settings.ffmpeg_bin
        )
        analysis = analyse(track.path, self._settings.ffmpeg_bin)

        clip_duration = float(original.duration_sec or 0.0)
        if clip_duration <= 0:
            raise MusicStageError("the clip has no duration recorded, so nothing can be planned")

        plan = plan_music(
            analysis,
            clip_duration_sec=clip_duration,
            mode=options.mode,
            gain_db=options.gain_db,
            # The contract defaults this to true; None means the client omitted it.
            align_to_beat=options.align_to_beat is not False,
        )

        # ── The picture ──────────────────────────────────────────────────────
        if options.captions is MusicCaptions.REMOVE:
            picture = self._rerender_without_captions(original)
            reencode = False  # already encoded, just now caption-free
        else:
            picture = source_path
            reencode = False

        # ── The mix ──────────────────────────────────────────────────────────
        clip_id = uuid.uuid4().hex
        key = f"clips/{job.uid}/{clip_id}.mp4"
        staged = self._workspace.tmp_dir / f"{clip_id}.mp4"

        args = build_ffmpeg_args(
            plan,
            clip_path=str(picture),
            music_path=str(track.path),
            output_path=str(staged),
            reencode_video=reencode,
            ffmpeg=self._settings.ffmpeg_bin,
        )
        _run(args, what="mixing the music")

        ref = self._blobs.put(key, staged, content_type="video/mp4")
        staged.unlink(missing_ok=True)

        now = datetime.now(UTC)
        scored = Clip(
            id=clip_id,
            uid=job.uid,
            candidate_id=original.candidate_id,
            source_id=original.source_id,
            job_id=job.id,
            derived_from_clip_id=original.id,
            location=(
                ClipLocation.REMOTE
                if (ref.storage_path or ref.playback_url)
                else ClipLocation.LOCAL
            ),
            local_path=str(ref.local_path),
            playback_url=ref.playback_url,
            storage_path=ref.storage_path,
            playback_expires_at=ref.expires_at,
            duration_sec=original.duration_sec,
            width_px=original.width_px,
            height_px=original.height_px,
            size_bytes=ref.size_bytes,
            render_profile=original.render_profile,
            title=original.title,
            description=original.description,
            # PENDING on purpose. The clip that was approved is the one without
            # music; this is a different edit and deserves to be watched before
            # it goes anywhere.
            review=ReviewState.PENDING,
            # The video's rights carry over — it is the same footage. The
            # music's own attestation is recorded beside it, not merged into it:
            # they are two claims about two things, and a Content ID match will
            # be against one or the other.
            rights=original.rights,
            music=AppliedMusic(
                mode=options.mode,
                captions=options.captions,
                source=options.source,
                track_title=track.title,
                tempo_bpm=plan.tempo_bpm,
                music_start_sec=plan.music_start_sec,
                rights=options.rights,
            ),
            created_at=now,
        )
        self._clips.save(scored)

        log.info(
            "music.applied",
            clip_id=clip_id,
            derived_from=original.id,
            mode=options.mode.value,
            captions=options.captions.value,
            tempo_bpm=round(plan.tempo_bpm, 1) if plan.tempo_bpm else None,
            music_start_sec=plan.music_start_sec,
            size_mb=round(ref.size_bytes / 1_048_576, 2),
        )

        return StageOutcome(
            detail=(
                f"{options.mode.value.lower()} at "
                f"{round(plan.tempo_bpm) if plan.tempo_bpm else '?'} BPM"
            ),
            metadata={"clipId": clip_id},
        )

    def _rerender_without_captions(self, original: Clip) -> Path:
        """Cut the segment again from the source, with no subtitle filter.

        The only way to remove captions: RENDER burns them into the picture, so
        there is nothing to switch off in the finished file.
        """
        from clipforge.media.ffprobe import probe
        from clipforge.media.profiles import load_profile
        from clipforge.media.render import RenderRequest, render_clip

        candidate = self._candidates.get(original.candidate_id)
        if candidate is None:
            raise MusicStageError(
                "removing captions needs the clip's candidate, which recorded where in "
                "the source it was cut from, and it is no longer there."
            )

        source = self._sources.get(original.source_id) if original.source_id else None
        if source is None or not source.local_path or not Path(source.local_path).is_file():
            raise MusicStageError(
                "removing captions means re-cutting from the original video, which is no "
                "longer on this machine — the workspace garbage collector reclaims sources. "
                "Keep the captions, or re-ingest the source first."
            )

        media = probe(Path(source.local_path), ffprobe_bin=self._settings.ffprobe_bin)
        profile = load_profile(original.render_profile or "default")
        staged = self._workspace.tmp_dir / f"nocaptions-{uuid.uuid4().hex}.mp4"

        render_clip(
            RenderRequest(
                source=Path(source.local_path),
                destination=staged,
                start_sec=candidate.start_sec,
                end_sec=candidate.end_sec,
                profile=profile,
                # The whole point: same cut, same reframe, no subtitle filter.
                subtitles=None,
            ),
            media,
            ffmpeg_bin=self._settings.ffmpeg_bin,
        )
        return staged


def _run(argv: list[str], *, what: str) -> None:
    import subprocess

    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise MusicStageError(f"{what} failed: {proc.stderr[-600:]}")
