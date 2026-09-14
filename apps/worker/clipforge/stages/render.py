"""The RENDER stage: candidates become finished, reviewable clips.

On the free tier this is where the pipeline's output stops moving. There is no
Cloud Storage bucket, so the clip is written to the workspace through the
`BlobStore` port and stays there; what travels to Firestore is the `Clip`
document and a small base64 poster. Enabling Blaze later means adding one adapter
and a `playbackUrl` appears — nothing here changes.
See docs/adr/0009-spark-tier-local-artefacts.md.

The stage renders each candidate independently and **tolerates individual
failures**. A clip that will not encode should not discard the four that would:
the job completes with what it produced, and the failure is recorded on the
checkpoint. That is a deliberate departure from the usual all-or-nothing stage
contract, and it is the right trade at the end of a pipeline that has already
spent GPU minutes.
"""

from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

from clipforge_contracts import (
    Candidate,
    Clip,
    ClipLocation,
    ClipPreview,
    Lane,
    ObscureOptions,
    ReviewState,
    StageName,
    Transcript,
)

from clipforge.analysis.metadata import ClipMetadata, write_metadata
from clipforge.media.captions import build_ass, group_into_cues
from clipforge.media.ffprobe import MediaInfo, probe
from clipforge.media.poster import PosterError, extract_poster
from clipforge.media.profiles import RenderProfile, load_profile
from clipforge.media.render import RenderError, RenderRequest, render_clip
from clipforge.media.workspace import Workspace
from clipforge.models.ollama import OllamaClient
from clipforge.observability import get_logger
from clipforge.stages.base import StageContext, StageOutcome
from clipforge.store.blobs import BlobStore
from clipforge.store.firestore import CandidateStore, ClipStore, SourceStore
from clipforge.store.transcripts import TranscriptArchive

log = get_logger(__name__)


# The same lease the note reader takes. Naming a clip is a small call to the
# same model, so it queues behind whatever else wants the GPU rather than
# racing it — and on a harvest it runs once per clip.
_METADATA_VRAM_MB = 3600


def _excerpt_for(transcript: Transcript | None, start_sec: float, end_sec: float) -> str:
    """What the clip says, as the recogniser heard it."""
    if transcript is None:
        return ""
    words = [
        word.text
        for segment in transcript.segments
        for word in (segment.words or [])
        if word.end_sec > start_sec and word.start_sec < end_sec
    ]
    return " ".join(word.strip() for word in words if word.strip())[:1200]


__all__ = ["NothingToRenderError", "RenderStage"]


class NothingToRenderError(RuntimeError):
    """RENDER ran with no candidates and no source."""


