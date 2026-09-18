"""The COMPILE job: several videos, one theme, one clip.

The first job type whose output has more than one source behind it, and
decision D10 in docs/PLAN.md says how that has to be shaped: a new job type
rather than extra CLIP stages, because a job's stage list is authoritative for
its whole life and a harvest must not carry stages it can never run.

Three stages, each reusing the single-source machinery rather than
reimplementing it:

**GATHER** (CPU) ingests every item through `DownloadStage.ingest`, one at a
time, keeping its own checkpoint of which landed. An item that cannot be
fetched is recorded and skipped — a compilation of four with one dead link
should still produce three, and say so.

**SELECT** (GPU) transcribes each source through `TranscribeStage.transcribe_source`
and then, under one broker lease, asks the model for the single best moment in
each — with the theme in the prompt, so it prefers the moment that belongs in
*this* compilation over the moment that is merely strongest. An item that
arrived with its own window skips the model entirely.

**ASSEMBLE** (CPU) renders each moment exactly as RENDER would — same crop,
same captions, same loudness, the source's own hidden rectangles — then
prepends a title card and joins the lot with `media/assemble.py`. The result is
a clip like any other: it enters the review queue, plays on the phone, takes
music, publishes. What it does not do is remake, because there is no single
source to re-cut from; the clip says so and the compile form is the way back.
"""

from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clipforge_contracts import (
    AppliedCompile,
    Candidate,
    Clip,
    ClipLocation,
    ClipPreview,
    CompileOptions,
    CompileSegment,
    CompileSkipped,
    CompileTransition,
    Lane,
    ReviewState,
    StageName,
    SubScores,
    Transcript,
)

from clipforge.analysis.prompts import THEMED_PROMPT_VERSION
from clipforge.media.assemble import (
    clamp_window,
    concat_segments,
    render_title_card,
    segment_budget_sec,
)
from clipforge.media.captions import build_ass, group_into_cues
from clipforge.media.ffprobe import ProbeError, probe
from clipforge.media.poster import PosterError, extract_poster
from clipforge.media.profiles import RenderProfile, load_profile
from clipforge.media.render import RenderError, RenderRequest, render_clip
from clipforge.media.workspace import Workspace
from clipforge.observability import get_logger
from clipforge.stages.analyze import DEFAULT_LLM_VRAM_MB, AnalyzeStage
from clipforge.stages.base import StageContext, StageOutcome
from clipforge.stages.download import DownloadStage
from clipforge.stages.transcribe import SourceNotIngestedError, TranscribeStage
from clipforge.store.blobs import BlobStore
from clipforge.store.firestore import CandidateStore, ClipStore, SourceStore
from clipforge.store.transcripts import TranscriptArchive

log = get_logger(__name__)

__all__ = ["AssembleStage", "CompileError", "GatherStage", "SelectStage"]


class CompileError(RuntimeError):
    """The compilation cannot be made. Read by the runner for retryability."""

    def __init__(self, message: str, *, retryable: bool = False, code: str = "COMPILE") -> None:
        self.retryable = retryable
        self.code = code
        super().__init__(message)


def _options(context: StageContext) -> CompileOptions:
    options = context.job.compile_options
    if options is None:
        raise CompileError(
            f"job {context.job.id} is a COMPILE job with no compileOptions", code="COMPILE_OPTIONS"
        )
    return options


def _checkpoint_of(context: StageContext, stage: StageName) -> dict[str, Any]:
    for step in context.job.stages:
        if step.name is stage and step.checkpoint:
            return dict(step.checkpoint)
    return {}


# ── GATHER ───────────────────────────────────────────────────────────────────


