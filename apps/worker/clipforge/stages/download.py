"""The DOWNLOAD stage: a submission becomes media on disk, exactly once.

The ordering here is the whole design, and it is ordered by what each step costs:

1. **Identify** — no network. A malformed URL fails in microseconds.
2. **Dedupe on identity** — one Firestore read. Re-submitting a known video must
   not re-fetch two gigabytes.
3. **Fetch metadata** — one cheap network call. Duration and liveness are checked
   *here*, so a four-hour video is refused before it is downloaded rather than
   after.
4. **Make room** — the workspace GC runs against the known download size, so a
   2 GB fetch does not fail at 1.9 GB.
5. **Download** — the expensive step, reached only once everything cheaper has
   agreed it is worth doing.
6. **Dedupe on content hash** — catches the same video under two URLs, which can
   only be known once the bytes exist.

The stage is idempotent, as the stage contract requires: an existing source with
its file still present short-circuits at step 2, and a re-run after a crash
re-downloads into the same deterministic path.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clipforge_contracts import (
    IngestErrorCode,
    Lane,
    Source,
    SourceKind,
    SourceProvider,
    StageName,
)

from clipforge.media.sources import (
    FetchedSource,
    IngestError,
    LocalFileAdapter,
    SourceAdapter,
    SourceIdentity,
    select_adapter,
)
from clipforge.media.thumbnails import build_source_preview
from clipforge.media.workspace import EvictionCandidate, Workspace
from clipforge.observability import get_logger
from clipforge.stages.base import StageContext, StageOutcome
from clipforge.store.firestore import SourceStore

log = get_logger(__name__)

__all__ = ["DownloadStage", "SubmissionMissingError"]


class SubmissionMissingError(ValueError):
    """A CLIP job arrived with nothing to ingest."""


class DownloadStage:
    """Ingest a submission into the workspace and record it in Firestore."""

    name = StageName.DOWNLOAD
    lane = Lane.CPU

    def __init__(
        self,
        *,
        sources: SourceStore,
        workspace: Workspace,
        adapter_factory: Any = None,
    ) -> None:
        self._sources = sources
        self._workspace = workspace
        # Injected so the offline tier can force LocalFileAdapter and never risk
        # a test reaching YouTube.
        self._adapter_factory = adapter_factory or select_adapter

    def run(self, context: StageContext) -> StageOutcome:
        submission = self._submission(context)
        settings = context.settings

        adapter: SourceAdapter = self._adapter_factory(
            submission,
            max_duration_sec=settings.max_source_duration_sec,
            ffprobe_bin=settings.ffprobe_bin,
        )

        try:
            return self._ingest(context, adapter, submission)
        except IngestError as exc:
            # Re-raised with the classification attached, so the runner records a
            # code the PWA can turn into a sentence rather than a stack trace.
            raise _as_stage_failure(exc) from exc

    # ── The pipeline above, step by step ─────────────────────────────────────

    def _ingest(
        self, context: StageContext, adapter: SourceAdapter, submission: str
    ) -> StageOutcome:
        identity = adapter.identify(submission)

        existing = self._sources.find_by_external_id(
            provider=identity.provider, external_id=identity.external_id
        )
        if existing is not None and _file_present(existing):
            self._sources.touch(existing.id)
            log.info("ingest.deduped", source_id=existing.id, reason="known identity")
            return StageOutcome(
                checkpoint={"sourceId": existing.id, "deduped": True},
                detail=f"already ingested as {existing.id}",
                metadata={"sourceId": existing.id},
            )

        # Narration starts here. Everything above is local: a parse and one
        # Firestore read. From here on the stage is waiting on somebody else's
        # network, and this is the stage that takes twenty minutes.
        context.progress("Looking up the video")
        metadata = adapter.fetch_metadata(identity)
        self._reject_if_too_long(context, metadata.duration_sec)
        self._make_room(context, metadata.estimated_bytes)

        # Cut before it is interpolated: a title is whatever the platform says it
        # is, sometimes a paragraph of one, and the note is capped at 120
        # characters — a long title would push the word "Downloading" out of it.
        title = (metadata.title or identity.external_id)[:80]
        context.progress(f"Downloading {title}")
        fetched = adapter.fetch(identity, self._workspace.sources_dir)

        duplicate = self._sources.find_by_content_hash(content_hash=fetched.content_hash)
        # The same video under a different URL. Keep the original and discard what
        # we just fetched — unless what we fetched *is* the original's file, which
        # is the normal case for a local source submitted twice by two paths.
        if (
            duplicate is not None
            and duplicate.id != (existing.id if existing else None)
            and _file_present(duplicate)
            and Path(duplicate.local_path or "") != fetched.path
        ):
            _discard(fetched, adapter)
            self._sources.touch(duplicate.id)
            log.info("ingest.deduped", source_id=duplicate.id, reason="identical content")
            return StageOutcome(
                checkpoint={"sourceId": duplicate.id, "deduped": True},
                detail=f"identical to existing source {duplicate.id}",
                metadata={"sourceId": duplicate.id},
            )

        source = self._record(
            context, identity, fetched, existing_id=existing.id if existing else None
        )
        return StageOutcome(
            checkpoint={"sourceId": source.id, "contentHash": source.content_hash},
            detail=f"ingested {source.title or source.id} ({fetched.media.duration_sec:.0f}s)",
            metadata={"sourceId": source.id},
        )

    def _submission(self, context: StageContext) -> str:
        checkpoint = context.checkpoint or {}
        submission = checkpoint.get("submission") or context.job.submission
        if not submission:
            raise SubmissionMissingError(f"job {context.job.id} has no submission to ingest")
        return str(submission)

    def _reject_if_too_long(self, context: StageContext, duration_sec: float | None) -> None:
        limit = context.settings.max_source_duration_sec
        if duration_sec is not None and duration_sec > limit:
            raise IngestError(
                IngestErrorCode.TOO_LONG,
                f"This video is {duration_sec / 60:.0f} minutes long; the limit is "
                f"{limit / 60:.0f}. Raise CLIPFORGE_MAX_SOURCE_DURATION_SEC to allow it.",
            )

    def _make_room(self, context: StageContext, estimated_bytes: int | None) -> None:
        """Reclaim space *before* the download, not after it fails.

        Only downloaded sources are eligible. A local-file source is not ours to
        delete — the workspace never owned that file.
        """
        del context
        headroom = self._workspace.over_budget_by()
        if estimated_bytes:
            projected = self._workspace.used_bytes() + estimated_bytes
            headroom = max(headroom, projected - self._workspace.max_bytes)
        if headroom <= 0:
            return

        candidates = [
            EvictionCandidate(
                source_id=source.id,
                path=Path(source.local_path),
                size_bytes=source.size_bytes or 0,
                last_accessed_at=source.last_accessed_at or source.created_at,
                # Pinned by hand, or pinned by being worth coming back to. A
                # source used more than once is one the reviewer returned to,
                # and they will return again — while re-fetching it risks the
                # one thing this project cannot buy back, a video that has since
                # been taken down. A single-use source stays collectable, so the
                # budget keeps its release valve.
                pinned=bool(source.pinned) or (source.use_count or 0) > 1,
            )
            for source in self._sources.eviction_candidates()
            if source.provider is not SourceProvider.LOCAL and source.local_path
        ]

        outcome = self._workspace.collect(candidates, need_bytes=headroom)
        if not outcome.target_met:
            raise IngestError(
                IngestErrorCode.DISK_FULL,
                f"Not enough workspace to download this video. Needed "
                f"{headroom // (1024 * 1024)} MB, freed {outcome.freed_bytes // (1024 * 1024)} MB. "
                f"{len(outcome.skipped_pinned)} source(s) are pinned.",
            )

    def _record(
        self,
        context: StageContext,
        identity: SourceIdentity,
        fetched: FetchedSource,
        *,
        existing_id: str | None,
    ) -> Source:
        now = datetime.now(UTC)
        source = Source(
            id=existing_id or uuid.uuid4().hex,
            uid=context.job.uid,
            provider=identity.provider,
            external_id=identity.external_id,
            url=identity.canonical_url,
            title=fetched.metadata.title,
            channel=fetched.metadata.channel,
            duration_sec=fetched.media.duration_sec,
            content_hash=fetched.content_hash,
            local_path=str(fetched.path),
            size_bytes=fetched.media.size_bytes,
            pinned=False,
            last_accessed_at=now,
            created_at=now,
        )
        self._sources.save(source)
        # A picture for the Sources page. After the save and never in front of
        # it: the source is recorded whether or not ffmpeg can find a frame, and
        # an ingest that worked must not be reported as failed over a thumbnail.
        preview = build_source_preview(
            source.id,
            kind=SourceKind.VIDEO,
            path=fetched.path,
            duration_sec=fetched.media.duration_sec,
            work_dir=self._workspace.tmp_dir,
            ffmpeg_bin=context.settings.ffmpeg_bin,
        )
        if preview is not None:
            self._sources.save_preview(preview)
        log.info(
            "ingest.recorded",
            source_id=source.id,
            provider=source.provider.value,
            duration_sec=round(fetched.media.duration_sec, 1),
            size_mb=fetched.media.size_bytes // (1024 * 1024),
        )
        return source


def _file_present(source: Source) -> bool:
    return bool(source.local_path) and Path(source.local_path or "").is_file()


def _discard(fetched: FetchedSource, adapter: SourceAdapter) -> None:
    """Remove a redundant download — but never a user's own file."""
    if isinstance(adapter, LocalFileAdapter):
        return
    fetched.path.unlink(missing_ok=True)


def _as_stage_failure(exc: IngestError) -> Exception:
    """Carry the ingest classification into the runner's error record."""
    failure = _ClassifiedIngestError(exc.message)
    failure.code = exc.code.value
    failure.retryable = exc.retryable
    return failure


class _ClassifiedIngestError(RuntimeError):
    """An ingest failure the runner can record verbatim.

    Exists so the runner does not have to import the ingest vocabulary to know
    whether a failure is worth retrying: the two attributes it reads are set here.
    """

    code: str | None = None
    retryable: bool = False
