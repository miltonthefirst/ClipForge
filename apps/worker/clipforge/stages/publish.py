"""The PUBLISH stage: an approved, attested clip reaches YouTube.

This is a one-stage job type of its own rather than a fifth stage of CLIP,
because publishing happens on the far side of a human decision. A CLIP job that
carried a PUBLISH stage would sit RUNNING — holding a lease, blocking a lane,
heartbeating — for however many days the operator took to review it on their
phone. The review is asynchronous by nature, so the pipeline ends at RENDER and
approval starts something new.

## The ordering that makes a retry safe

    1. check the rights gate
    2. write a PENDING publication document
    3. reserve a resumable upload session, checkpoint its URL
    4. send the bytes
    5. mark the publication PUBLISHED with the video id

A crash between any two of those is recoverable, and none of them produces a
second video:

- crash after 2 → the retry finds the PENDING record, sees no session, starts one
- crash after 3 → the retry finds the checkpointed session URL and resumes it
- crash after 4 but before 5 → the retry finds the record, and asks YouTube
  whether the video exists before doing anything else

The last case is the one that matters and the one naive implementations get
wrong. "Upload succeeded but the acknowledgement was lost" and "upload never
happened" look identical from the worker's side; the only thing that can tell
them apart is the platform itself, so the retry asks it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clipforge_contracts import (
    Channel,
    Clip,
    Lane,
    Publication,
    PublicationState,
    PublishOptions,
    PublishPlatform,
    PublishPrivacy,
    StageError,
    StageName,
)

from clipforge.observability import get_logger
from clipforge.publish.metadata import (
    FALLBACK_CATEGORY,
    FALLBACK_TITLE,
    PublishMetadata,
    resolve_metadata,
)
from clipforge.publish.rights import require_publishable
from clipforge.publish.youtube import YouTubeClient, YouTubeError
from clipforge.stages.base import StageContext, StageOutcome
from clipforge.store.channels import ChannelStore
from clipforge.store.firestore import ClipStore, PublicationStore

log = get_logger(__name__)

__all__ = ["PublishStage", "PublishStageError"]


class PublishStageError(RuntimeError):
    """The clip could not be published for a reason that is not a rights refusal.

    ``retryable`` and ``code`` are read by :class:`~clipforge.scheduler.runner.
    StageRunner`, which is what decides whether the job burns another attempt.
    Nothing here needs to build a ``StageError`` itself.
    """

    def __init__(
        self, message: str, *, retryable: bool = True, code: str = "PUBLISH_FAILED"
    ) -> None:
        self.retryable = retryable
        self.code = code
        super().__init__(message)


class PublishStage:
    """Upload one approved clip, once, with an audit record either way."""

    name = StageName.PUBLISH
    # Network-bound, not GPU-bound. It must not take the model residency lock:
    # a 20 MB upload holding the GPU lane would stall transcription for minutes
    # over something that never touches CUDA.
    lane = Lane.CPU

    def __init__(
        self,
        *,
        clips: ClipStore,
        publications: PublicationStore,
        client_factory: Any,
        channels: ChannelStore | None = None,
    ) -> None:
        self._clips = clips
        self._publications = publications
        # A factory rather than a client, so nothing tries to read a token file
        # on a machine that has never authorised YouTube. A worker that only
        # runs CLIP jobs must start cleanly with no credentials at all.
        self._client_factory = client_factory
        # Optional, because a worker with no Firestore reachable can still
        # publish: the channel document supplies *preferences*, and the absence
        # of preferences is a default, not a failure.
        self._channels = channels

    def run(self, context: StageContext) -> StageOutcome:
        clip = self._require_clip(context)
        settings = context.settings

        # The gate first, before a token is read or a byte moves. It raises
        # rather than returns here: by the time a job exists, "may not publish"
        # is a failure, not a filter.
        attestation = require_publishable(clip, publishing_enabled=settings.publishing_enabled)

        checkpoint = dict(context.checkpoint or {})
        publication = self._publication_for(clip, attestation, checkpoint, context)

        if publication.state is PublicationState.PUBLISHED:
            # Someone — most likely a previous attempt of this job — already did
            # this. Recorded as SKIPPED, so "we published it" and "it was already
            # published" stay distinguishable in the job document.
            log.info("publish.already_published", clip=clip.id, video=publication.external_id)
            return StageOutcome(
                checkpoint=checkpoint | {"publicationId": publication.id},
                skipped=True,
                detail=f"already published as {publication.external_url}",
            )

        client = self._client_factory()
        reconciled = self._reconcile(client, publication)
        if reconciled is not None:
            self._finish(reconciled, checkpoint)
            return StageOutcome(
                checkpoint=checkpoint,
                skipped=True,
                detail=f"a previous attempt had already uploaded {reconciled.external_url}",
            )

        return self._upload(context, client, clip, publication, checkpoint)

    # ── The attempt ──────────────────────────────────────────────────────────

    def _upload(
        self,
        context: StageContext,
        client: YouTubeClient,
        clip: Clip,
        publication: Publication,
        checkpoint: dict[str, Any],
    ) -> StageOutcome:
        source_file = Path(clip.local_path)
        # Read off the publication record rather than re-resolved here. The
        # record was written before the first attempt, so a retry sends exactly
        # what the first attempt sent — even if the channel's defaults were
        # edited in between, and even if the resumable session it is resuming
        # was reserved with the old values.
        privacy = publication.privacy or PublishPrivacy.UNLISTED

        try:
            session_url = checkpoint.get("uploadSessionUrl")
            if not session_url:
                session_url = client.start_resumable_upload(
                    title=publication.title or FALLBACK_TITLE,
                    description=publication.description or "",
                    tags=list(publication.tags or []),
                    privacy=privacy.value,
                    category_id=publication.category_id or FALLBACK_CATEGORY,
                )
                # Checkpointed before the bytes move. The session URL is what a
                # resume needs, and it is a job-scoped value — it lives on the
                # job document, which only its owner can read, and never on the
                # publication record that the audit log exposes.
                checkpoint["uploadSessionUrl"] = session_url

            publication = publication.model_copy(
                update={
                    "state": PublicationState.UPLOADING,
                    "attempts": (publication.attempts or 0) + 1,
                }
            )
            self._publications.save(publication)

            result = client.upload_bytes(session_url, source_file)
        except YouTubeError as exc:
            self._record_failure(publication, exc)
            if not exc.retryable:
                raise PublishStageError(str(exc), retryable=False) from exc
            raise PublishStageError(str(exc), retryable=True) from exc

        published = publication.model_copy(
            update={
                "state": PublicationState.PUBLISHED,
                "external_id": result.video_id,
                "external_url": result.url,
                "quota_units": result.quota_units,
                "published_at": datetime.now(UTC),
                "error": None,
            }
        )
        self._publications.save(published)
        self._finish(published, checkpoint)

        log.info(
            "publish.done",
            clip=clip.id,
            video=result.video_id,
            privacy=privacy.value,
            uploads_left_today=client.quota.uploads_remaining,
        )
        return StageOutcome(
            checkpoint=checkpoint,
            detail=f"published as {result.url} ({privacy.value})",
            metadata={
                "videoId": result.video_id,
                "quotaUnits": result.quota_units,
                "uploadsRemainingToday": client.quota.uploads_remaining,
            },
        )

    # ── Idempotency ──────────────────────────────────────────────────────────

    def _publication_for(
        self,
        clip: Clip,
        attestation: Any,
        checkpoint: dict[str, Any],
        context: StageContext,
    ) -> Publication:
        """Find this job's publication record, or write it before doing anything.

        Written *first*, deliberately: a record with no video is a recoverable
        state, and a video with no record is not.
        """
        existing = self._publications.live_for_clip(clip.id, PublishPlatform.YOUTUBE.value)
        if existing is not None:
            checkpoint["publicationId"] = existing.id
            return existing

        # Resolved once, here, and only on the path that creates the record:
        # three layers of preference collapse into one answer
        # (clipforge/publish/metadata.py), and that answer is what the audit
        # trail holds.
        metadata = self._metadata(context, clip)

        publication = Publication(
            id=checkpoint.get("publicationId") or uuid.uuid4().hex,
            clip_id=clip.id,
            uid=clip.uid,
            platform=PublishPlatform.YOUTUBE,
            state=PublicationState.PENDING,
            external_id=None,
            external_url=None,
            channel_id=metadata.channel_id,
            privacy=metadata.privacy,
            title=metadata.title,
            description=metadata.description,
            tags=metadata.tags,
            category_id=metadata.category_id,
            # Copied, not referenced. If the operator later edits the clip's
            # attestation, this record must still say what justified *this*
            # upload — that is the difference between an audit log and a pointer.
            rights=attestation,
            attempts=0,
            quota_units=None,
            error=None,
            publish_at=context.job.not_before,
            created_at=datetime.now(UTC),
            published_at=None,
        )
        self._publications.save(publication)
        checkpoint["publicationId"] = publication.id
        log.info("publish.pending_recorded", clip=clip.id, publication=publication.id)
        return publication

    def _reconcile(self, client: YouTubeClient, publication: Publication) -> Publication | None:
        """Ask YouTube whether a previous attempt already landed.

        Only asked when a previous attempt got far enough to be uncertain. A
        first attempt has nothing to reconcile and should not spend a quota unit
        establishing that.
        """
        if publication.state is not PublicationState.UPLOADING:
            return None
        if not publication.external_id:
            # It reached UPLOADING without recording a video id, so no video was
            # ever acknowledged. Nothing to look up; the session resumes instead.
            return None

        found = client.find_video(publication.external_id)
        if found is None:
            return None

        log.info("publish.reconciled", video=publication.external_id)
        recovered = publication.model_copy(
            update={
                "state": PublicationState.PUBLISHED,
                "published_at": publication.published_at or datetime.now(UTC),
            }
        )
        self._publications.save(recovered)
        return recovered

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _require_clip(self, context: StageContext) -> Clip:
        clip_id = context.job.clip_id
        if not clip_id:
            raise PublishStageError(
                "this publish job names no clip; it cannot be recovered by retrying",
                retryable=False,
            )
        clip = self._clips.get(clip_id)
        if clip is None:
            raise PublishStageError(f"clip {clip_id} no longer exists", retryable=False)
        if not Path(clip.local_path).is_file():
            # On the free tier the file never left this machine, so a missing
            # file means a wiped workspace — re-render, do not retry.
            raise PublishStageError(
                f"the rendered file for clip {clip_id} is gone ({clip.local_path}). "
                "Re-run the clip job to render it again.",
                retryable=False,
            )
        return clip

    def _metadata(self, context: StageContext, clip: Clip) -> PublishMetadata:
        """What this upload will say, from the three layers that can say it."""
        options = context.job.publish_options
        return resolve_metadata(
            clip=clip,
            options=options,
            channel=self._channel(options),
            default_privacy=context.settings.youtube_default_privacy,
        )

    def _channel(self, options: PublishOptions | None) -> Channel | None:
        """The channel this publish is for, or None to use the install defaults.

        A named channel that does not exist is a hard failure rather than a
        fallback. Publishing to a different destination than the one the
        operator picked is worse than not publishing: an unlisted upload can be
        deleted, and a video on the wrong channel has already been the wrong
        video on the wrong channel.
        """
        wanted = options.channel_id if options is not None else None
        if self._channels is None:
            if wanted:
                # Only reachable on a worker running without Firestore, which
                # cannot have been handed a job from Firestore either. Said out
                # loud rather than swallowed, because the symptom otherwise is a
                # publish that quietly used the wrong defaults.
                log.warning("publish.channel_unverifiable", channel=wanted)
            return None

        if wanted:
            channel = self._channels.get(wanted)
            if channel is None:
                raise PublishStageError(
                    f"this publish names channel {wanted!r}, which no longer exists. "
                    "Pick a channel on the publish screen and try again.",
                    retryable=False,
                    code="CHANNEL_MISSING",
                )
            return channel
        return self._channels.default()

    def _record_failure(self, publication: Publication, exc: YouTubeError) -> None:
        """Record why an attempt failed, on the attempt itself.

        A failed publication stays queryable: "this clip was tried and refused"
        is information the review UI needs, and losing it would make a repeatedly
        failing upload look like one that was never attempted.
        """
        self._publications.save(
            publication.model_copy(
                update={
                    "state": PublicationState.FAILED if not exc.retryable else publication.state,
                    "error": StageError(
                        type=type(exc).__name__,
                        message=str(exc)[:500],
                        code=exc.code,
                        traceback=None,
                        retryable=exc.retryable,
                    ),
                }
            )
        )

    def _finish(self, publication: Publication, checkpoint: dict[str, Any]) -> None:
        checkpoint["publicationId"] = publication.id
        checkpoint["videoId"] = publication.external_id
        # The session is spent. Clearing it stops a later retry from resuming a
        # completed upload session, which YouTube would answer confusingly.
        checkpoint.pop("uploadSessionUrl", None)