class GatherStage:
    """Every item into the workspace, through the ingest path CLIP uses."""

    name = StageName.GATHER
    lane = Lane.CPU

    def __init__(self, *, download: DownloadStage) -> None:
        self._download = download

    def run(self, context: StageContext) -> StageOutcome:
        options = _options(context)
        checkpoint = context.checkpoint or {}
        items: list[dict[str, Any]] = [dict(item) for item in (checkpoint.get("items") or [])]
        if len(items) != len(options.items):
            items = [
                {"submission": item.submission, "sourceId": None, "error": None}
                for item in options.items
            ]

        for index, item in enumerate(items, start=1):
            if item.get("sourceId") or item.get("error"):
                continue
            if context.stopping():
                return StageOutcome(incomplete=True, checkpoint={"items": items})
            context.progress(f"Fetching video {index} of {len(items)}")
            try:
                outcome = self._download.ingest(context, str(item["submission"]))
            except Exception as exc:
                # A rate limit is worth the whole stage retrying; everything
                # else is a property of that one link.
                if getattr(exc, "retryable", False):
                    raise
                item["error"] = str(exc)[:300]
                log.warning(
                    "gather.item_failed", submission=item["submission"], error=item["error"]
                )
                continue
            source_id = outcome.metadata.get("sourceId") or (outcome.checkpoint or {}).get(
                "sourceId"
            )
            item["sourceId"] = str(source_id) if source_id else None
            if item["sourceId"] is None:
                item["error"] = "ingest reported success but named no source"

        fetched = [item for item in items if item.get("sourceId")]
        if len(fetched) < 2:
            raise CompileError(
                f"only {len(fetched)} of {len(items)} videos could be fetched, and a "
                "compilation needs at least two: "
                + "; ".join(str(item["error"]) for item in items if item.get("error")),
                code="COMPILE_TOO_FEW",
            )
        failed = len(items) - len(fetched)
        return StageOutcome(
            checkpoint={"items": items},
            detail=f"fetched {len(fetched)} of {len(items)} videos"
            + (f", {failed} could not be" if failed else ""),
        )


# ── SELECT ───────────────────────────────────────────────────────────────────