class RenderStage:
    """Render each candidate to a vertical, captioned, loudness-normalised clip."""

    name = StageName.RENDER
    # The CPU lane. NVENC is used, but it is a fixed-function encoder block that
    # does not contend for the VRAM the models need — so the render of one job
    # can proceed while another transcribes, which is the entire point of the
    # two-lane scheduler.
    lane = Lane.CPU

    def __init__(
        self,
        *,
        sources: SourceStore,
        candidates: CandidateStore,
        clips: ClipStore,
        archive: TranscriptArchive,
        workspace: Workspace,
        blobs: BlobStore,
    ) -> None:
        self._sources = sources
        self._candidates = candidates
        self._clips = clips
        self._archive = archive
        self._workspace = workspace
        self._blobs = blobs

    def run(self, context: StageContext) -> StageOutcome:
        settings = context.settings
        proposals = self._candidates.for_job(context.job.id)
        if not proposals:
            # Not a failure. A video with nothing worth clipping is a legitimate
            # and expected outcome — the prompt explicitly says so.
            log.info("render.nothing_to_do", job_id=context.job.id)
            return StageOutcome(
                checkpoint={"clipIds": [], "rendered": 0},
                detail="no candidates were selected for this source",
            )

        source = self._sources.get(proposals[0].source_id)
        if source is None or not source.local_path:
            raise NothingToRenderError(
                f"source {proposals[0].source_id} is missing; re-run the job to re-ingest it"
            )

        media_path = self._resolve_media(source.local_path)
        media = probe(media_path, ffprobe_bin=settings.ffprobe_bin)
        profile = load_profile(settings.render_profile)
        transcript = self._transcript(context)

        rendered: list[Clip] = []
        failures: list[dict[str, str]] = []

        for candidate in sorted(proposals, key=lambda c: -c.total):
            if context.stopping():
                # Checkpoint what exists rather than discarding it; the next
                # attempt renders the remainder.
                log.info("render.stopping", rendered=len(rendered))
                return self._checkpoint(rendered, failures, incomplete=True)
            try:
                rendered.append(
                    self._render_one(
                        context, candidate, media_path, media, profile, transcript, source.obscure
                    )
                )
            except (RenderError, PosterError) as exc:
                # One clip that will not encode must not discard the others.
                log.warning("render.candidate_failed", candidate=candidate.id, error=str(exc))
                failures.append({"candidateId": candidate.id, "error": str(exc)[:300]})

        if not rendered and failures:
            raise RenderError(
                f"every candidate failed to render ({len(failures)} of them); "
                f"first error: {failures[0]['error']}"
            )

        self._sources.touch(source.id)
        return self._checkpoint(rendered, failures, incomplete=False)

    # ── One clip ─────────────────────────────────────────────────────────────

    def _render_one(
        self,
        context: StageContext,
        candidate: Candidate,
        media_path: Path,
        media: MediaInfo,
        profile: RenderProfile,
        transcript: Transcript | None,
        obscure: ObscureOptions | None = None,
    ) -> Clip:
        """One candidate, cut and encoded.

        `obscure` is the source's own standing list of things to hide, and it
        arrives here rather than being looked up per clip because it is a
        property of the channel: every clip in this job is cut from the same
        video and needs the same rectangles gone. Hiding them at this point is
        the difference between a feature and a chore — a reviewer who has
        already said "this channel puts its bug in the top right" should never
        see the bug again, on this clip or any future one.
        """
        settings = context.settings
        clip_id = uuid.uuid4().hex
        scratch = self._workspace.tmp_dir / clip_id
        scratch.mkdir(parents=True, exist_ok=True)

        try:
            subtitles = self._write_subtitles(scratch, candidate, profile, transcript)
            staged = scratch / "clip.mp4"
            result = render_clip(
                RenderRequest(
                    source=media_path,
                    destination=staged,
                    start_sec=candidate.start_sec,
                    end_sec=candidate.end_sec,
                    profile=profile,
                    subtitles=subtitles,
                    encoder=settings.video_encoder,
                    obscure=obscure,
                ),
                media,
                ffmpeg_bin=settings.ffmpeg_bin,
            )

            written = self._name(context, candidate, transcript, result.duration_sec)

            images = extract_poster(
                staged,
                duration_sec=result.duration_sec,
                work_dir=scratch,
                ffmpeg_bin=settings.ffmpeg_bin,
            )

            # Through the port, so the Blaze adapter is the only thing that ever
            # needs to change here.
            ref = self._blobs.put(
                f"clips/{context.job.uid}/{clip_id}.mp4", staged, content_type="video/mp4"
            )

            now = datetime.now(UTC)
            clip = Clip(
                id=clip_id,
                uid=context.job.uid,
                candidate_id=candidate.id,
                source_id=candidate.source_id,
                job_id=context.job.id,
                # The root of its own lineage. Every correction of this clip
                # inherits the id, so they stay one row in the review queue.
                lineage_id=clip_id,
                version=1,
                # REMOTE means "reachable from somewhere that is not this
                # machine" — a bucket object, or a URL from some other store.
                # The worker's own file server does not count: it answers on
                # 127.0.0.1, which is the phone when a phone asks.
                location=(
                    ClipLocation.REMOTE
                    if (ref.storage_path or ref.playback_url)
                    else ClipLocation.LOCAL
                ),
                local_path=str(ref.local_path),
                playback_url=ref.playback_url,
                storage_path=ref.storage_path,
                playback_expires_at=ref.expires_at,
                duration_sec=round(result.duration_sec, 3),
                width_px=result.width,
                height_px=result.height,
                size_bytes=ref.size_bytes,
                render_profile=profile.identifier,
                # `hook` is a line quoted out of the transcript and `reason` is
                # one sentence on why this window was SELECTED. Neither was ever
                # written to be read, and both were going out to viewers: a real
                # clip reached the queue titled "on a franchi un premier rideau
                # kane peut enroule du plat" and described as "This segment
                # captures the high-tension moment...". They stay as the
                # fallback, because a clip with a rough title is worth far more
                # than a clip that failed over a language model — and they are
                # still exactly right for the review queue, which is the one
                # audience they were written for.
                title=(written.title if written else candidate.hook),
                description=(written.description if written else candidate.reason),
                tags=list(written.tags) if written else [],
                review=ReviewState.PENDING,
                rights=None,
                created_at=now,
            )
            # A preview is worth a thumbnail in the review queue; it is not
            # worth a clip. `ClipPreview` requires positive dimensions, and a
            # poster whose size could not be read would fail that validation and
            # discard a render that had already succeeded — so an unusable one
            # is dropped here rather than raised. `_dimensions` explains how a
            # poster ends up unmeasurable.
            usable = images.width_px > 0 and images.height_px > 0
            if not usable:
                log.warning("render.preview_unusable", clip_id=clip_id, detail="no poster saved")
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
            return clip
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _name(
        self,
        context: StageContext,
        candidate: Candidate,
        transcript: Transcript | None,
        duration_sec: float,
    ) -> ClipMetadata | None:
        """A title, a description and tags, written to be read.

        Never looks at the picture, unlike REMAKE. One harvest produces a dozen
        clips and a vision pass costs about a minute each; the transcript
        excerpt and the source's own title are enough for a first name, and the
        reviewer who corrects a clip gets the grounded version then.

        Returns None on any failure, and the caller falls back to what it had.
        Losing a good title must never lose a finished render.
        """
        settings = context.settings
        client = OllamaClient(
            host=settings.ollama_host,
            model=settings.ollama_model,
            num_ctx=settings.ollama_num_ctx,
        )
        if not client.is_available():
            return None

        source = self._sources.get(candidate.source_id) if candidate.source_id else None
        spoken = _excerpt_for(transcript, candidate.start_sec, candidate.end_sec)
        try:
            with context.broker.acquire(f"ollama:{client.model}", _METADATA_VRAM_MB):
                return write_metadata(
                    client,
                    # The source's own language, because nothing has changed it
                    # yet. A clip is named in the language it speaks, and REMAKE
                    # renames it when the reviewer changes that.
                    language=(transcript.language if transcript else None) or "en",
                    spoken_text=spoken,
                    duration_sec=duration_sec,
                    visual=None,
                    source_title=source.title if source is not None else None,
                )
        except Exception as exc:  # noqa: BLE001 - a title is never worth a clip
            log.info("render.metadata_skipped", error=str(exc))
            return None

    def _write_subtitles(
        self,
        scratch: Path,
        candidate: Candidate,
        profile: RenderProfile,
        transcript: Transcript | None,
    ) -> Path | None:
        """Write the ASS subtitle file for one clip, or None if there is nothing
        to caption."""
        if transcript is None:
            return None
        words = [word for segment in transcript.segments for word in (segment.words or [])]
        cues = group_into_cues(
            words,
            style=profile.captions,
            clip_start_sec=candidate.start_sec,
            clip_end_sec=candidate.end_sec,
        )
        if not cues:
            return None
        path = scratch / "captions.ass"
        path.write_text(build_ass(cues, style=profile.captions), encoding="utf-8", newline="\n")
        return path

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _resolve_media(self, local_path: str) -> Path:
        path = Path(local_path)
        if not path.is_file():
            raise NothingToRenderError(
                f"source media at {path} is gone; re-run the job to re-ingest it"
            )
        return path

    def _transcript(self, context: StageContext) -> Transcript | None:
        for stage in context.job.stages:
            if stage.name is StageName.TRANSCRIBE and stage.checkpoint:
                return self._archive.load(
                    str(stage.checkpoint.get("contentHash", "")),
                    str(stage.checkpoint.get("modelVersion", "")),
                )
        return None

    def _checkpoint(
        self, rendered: list[Clip], failures: list[dict[str, str]], *, incomplete: bool
    ) -> StageOutcome:
        return StageOutcome(
            incomplete=incomplete,
            checkpoint={
                "clipIds": [c.id for c in rendered],
                "rendered": len(rendered),
                "failed": failures,
            },
            detail=(
                f"rendered {len(rendered)} clip(s)"
                + (f", {len(failures)} failed" if failures else "")
            ),
        )
