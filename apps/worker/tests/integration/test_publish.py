"""Publishing against the Firestore emulator.

Phase 8, exit criteria 3, 4 and 5. What these add over the unit tests is the
things only a real store can show: that a `Publication` survives a round trip
through Firestore unchanged, that a scheduled publish is genuinely unclaimable
until its time, and — criterion 4 — that a sweep of everything the emulator holds
finds no OAuth token anywhere.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from clipforge.config import Settings
from clipforge.models.broker import ModelBroker
from clipforge.publish.credentials import OAuthTokens, TokenStore
from clipforge.scheduler import lease
from clipforge.stages.base import StageContext
from clipforge.stages.pipeline import new_publish_job
from clipforge.stages.publish import PublishStage
from clipforge.store.firestore import ClipStore, JobStore, PublicationStore
from clipforge_contracts import (
    Clip,
    ClipLocation,
    Publication,
    PublicationState,
    PublishPlatform,
    PublishPrivacy,
    ReviewState,
    StageName,
)
from google.cloud import firestore

from tests.unit.test_publish_stage import FakeYouTube

pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
REFRESH_TOKEN = "1//0e-a-refresh-token-that-must-never-reach-firestore"


@pytest.fixture
def clips(client: firestore.Client, settings: Settings) -> ClipStore:
    return ClipStore(client, settings)


@pytest.fixture
def publications(client: firestore.Client, settings: Settings) -> PublicationStore:
    return PublicationStore(client, settings)


def make_clip(tmp_path: Path) -> Clip:
    rendered = tmp_path / "clip.mp4"
    rendered.write_bytes(b"\x00" * 2048)
    return Clip(
        id="clip-1",
        uid="user-1",
        candidate_id="cand-1",
        source_id="src-1",
        job_id="job-1",
        location=ClipLocation.LOCAL,
        local_path=str(rendered),
        playback_url=None,
        storage_path=None,
        duration_sec=38.0,
        width_px=1080,
        height_px=1920,
        size_bytes=2048,
        render_profile="default",
        title="A hook",
        description="why",
        review=ReviewState.APPROVED,
        created_at=NOW,
    )


def publish_settings(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(
        update={
            "publishing_enabled": True,
            "youtube_token_store": tmp_path / "token.enc",
            "youtube_default_privacy": "unlisted",
        }
    )


def make_context(job: Any, settings: Settings) -> StageContext:
    return StageContext(
        job=job,
        stage_name=StageName.PUBLISH,
        checkpoint=None,
        settings=settings,
        broker=ModelBroker(reserve_mb=settings.vram_reserve_mb),
    )


# ── Round trip ───────────────────────────────────────────────────────────────


def test_a_publication_survives_a_firestore_round_trip(
    publications: PublicationStore,
) -> None:
    """Including the resolved metadata, which is what the audit log is for."""
    record = Publication(
        id="pub-1",
        clip_id="clip-1",
        uid="user-1",
        platform=PublishPlatform.YOUTUBE,
        state=PublicationState.PUBLISHED,
        external_id="vid-1",
        external_url="https://www.youtube.com/watch?v=vid-1",
        privacy=PublishPrivacy.UNLISTED,
        title="A hook",
        description="why",
        tags=["clip"],
        attempts=1,
        quota_units=1600,
        error=None,
        publish_at=None,
        created_at=NOW,
        published_at=NOW,
    )
    publications.save(record)

    loaded = publications.get("clip-1", "pub-1")
    assert loaded == record
    assert loaded is not None
    assert loaded.privacy is PublishPrivacy.UNLISTED
    assert loaded.external_url == "https://www.youtube.com/watch?v=vid-1"


def test_live_for_clip_ignores_a_failed_attempt(publications: PublicationStore) -> None:
    """A failed attempt must not block a fresh one; it is history, not a claim."""
    for state in (PublicationState.FAILED, PublicationState.CANCELLED):
        publications.save(
            Publication(
                id=f"pub-{state.value}",
                clip_id="clip-1",
                uid="user-1",
                platform=PublishPlatform.YOUTUBE,
                state=state,
                created_at=NOW,
            )
        )

    assert publications.live_for_clip("clip-1", "YOUTUBE") is None
    assert len(publications.for_clip("clip-1")) == 2


# ── The stage, against real stores ───────────────────────────────────────────


def test_publishing_writes_an_audit_trail_the_owner_can_reconstruct(
    clips: ClipStore,
    publications: PublicationStore,
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Exit criterion 5, end to end: who authorised it, on what basis, and what
    was uploaded — all answerable from the clip alone."""
    clip = make_clip(tmp_path)
    clips.save(clip)

    stage = PublishStage(
        clips=clips, publications=publications, client_factory=lambda: FakeYouTube()
    )
    job = new_publish_job(uid=clip.uid, clip_id=clip.id)
    stage.run(make_context(job, publish_settings(settings, tmp_path)))

    trail = publications.for_clip(clip.id)
    assert len(trail) == 1
    record = trail[0]
    assert record.state is PublicationState.PUBLISHED
    assert record.external_url == "https://www.youtube.com/watch?v=vid-1"
    assert record.uid == "user-1"
    assert record.title == "A hook"