class SelectStage:
    """One moment per source, chosen for the theme."""

    name = StageName.SELECT
    lane = Lane.GPU

    def __init__(
        self,
        *,
        transcribe: TranscribeStage,
        analyze: AnalyzeStage,
        archive: TranscriptArchive,
        candidates: CandidateStore,
        sources: SourceStore,
    ) -> None:
        self._transcribe = transcribe
        self._analyze = analyze
        self._archive = archive
        self._candidates = candidates
        self._sources = sources

    def run(self, context: StageContext) -> StageOutcome:
        options = _options(context)
        gathered = _checkpoint_of(context, StageName.GATHER).get("items") or []
        wanted: list[tuple[int, dict[str, Any]]] = [
            (index, item) for index, item in enumerate(gathered) if item.get("sourceId")
        ]
        if len(wanted) < 2:
            raise CompileError(
                "GATHER left fewer than two videos to choose from", code="COMPILE_TOO_FEW"
            )

        budget = segment_budget_sec(
            len(wanted),
            target_sec=options.target_duration_sec or 60,
            max_segment_sec=options.max_segment_sec or 20,
        )
        checkpoint = context.checkpoint or {}
        segments: dict[str, dict[str, Any]] = {
            key: dict(value) for key, value in (checkpoint.get("segments") or {}).items()
        }

        # Pass one: transcripts. Whisper holds the card for each of these.
        for position, (index, item) in enumerate(wanted, start=1):
            key = str(index)
            segment = segments.get(key) or {
                "submission": item["submission"],
                "sourceId": item["sourceId"],
            }
            segments[key] = segment
            if segment.get("contentHash") or segment.get("error"):
                continue
            if context.stopping():
                return StageOutcome(incomplete=True, checkpoint={"segments": segments})
            context.progress(f"Transcribing video {position} of {len(wanted)}")
            try:
                outcome = self._transcribe.transcribe_source(context, str(item["sourceId"]))
            except SourceNotIngestedError as exc:
                segment["error"] = str(exc)[:300]
                continue
            transcribed = outcome.checkpoint or {}
            segment["contentHash"] = transcribed.get("contentHash")
            segment["modelVersion"] = transcribed.get("modelVersion")

        # Pass two: windows. The operator's own come free; the rest cost a lease.
        peak: int | None = None
        needs_model = False
        for index, _item in wanted:
            segment = segments[str(index)]
            if segment.get("error") or "startSec" in segment:
                continue
            given = options.items[index]
            if given.start_sec is not None and given.end_sec is not None:
                start, end, trimmed = clamp_window(given.start_sec, given.end_sec, max_sec=budget)
                if end <= start:
                    segment["error"] = "the window given has no length"
                    continue
                segment.update(
                    {"startSec": start, "endSec": end, "chosenBy": "operator", "trimmed": trimmed}
                )
            else:
                needs_model = True

        if needs_model:
            client = self._analyze.build_client(context)
            context.progress("Waiting for the GPU")
            with context.broker.acquire(f"ollama:{client.model}", DEFAULT_LLM_VRAM_MB) as leased:
                for position, (index, _item) in enumerate(wanted, start=1):
                    segment = segments[str(index)]
                    if segment.get("error") or "startSec" in segment:
                        continue
                    if context.stopping():
                        return StageOutcome(incomplete=True, checkpoint={"segments": segments})
                    context.progress(f"Choosing a moment from video {position} of {len(wanted)}")
                    self._choose(context, client, segment, theme=options.theme, budget=budget)
                peak = leased.observe()

        documents = self._candidates_for(context, segments)
        self._candidates.replace_for_job(context.job.id, documents)

        good = [segment for segment in segments.values() if not segment.get("error")]
        if len(good) < 2:
            raise CompileError(
                "fewer than two videos yielded a moment to cut: "
                + "; ".join(str(s["error"]) for s in segments.values() if s.get("error")),
                code="COMPILE_TOO_FEW",
            )
        chosen_by_model = sum(1 for segment in good if segment.get("chosenBy") == "model")
        return StageOutcome(
            checkpoint={
                "segments": segments,
                "budgetSec": round(budget, 2),
                "promptVersion": THEMED_PROMPT_VERSION,
            },
            peak_vram_mb=peak or None,
            detail=f"{len(good)} moments chosen, {chosen_by_model} by the model, "
            f"up to {budget:.0f}s each",
        )

    def _choose(
        self,
        context: StageContext,
        client: Any,
        segment: dict[str, Any],
        *,
        theme: str,
        budget: float,
    ) -> None:
        content_hash = str(segment.get("contentHash") or "")
        version = str(segment.get("modelVersion") or "")
        transcript = self._archive.load(content_hash, version)
        if transcript is None:
            segment["error"] = "the transcript is missing from the archive"
            return
        spans = self._archive.load_speech_spans(content_hash, version)
        windows = self._analyze.propose_windows(
            context,
            client,
            transcript,
            spans,
            limit=1,
            theme=theme,
            min_duration_sec=min(8.0, budget),
            max_duration_sec=budget,
        )
        if windows:
            best = windows[0]
            segment.update(
                {
                    "startSec": round(best.start_sec, 3),
                    "endSec": round(best.end_sec, 3),
                    "chosenBy": "model",
                    "hook": best.hook,
                    "reason": best.reason,
                    "subScores": best.sub_scores.model_dump(by_alias=True),
                    "total": best.total,
                }
            )
            return
        # Nothing stood out. The middle of the video is the honest default —
        # and it is written down as such, so the reviewer knows it was nobody's
        # idea rather than a bad one.
        duration = _duration_of(transcript) or self._source_duration(segment)
        if not duration:
            segment["error"] = "no moment stood out and the video's length is unknown"
            return
        length = min(budget, duration)
        start = max(0.0, duration / 2 - length / 2)
        segment.update(
            {
                "startSec": round(start, 3),
                "endSec": round(start + length, 3),
                "chosenBy": "fallback",
                "reason": "no moment stood out for the theme, so the middle of the video was used",
            }
        )

    def _source_duration(self, segment: dict[str, Any]) -> float | None:
        source = self._sources.get(str(segment.get("sourceId") or ""))
        return source.duration_sec if source is not None else None

    def _candidates_for(
        self, context: StageContext, segments: dict[str, dict[str, Any]]
    ) -> list[Candidate]:
        """One candidate per chosen moment, so the review screen has a window to show."""
        now = datetime.now(UTC)
        documents: list[Candidate] = []
        for key in sorted(segments, key=int):
            segment = segments[key]
            if segment.get("error") or "startSec" not in segment:
                continue
            candidate_id = str(segment.get("candidateId") or uuid.uuid4().hex)
            segment["candidateId"] = candidate_id
            scores = segment.get("subScores")
            documents.append(
                Candidate(
                    id=candidate_id,
                    uid=context.job.uid,
                    source_id=str(segment["sourceId"]),
                    job_id=context.job.id,
                    start_sec=float(segment["startSec"]),
                    end_sec=float(segment["endSec"]),
                    sub_scores=SubScores.model_validate(scores) if scores else _NEUTRAL,
                    total=int(segment.get("total") or 0),
                    hook=segment.get("hook"),
                    reason=segment.get("reason")
                    or (
                        "the window the operator gave"
                        if segment.get("chosenBy") == "operator"
                        else None
                    ),
                    model_version=None if segment.get("chosenBy") != "model" else "ollama",
                    prompt_version=THEMED_PROMPT_VERSION
                    if segment.get("chosenBy") == "model"
                    else None,
                    created_at=now,
                )
            )
        return documents


