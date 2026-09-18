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

## What the scored clip is allowed to claim about itself

A scored clip is a *version* of the clip it was made from, so it carries that
clip's ``AppliedRemake`` — and every part of that record has to be true of the
file this stage just wrote, not merely true of its parent.

The picture half is made true by re-cutting faithfully: same window, same
framing, same window path, same hidden rectangles, minus the subtitle filter.
The audio half frequently is not, and cannot be made so. ``AppliedRemake.voice``
describes a narration, a re-cut takes the source video's own sound, and
``MusicMode.REPLACE`` drops the clip's audio altogether — so on every path but
one the narration the parent recorded is simply not in this file, and the voice
sub-record is dropped with a warning saying why. Asserting it anyway is the same
bug in a quieter field.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clipforge_contracts import (
    AppliedMusic,
    AppliedRemake,
    Clip,
    ClipLocation,
    FramingMode,
    Lane,
    MusicCaptions,
    MusicMode,
    ObscureFound,
    ObscureOptions,
    ReviewState,
    Source,
    StageName,
)

from clipforge.config import Settings
from clipforge.media.ffprobe import probe
from clipforge.media.framing import FramingError, build_video_chain
from clipforge.media.library import remember_music
from clipforge.media.obscure import merge_regions
from clipforge.media.profiles import load_profile
from clipforge.media.render import RenderRequest, render_clip
from clipforge.media.scoring import ScoringError, apply_music
from clipforge.media.workspace import Workspace
from clipforge.observability import get_logger
from clipforge.stages.base import StageContext, StageOutcome
from clipforge.stages.lineage import (
    inherited_framing,
    inherited_keyframes,
    inherited_profile,
    parent_window,
)
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

        if original.music is not None:
            # Nothing server-side stopped this before. The clip page hides the
            # Add-a-track panel while `music` is set, so in practice this only
            # fires on a stale tab or a job posted by hand — but a track is
            # mixed into the audio rather than kept beside it, so a second one
            # leaves both playing a few seconds apart and there is no un-mixing
            # it afterwards. The clip without a track is still there to score.
            raise MusicStageError(
                "this clip already has a soundtrack "
                f"({original.music.track_title or original.music.source}), and a track is "
                "mixed into the audio rather than kept beside it — adding a second one "
                "would leave both playing. Add music to the version that has none."
            )

        source_path = Path(original.local_path)
        if not source_path.is_file():
            raise MusicStageError(
                f"the clip's file is gone from this machine ({source_path.name}). "
                "Music is added to the rendered file, which lives on the worker."
            )

        # ── The picture, and the record that describes it ─────────────────────
        #
        # Decided together, because them going their separate ways is the bug:
        # the re-render used to rebuild a different picture from the one the
        # record then claimed.
        if options.captions is MusicCaptions.REMOVE:
            # Only this branch is worth announcing: it is a full re-encode of the
            # segment. Keeping the captions copies the video stream untouched,
            # and narrating something instant is how a ticker loses its meaning.
            context.progress("Re-rendering the clip without captions")
            picture, provenance = self._rerender_without_captions(original)
        else:
            # Untouched pixels, so whatever the parent's record says about them
            # is still exactly true.
            picture, provenance = source_path, original.remake

        # The file's own length and the file's own streams, never the clip
        # document's. `-t` bounds the copied video as well as the looping music,
        # so a duration that disagrees with the picture by a second throws that
        # second of footage away; and a BED against a segment with no audio has
        # nothing to duck under, which ffmpeg answers by refusing the whole
        # invocation rather than by making a quiet clip.
        media = probe(picture, ffprobe_bin=self._settings.ffprobe_bin)

        # ── The mix ──────────────────────────────────────────────────────────
        #
        # Through the shared seam, because REMAKE has to make the same mix to
        # carry an approved soundtrack across a re-cut, and two copies of five
        # calls is how the music was lost in the first place. The waiting is
        # narrated from in there: fetching one track off YouTube is tens of
        # seconds on this machine, and a stage that says nothing for tens of
        # seconds looks identical to one that has hung.
        clip_id = uuid.uuid4().hex
        key = f"clips/{job.uid}/{clip_id}.mp4"
        staged = self._workspace.tmp_dir / f"{clip_id}.mp4"

        try:
            result = apply_music(
                picture=picture,
                destination=staged,
                duration_sec=media.duration_sec,
                source=options.source,
                mode=options.mode,
                gain_db=options.gain_db,
                # The contract defaults this to true; None means the client omitted it.
                align_to_beat=options.align_to_beat is not False,
                # Nothing to reproduce. This is the first time this track meets
                # this clip, so the excerpt is being chosen here rather than
                # recovered from a mix somebody already approved.
                forced_start_sec=None,
                forced_tempo_bpm=None,
                has_original_audio=media.has_audio,
                work_dir=self._workspace.music_dir,
                ffmpeg=self._settings.ffmpeg_bin,
                ffprobe=self._settings.ffprobe_bin,
                on_progress=context.progress,
            )
        except ScoringError as exc:
            # The seam's never-retryable failure is this stage's never-retryable
            # failure, and the runner classifies by type rather than by message.
            # A TimeoutError from the same call is deliberately left alone: a
            # wedged ffmpeg is the one failure here another attempt gets past.
            raise MusicStageError(str(exc)) from exc

        # Keep score of the track. It is what the Sources list sorts by and what
        # decides, later, whether the collector may reclaim it — a bed used
        # twice is one the reviewer will come back to a third time.
        if result.track_path is not None:
            remember_music(
                self._sources,
                uid=job.uid,
                reference=options.source,
                path=result.track_path,
                title=result.track_title,
                ffmpeg_bin=self._settings.ffmpeg_bin,
            )

        # ── What is left of the parent's record ──────────────────────────────
        #
        # `AppliedRemake.voice` is a claim about AUDIO, and this clip's audio
        # comes down from its parent on exactly one path: the picture was the
        # parent's own file, so `[0:a]` is the parent's audio, AND the mix that
        # ran kept `[0:a]`, which only a BED does. A caption-free re-cut takes
        # the source video's own sound instead, and REPLACE drops the clip's
        # audio altogether. On every other path the narration is not in this
        # file — a production scored clip measured -40.9 dB against its parent's
        # narration while still recording one — so the sub-record goes and a
        # warning says which of the two reasons it was.
        #
        # `result.plan.mode` rather than the requested mode: a BED against a
        # segment with no audio is downgraded to REPLACE, and that downgrade
        # takes the narration with it too.
        kept_the_voice = (
            options.captions is MusicCaptions.KEEP and result.plan.mode is MusicMode.BED
        )
        if provenance is not None and provenance.voice is not None and not kept_the_voice:
            provenance = _restated(
                provenance,
                warning=(
                    "the picture had to be cut from the source again to take the captions "
                    "off, so this clip has the source's own sound and not the narration the "
                    "version it was made from had"
                    if options.captions is MusicCaptions.REMOVE
                    else "the track replaced this clip's audio, so the narration the version "
                    "it was made from had is no longer in it"
                ),
                voice=None,
            )

        # The mix is over; the note has to move with it. Putting a whole MP4 into
        # the bucket is the longer of the two waits on a home connection, and
        # until this line it was reported as mixing.
        context.progress("Uploading the finished clip")
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
            # A scored clip is a version of its parent too. It was already
            # linked by derivedFromClipId; joining the lineage is what stops it
            # occupying a second slot in the review queue.
            lineage_id=original.lineage_id or original.id,
            version=(original.version or 1) + 1,
            location=(
                ClipLocation.REMOTE
                if (ref.storage_path or ref.playback_url)
                else ClipLocation.LOCAL
            ),
            local_path=str(ref.local_path),
            playback_url=ref.playback_url,
            storage_path=ref.storage_path,
            playback_expires_at=ref.expires_at,
            # The picture's own length, which is what the mix was bounded to.
            # `original.duration_sec` is the clip document's idea of it, and a
            # caption-free re-render is free to disagree by a frame or two —
            # which a remake of this clip then reads as "the duration moved" and
            # re-picks the excerpt over.
            duration_sec=result.plan.duration_sec,
            width_px=original.width_px,
            height_px=original.height_px,
            size_bytes=ref.size_bytes,
            render_profile=original.render_profile,
            title=original.title,
            description=original.description,
            tags=list(original.tags or []),
            # PENDING on purpose. The clip that was approved is the one without
            # music; this is a different edit and deserves to be watched before
            # it goes anywhere.
            review=ReviewState.PENDING,
            music=AppliedMusic(
                # The mode that ran, which is not always the one asked for: a BED
                # against a segment with no audio is downgraded to REPLACE, and
                # recording BED would describe a mix nobody made.
                mode=result.plan.mode,
                captions=options.captions,
                source=options.source,
                track_title=result.track_title,
                tempo_bpm=result.plan.tempo_bpm,
                music_start_sec=result.plan.music_start_sec,
                # These two are copied from the request rather than read off the
                # plan, because they are the *inputs* a remake has to feed back
                # in to reproduce this mix. The plan's gain is the resolved
                # number, and recording it would read as an explicit choice on a
                # clip where the reviewer made none.
                gain_db=options.gain_db,
                align_to_beat=options.align_to_beat,
            ),
            # The parent's corrections, kept only as far as they are still true
            # of this file — see the two blocks above. Without any record at all
            # a remake of a scored clip falls back through `candidateId` to the
            # original window and silently discards the trim, the reframe and
            # the narration that were approved; `_cut` and the `inherited_*`
            # helpers in stages/lineage.py read nothing else.
            remake=provenance,
            created_at=now,
        )
        self._clips.save(scored)

        log.info(
            "music.applied",
            clip_id=clip_id,
            derived_from=original.id,
            mode=result.plan.mode.value,
            requested_mode=options.mode.value,
            captions=options.captions.value,
            tempo_bpm=round(result.plan.tempo_bpm, 1) if result.plan.tempo_bpm else None,
            music_start_sec=result.plan.music_start_sec,
            size_mb=round(ref.size_bytes / 1_048_576, 2),
        )

        return StageOutcome(
            detail=(
                f"{result.plan.mode.value.lower()} at "
                f"{round(result.plan.tempo_bpm) if result.plan.tempo_bpm else '?'} BPM"
            ),
            metadata={"clipId": clip_id},
        )

    def _rerender_without_captions(self, original: Clip) -> tuple[Path, AppliedRemake | None]:
        """Rebuild the picture this clip already has, minus the subtitle filter.

        The only way to remove captions: RENDER burns them into the pixels, so
        there is nothing to switch off in the finished file.

        **The cut has to be the parent's, not the candidate's.** This re-rendered
        `candidate.start_sec`-`candidate.end_sec` with no framing, no window path
        and no hidden rectangles, and the clip was still saved carrying the
        parent's `AppliedRemake` — so taking the captions off a remade clip threw
        the reviewer's trim, their reframe and their blur boxes out of the pixels
        while the document went on asserting all three. A 6.0 s FIT clip came
        back as 20.0 s of centre-cropped candidate; two production clips matched
        a plain centre crop of their source at 31-34 dB PSNR_y and their own
        remade parents at only 14-17, with a watermark the parent had blurred
        sharp again in the child.

        Returns the picture and the record that is true of it.
        """
        window = parent_window(original)
        if window is None:
            candidate = self._candidates.get(original.candidate_id)
            if candidate is None:
                raise MusicStageError(
                    "removing captions needs the clip's candidate, which recorded where in "
                    "the source it was cut from, and it is no longer there."
                )
            window = (candidate.start_sec, candidate.end_sec)

        source = self._sources.get(original.source_id) if original.source_id else None
        if source is None or not source.local_path or not Path(source.local_path).is_file():
            raise MusicStageError(
                "removing captions means re-cutting from the original video, which is no "
                "longer on this machine — the workspace garbage collector reclaims sources. "
                "Keep the captions, or re-ingest the source first."
            )

        media = probe(Path(source.local_path), ffprobe_bin=self._settings.ffprobe_bin)
        profile = load_profile(inherited_profile(original))
        framing = inherited_framing(original)
        keyframes = inherited_keyframes(original)

        lost_the_reframe = (
            framing is not None
            and framing.mode in (FramingMode.PAN, FramingMode.TRACK)
            and not keyframes
        )
        if lost_the_reframe:
            # REMAKE cannot write this: `build_video_chain` refuses a PAN or a
            # TRACK with no points, so the render fails long before the record
            # is saved. A document from an older version or edited by hand can
            # carry one, and there is no honest way to guess where the window
            # went — so the profile's fixed crop plus a record that says
            # AS_RENDERED is the answer. A centred crop the document calls a pan
            # is the one thing that cannot be true.
            framing, keyframes = None, []

        staged = self._workspace.tmp_dir / f"nocaptions-{uuid.uuid4().hex}.mp4"
        try:
            graph = build_video_chain(
                media=media,
                profile=profile,
                framing=framing,
                keyframes=keyframes,
                # The whole point of this branch. Everything else in the graph is
                # what the parent's picture was built from.
                subtitles_expr=None,
                obscure=self._obscured_as_before(original, source),
            )
        except FramingError as exc:
            # Never retryable: a graph that will not build is a property of the
            # record and of the source's dimensions, and says the same no twice
            # more.
            raise MusicStageError(
                f"the clip could not be cut again without captions: {exc}"
            ) from exc

        render_clip(
            RenderRequest(
                source=Path(source.local_path),
                destination=staged,
                start_sec=window[0],
                end_sec=window[1],
                profile=profile,
                # Resolved into `video_filter` above: the framing, the hidden
                # rectangles and the absent captions have to compose into one
                # graph, which is why they are built together rather than handed
                # over as parts.
                subtitles=None,
                encoder=self._settings.video_encoder,
                video_filter=graph,
            ),
            media,
            ffmpeg_bin=self._settings.ffmpeg_bin,
        )

        previous = original.remake
        if previous is None or not lost_the_reframe:
            return staged, previous
        return staged, _restated(
            previous,
            warning=(
                "this clip's reframe recorded no window positions, so removing the captions "
                "cost it — the picture is the profile's fixed crop of the same window"
            ),
            framing_mode=FramingMode.AS_RENDERED,
            keyframes=[],
        )

    def _obscured_as_before(self, original: Clip, source: Source) -> ObscureOptions | None:
        """Everything the parent's picture had hidden, from both places it lives.

        A remade parent recorded the merged result on its own `AppliedRemake`. A
        parent RENDER made recorded nothing, because RENDER hides whatever the
        *source* says is always there and does not write it down. Reading only
        the first brought a channel bug back on a job whose entire request was
        "take the captions off", which is what the production clips showed.

        Precedence is REMAKE's own, and `merge_regions` enforces it: the first
        rectangle of any pair covering the same pixels wins, so the parent's own
        boxes beat the channel's.

        The options-level `method` and `strength` a remake may have set are not
        on `AppliedRemake`, so a region that named neither falls back to the
        renderer's choose-by-size default. That is the same small loss of
        fidelity `inherited_framing` takes on FIT's fill, for the same reason:
        what was never written down cannot be reproduced.
        """
        remembered = [
            region.model_copy(update={"found": ObscureFound.REMEMBERED})
            for region in (source.obscure.regions if source.obscure is not None else None) or []
        ]
        previous = original.remake
        regions = merge_regions(list(previous.obscured or []) if previous else [], remembered)
        if not regions:
            return None
        return ObscureOptions(auto=False, regions=regions[:6])


def _restated(previous: AppliedRemake, *, warning: str, **changed: Any) -> AppliedRemake:
    """The parent's record with `changed` applied and `warning` appended.

    Re-validated rather than `model_copy`-ed. `warnings` is a list of a root
    model with a 300-character ceiling and `model_copy(update=...)` skips
    validation entirely, so a bare string put there reaches Firestore as
    something no reader of the contract expects.

    The new warning goes last and is never the one truncated away: a record
    already carrying eight warnings from the remake that made it is exactly the
    one whose ninth — what this job just cost it — matters most.
    """
    fields: dict[str, Any] = {**previous.model_dump(), **changed}
    fields["warnings"] = [*(fields.get("warnings") or [])[:7], warning[:300]]
    return AppliedRemake.model_validate(fields)