def test_two_runs_of_the_same_publish_job_produce_one_video(
    clips: ClipStore,
    publications: PublicationStore,
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Exit criterion 3, through a real store.

    The second run must find the first run's PUBLISHED record and stop, without
    reserving a session or sending a byte.
    """
    clip = make_clip(tmp_path)
    clips.save(clip)
    configured = publish_settings(settings, tmp_path)
    job = new_publish_job(uid=clip.uid, clip_id=clip.id)

    first = FakeYouTube()
    PublishStage(clips=clips, publications=publications, client_factory=lambda: first).run(
        make_context(job, configured)
    )

    second = FakeYouTube()
    outcome = PublishStage(
        clips=clips, publications=publications, client_factory=lambda: second
    ).run(make_context(job, configured))

    assert outcome.skipped is True
    assert second.videos == {}
    assert second.sessions == []
    assert len(publications.for_clip(clip.id)) == 1


# ── Scheduling ───────────────────────────────────────────────────────────────


def test_a_scheduled_publish_is_not_claimable_until_its_time(jobs: JobStore) -> None:
    """The publish-at time, honoured by the one predicate every claim path
    consults. A second scheduler could disagree with the first; this cannot."""
    later = datetime.now(UTC) + timedelta(hours=2)
    job = new_publish_job(uid="user-1", clip_id="clip-1", publish_at=later)
    jobs.create(job)

    assert jobs.claim_next() is None
    assert jobs.try_claim(job.id) is None

    # ...and it becomes claimable the moment its time arrives.
    claimed = jobs.try_claim(job.id, now=later + timedelta(seconds=1))
    assert claimed is not None
    assert claimed.id == job.id


def test_an_unscheduled_publish_is_claimable_immediately(jobs: JobStore) -> None:
    job = new_publish_job(uid="user-1", clip_id="clip-1")
    jobs.create(job)

    claimed = jobs.claim_next()
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.clip_id == "clip-1"


def test_a_scheduled_job_does_not_block_a_ready_one_behind_it(jobs: JobStore) -> None:
    """Head-of-line blocking would make one scheduled publish stall the queue."""
    scheduled = new_publish_job(
        uid="user-1", clip_id="clip-1", publish_at=datetime.now(UTC) + timedelta(days=1)
    )
    jobs.create(scheduled)

    ready = new_publish_job(uid="user-1", clip_id="clip-2")
    jobs.create(ready)

    claimed = jobs.claim_next()
    assert claimed is not None
    assert claimed.id == ready.id


def test_notbefore_round_trips_as_a_timestamp_not_a_string(jobs: JobStore) -> None:
    """A string here would compare wrongly the moment a timezone offset differed
    — the same trap the reaper's leaseExpiresAt filter avoids."""
    later = (datetime.now(UTC) + timedelta(hours=3)).replace(microsecond=0)
    job = new_publish_job(uid="user-1", clip_id="clip-1", publish_at=later)
    jobs.create(job)

    stored = jobs.get(job.id)
    assert stored is not None
    assert stored.not_before == later
    assert lease.is_due(stored, datetime.now(UTC)) is False


# ── Exit criterion 4 ─────────────────────────────────────────────────────────


def _every_document(db: firestore.Client) -> list[dict[str, Any]]:
    """Every document in the emulator, subcollections included.

    Written as an explicit walk rather than a collection-group query because a
    collection-group query would only find the subcollections someone remembered
    to name — and the point of this sweep is to catch the one nobody thought of.
    """
    found: list[dict[str, Any]] = []

    def walk(ref: Any) -> None:
        for snapshot in ref.stream():
            found.append(snapshot.to_dict() or {})
            for child in snapshot.reference.collections():
                walk(child)

    for collection in db.collections():
        walk(collection)
    return found


def test_no_oauth_token_reaches_firestore(
    client: firestore.Client,
    clips: ClipStore,
    publications: PublicationStore,
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Exit criterion 4, asserted rather than asserted-about.

    A full publish is run with a real token on disk, and then every document the
    emulator holds is swept for it. This is the test that would catch a future
    change adding "just the access token, for convenience" to a publication.
    """
    store = TokenStore(tmp_path / "token.enc")
    store.save(
        OAuthTokens(
            refresh_token=REFRESH_TOKEN,
            access_token="ya29.an-access-token-that-must-not-leak",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )

    clip = make_clip(tmp_path)
    clips.save(clip)
    job = new_publish_job(uid=clip.uid, clip_id=clip.id)
    JobStore(client, settings).create(job)

    PublishStage(clips=clips, publications=publications, client_factory=lambda: FakeYouTube()).run(
        make_context(job, publish_settings(settings, tmp_path))
    )

    dumped = json.dumps(_every_document(client), default=str)
    assert REFRESH_TOKEN not in dumped
    assert "ya29." not in dumped
    assert "refreshToken" not in dumped
    assert "refresh_token" not in dumped

    # And the sweep genuinely looked at the publication, rather than passing
    # because it found nothing at all.
    assert "vid-1" in dumped


def test_the_resumable_session_url_stays_off_the_publication_record(
    clips: ClipStore,
    publications: PublicationStore,
    settings: Settings,
    tmp_path: Path,
) -> None:
    """A session URL is a bearer capability for one upload.

    It has to be checkpointed somewhere for a resume to work, and the job
    document — readable only by its owner — is the right place. The publication
    record is the audit log, and an audit log should not carry credentials.
    """
    clip = make_clip(tmp_path)
    clips.save(clip)

    platform = FakeYouTube(
        fail_upload_with=RuntimeError("interrupted"),
    )
    stage = PublishStage(clips=clips, publications=publications, client_factory=lambda: platform)
    job = new_publish_job(uid=clip.uid, clip_id=clip.id)

    with pytest.raises(RuntimeError):
        stage.run(make_context(job, publish_settings(settings, tmp_path)))

    stored = json.dumps(
        [p.model_dump(by_alias=True, mode="json") for p in publications.for_clip(clip.id)]
    )
    assert "upload.example" not in stored


def test_a_publish_job_carries_its_clip_through_firestore(jobs: JobStore) -> None:
    job = new_publish_job(uid="user-1", clip_id="clip-42", job_id=uuid.uuid4().hex)
    jobs.create(job)

    stored = jobs.get(job.id)
    assert stored is not None
    assert stored.clip_id == "clip-42"
    assert [stage.name for stage in stored.stages] == [StageName.PUBLISH]