_NEUTRAL = SubScores(hook=0, curiosity=0, standalone=0, emotion=0, pacing=0, shareability=0)


def _duration_of(transcript: Transcript) -> float | None:
    if transcript.duration_sec:
        return transcript.duration_sec
    ends = [segment.end_sec for segment in transcript.segments]
    return max(ends) if ends else None


# ── ASSEMBLE ─────────────────────────────────────────────────────────────────


class AssembleStage:
    """Render every chosen moment, then join them into one clip."""

    name = StageName.ASSEMBLE
    lane = Lane.CPU

    def __init__(
        self,
        *,
        sources: SourceStore,
        clips: ClipStore,
        archive: TranscriptArchive,
        workspace: Workspace,
        blobs: BlobStore,
    ) -> None:
        self._sources = sources
        self._clips = clips
        self._archive = archive
        self._workspace = workspace
        self._blobs = blobs

    def run(self, context: StageContext) -> StageOutcome:
        options = _options(context)
        settings = context.settings
        selected = _checkpoint_of(context, StageName.SELECT).get("segments") or {}
        ordered = [
            selected[key]
            for key in sorted(selected, key=int)
            if not selected[key].get("error") and "startSec" in selected[key]
        ]
        if not ordered:
            raise CompileError("SELECT chose nothing to assemble", code="COMPILE_TOO_FEW")

        profile = load_profile(settings.render_profile)
        transition = options.transition or CompileTransition.FADE
        clip_id = uuid.uuid4().hex
        scratch = self._workspace.tmp_dir / f"compile-{clip_id}"
        scratch.mkdir(parents=True, exist_ok=True)

        try:
            parts: list[tuple[Path, float]] = []
            applied: list[CompileSegment] = []
            skipped: list[CompileSkipped] = []
            warnings: list[str] = []

            if options.title_card is not False:
                context.progress("Rendering the title card")
                card = render_title_card(
                    options.title or options.theme,
                    scratch / "card.mp4",
                    profile=profile,
                    work_dir=scratch,
                    encoder=settings.video_encoder,
                    ffmpeg_bin=settings.ffmpeg_bin,
                )
                parts.append((card.path, card.duration_sec))

            for position, segment in enumerate(ordered, start=1):
                if context.stopping():
                    return StageOutcome(incomplete=True)
                context.progress(f"Rendering segment {position} of {len(ordered)}")
                try:
                    rendered = self._render_segment(
                        context, segment, scratch / f"segment-{position}.mp4", profile, options
                    )
                except (RenderError, ProbeError, PosterError, CompileError) as exc:
                    skipped.append(
                        CompileSkipped(submission=str(segment["submission"]), reason=str(exc)[:300])
                    )
                    log.warning("assemble.segment_failed", position=position, error=str(exc)[:200])
                    continue
                path, duration, described = rendered
                parts.append((path, duration))
                applied.append(described)
                if segment.get("trimmed"):
                    warnings.append(
                        f"segment {position} was trimmed to {duration:.0f}s to fit the budget"
                    )

            if not applied:
                raise CompileError(
                    "no segment could be rendered: "
                    + "; ".join(f"{s.submission}: {s.reason}" for s in skipped),
                    code="COMPILE_RENDER",
                )
            if len(applied) == 1:
                warnings.append(
                    "only one segment could be rendered, so this is a clip rather than a compilation"
                )

            context.progress("Joining the segments")
            assembled = concat_segments(
                [path for path, _ in parts],
                scratch / "clip.mp4",
                durations=[duration for _, duration in parts],
                transition=transition,
                profile=profile,
                encoder=settings.video_encoder,
                ffmpeg_bin=settings.ffmpeg_bin,
            )
            images = extract_poster(
                assembled.path,
                duration_sec=assembled.duration_sec,
                work_dir=scratch,
                ffmpeg_bin=settings.ffmpeg_bin,
            )
            context.progress("Uploading the compilation")
            ref = self._blobs.put(
                f"clips/{context.job.uid}/{clip_id}.mp4", assembled.path, content_type="video/mp4"
            )

            now = datetime.now(UTC)
            title = (options.title or options.theme).strip()[:200]
            clip = Clip(
                id=clip_id,
                uid=context.job.uid,
                # A compilation has no single window. The first segment's stands
                # in so the queue has a score to show; the truth is in `compile`.
                candidate_id=applied[0].candidate_id or f"compile-{context.job.id}",
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
                duration_sec=round(assembled.duration_sec, 3),
                width_px=assembled.width,
                height_px=assembled.height,
                size_bytes=ref.size_bytes,
                render_profile=profile.identifier,
                title=title,
                description=_describe(options.theme, applied)[:2000],
                tags=_tags(options.theme),
                review=ReviewState.PENDING,
                compile=AppliedCompile(
                    theme=options.theme[:200],
                    segments=applied,
                    skipped=skipped[:12],
                    title_card=options.title_card is not False,
                    transition=transition,
                    warnings=warnings[:8],
                    trend_id=options.trend_id,
                ),
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
            return StageOutcome(
                checkpoint={
                    "clipId": clip_id,
                    "segments": len(applied),
                    "skipped": [s.model_dump(by_alias=True) for s in skipped],
                    "durationSec": round(assembled.duration_sec, 2),
                },
                detail=f"assembled {len(applied)} segments into a {assembled.duration_sec:.0f}s clip"
                + (f", {len(skipped)} skipped" if skipped else ""),
            )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _render_segment(
        self,
        context: StageContext,
        segment: dict[str, Any],
        destination: Path,
        profile: RenderProfile,
        options: CompileOptions,
    ) -> tuple[Path, float, CompileSegment]:
        settings = context.settings
        source = self._sources.get(str(segment["sourceId"]))
        if source is None or not source.local_path:
            raise CompileError("the source is no longer recorded")
        media_path = Path(source.local_path)
        if not media_path.is_file():
            raise CompileError("the source media has been collected from the workspace")
        media = probe(media_path, ffprobe_bin=settings.ffprobe_bin)
        start = float(segment["startSec"])
        end = float(segment["endSec"])

        subtitles: Path | None = None
        if options.captions is not False and segment.get("contentHash"):
            transcript = self._archive.load(
                str(segment["contentHash"]), str(segment.get("modelVersion") or "")
            )
            if transcript is not None:
                words = [w for s in transcript.segments for w in (s.words or [])]
                cues = group_into_cues(
                    words, style=profile.captions, clip_start_sec=start, clip_end_sec=end
                )
                if cues:
                    subtitles = destination.with_suffix(".ass")
                    subtitles.write_text(
                        build_ass(cues, style=profile.captions), encoding="utf-8", newline="\n"
                    )

        result = render_clip(
            RenderRequest(
                source=media_path,
                destination=destination,
                start_sec=start,
                end_sec=end,
                profile=profile,
                subtitles=subtitles,
                encoder=settings.video_encoder,
                obscure=source.obscure,
            ),
            media,
            ffmpeg_bin=settings.ffmpeg_bin,
        )
        self._sources.touch(source.id)
        described = CompileSegment(
            source_id=source.id,
            candidate_id=segment.get("candidateId"),
            submission=str(segment["submission"]),
            url=source.url,
            title=source.title,
            channel=source.channel,
            start_sec=round(start, 3),
            end_sec=round(end, 3),
            duration_sec=round(result.duration_sec, 3),
            chosen_by=segment.get("chosenBy"),
        )
        return result.path, result.duration_sec, described


def _describe(theme: str, segments: list[CompileSegment]) -> str:
    lines = [f"{theme.strip()}", "", f"Compiled from {len(segments)} videos:"]
    for segment in segments:
        name = segment.title or segment.submission
        lines.append(f"• {name}" + (f" — {segment.channel}" if segment.channel else ""))
    return "\n".join(lines)


def _tags(theme: str) -> list[str]:
    from clipforge.research.scoring import topic_tokens

    return sorted(token[:40] for token in topic_tokens(theme))[:15]
