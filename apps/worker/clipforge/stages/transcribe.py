"""The TRANSCRIBE stage: audio in, word-level transcript out, GPU released.

Cached on ``(contentHash, modelVersion)`` rather than on the source id. The same
video re-submitted under a different URL is a different source with identical
bytes, and re-transcribing it costs real GPU minutes for a byte-identical
result. A cache hit reports **SKIPPED** rather than DONE, so "this was already
transcribed" stays distinguishable from "we transcribed it" in the job document.

Audio extraction is a separate artefact rather than a pipe into Whisper: it is
small, it makes a crash-resume free instead of re-decoding a 2 GB source, and it
is the first thing worth listening to when a transcript looks wrong.
"""

from __future__ import annotations

from pathlib import Path

from clipforge_contracts import Lane, StageName

from clipforge.media.audio import extract_audio
from clipforge.media.workspace import Workspace
from clipforge.models.whisper import WhisperTranscriber, model_version
from clipforge.observability import get_logger
from clipforge.stages.base import StageContext, StageOutcome
from clipforge.store.firestore import SourceStore
from clipforge.store.transcripts import TranscriptArchive, TranscriptStore

log = get_logger(__name__)

__all__ = ["SourceNotIngestedError", "TranscribeStage"]


class SourceNotIngestedError(RuntimeError):
    """TRANSCRIBE ran without DOWNLOAD having produced a source."""


class TranscribeStage:
    """Produce a word-level transcript for the job's source."""

    name = StageName.TRANSCRIBE
    # The GPU lane. Whisper takes the broker's exclusive lease, which is what
    # stops it being co-resident with the analysis LLM (docs/PLAN.md §2.1).
    lane = Lane.GPU

    def __init__(
        self,
        *,
        sources: SourceStore,
        transcripts: TranscriptStore,
        archive: TranscriptArchive,
        workspace: Workspace,
        transcriber_factory: object = None,
    ) -> None:
        self._sources = sources
        self._transcripts = transcripts
        self._archive = archive
        self._workspace = workspace
        # Injected so the offline tier can substitute a transcriber that needs no
        # GPU, no model download and no CUDA.
        self._transcriber_factory = transcriber_factory

    def run(self, context: StageContext) -> StageOutcome:
        source_id = self._source_id(context)
        source = self._sources.get(source_id)
        if source is None:
            raise SourceNotIngestedError(f"source {source_id} does not exist")
        if not source.content_hash or not source.local_path:
            raise SourceNotIngestedError(f"source {source_id} has no ingested media")

        settings = context.settings
        version = model_version(settings.whisper_model, settings.whisper_compute_type)

        cached = self._archive.load(source.content_hash, version)
        if cached is not None:
            return self._reuse(context, source_id, source.content_hash, version, cached)

        media_path = Path(source.local_path)
        if not media_path.is_file():
            raise SourceNotIngestedError(
                f"source {source_id} was garbage-collected; re-run the job to re-ingest it"
            )

        audio_path = self._workspace.tmp_dir / f"{source.content_hash}.wav"
        # Decoding a two-gigabyte source is minutes of ffmpeg on its own, before
        # the model has been asked for anything.
        context.progress("Extracting the audio track")
        extract_audio(media_path, audio_path, ffmpeg_bin=settings.ffmpeg_bin)

        # The stage the whole checkpoint design exists for: twenty minutes with
        # nothing to show until it is over. Whisper yields segments lazily and
        # only inside the broker lease, so there is no count to report from out
        # here — the length of the audio is the honest thing to say instead.
        minutes = round((source.duration_sec or 0.0) / 60)
        context.progress(
            f"Transcribing {minutes} minutes of audio" if minutes else "Transcribing the audio"
        )
        try:
            transcriber = self._build_transcriber(context)
            result = transcriber.transcribe(audio_path, source_id=source_id)
        finally:
            # The WAV is reconstructible from the source and can be hundreds of
            # megabytes; the transcript is the artefact worth keeping.
            audio_path.unlink(missing_ok=True)

        local_path, vad_path = self._archive.save(
            result.transcript, result.speech_spans, content_hash=source.content_hash
        )
        self._transcripts.save(
            TranscriptStore.describe(
                transcript=result.transcript,
                source_id=source_id,
                local_path=local_path,
                vad_path=vad_path,
            )
        )
        self._sources.touch(source_id)

        words = sum(len(s.words or []) for s in result.transcript.segments)
        return StageOutcome(
            checkpoint={
                "sourceId": source_id,
                "contentHash": source.content_hash,
                "modelVersion": version,
                "transcriptPath": str(local_path),
                "vadPath": str(vad_path),
                # Recorded so a later model swap is comparable against measured
                # throughput rather than a remembered impression.
                "realtimeFactor": round(result.realtime_factor, 3),
                "wordCount": words,
            },
            peak_vram_mb=result.peak_vram_mb or None,
            detail=(
                f"{words} words in {len(result.transcript.segments)} segments "
                f"({result.realtime_factor:.1f}x realtime)"
            ),
        )

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _reuse(
        self,
        context: StageContext,
        source_id: str,
        content_hash: str,
        version: str,
        cached: object,
    ) -> StageOutcome:
        del context
        local_path, vad_path = self._archive.paths_for(content_hash, version)
        self._sources.touch(source_id)
        log.info("transcribe.cache_hit", source_id=source_id, model_version=version)
        return StageOutcome(
            skipped=True,
            checkpoint={
                "sourceId": source_id,
                "contentHash": content_hash,
                "modelVersion": version,
                "transcriptPath": str(local_path),
                "vadPath": str(vad_path),
                "cacheHit": True,
            },
            detail=f"already transcribed with {version}",
        )

    def _source_id(self, context: StageContext) -> str:
        """The source id the DOWNLOAD stage recorded in its checkpoint.

        Read from the job's own stages rather than from `job.sourceId`, because a
        stage must be able to resume from what the pipeline actually produced,
        not from a field another writer may not have set yet.
        """
        for stage in context.job.stages:
            if stage.name is StageName.DOWNLOAD and stage.checkpoint:
                source_id = stage.checkpoint.get("sourceId")
                if source_id:
                    return str(source_id)
        if context.job.source_id:
            return context.job.source_id
        raise SourceNotIngestedError(
            f"job {context.job.id} reached TRANSCRIBE with no ingested source"
        )

    def _build_transcriber(self, context: StageContext) -> WhisperTranscriber:
        if self._transcriber_factory is not None:
            return self._transcriber_factory(context)  # type: ignore[operator, no-any-return]
        settings = context.settings
        return WhisperTranscriber(
            model=settings.whisper_model,
            compute_type=settings.whisper_compute_type,
            device=settings.whisper_device,
            broker=context.broker,
        )
