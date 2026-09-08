"""The PUBLISH stage — Phase 8, exit criteria 3 and 5.

Criterion 3 is "interrupting the upload and retrying produces exactly one video".
That is asserted here by counting the uploads a fake platform received across a
crash-and-retry, which is the only way to assert it without an actual YouTube
channel. Criterion 1 — a real video appearing on a real channel — needs the
operator's Google account and cannot be automated; it is recorded as unverified
in docs/PLAN.md rather than papered over with a mock that proves nothing.

The stores are in-memory fakes rather than the emulator. What is under test is
the *ordering* of the writes, and an in-memory store lets a test interrupt the
stage between any two of them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from clipforge.config import Settings
from clipforge.models.broker import ModelBroker
from clipforge.publish.rights import RightsViolationError
from clipforge.publish.youtube import UPLOAD_QUOTA_UNITS, QuotaLedger, UploadResult, YouTubeError
from clipforge.stages.base import StageContext
from clipforge.stages.publish import PublishStage, PublishStageError
from clipforge_contracts import (
    Clip,
    ClipLocation,
    Job,
    JobStatus,
    JobType,
    Lane,
    Publication,
    PublicationState,
    ReviewState,
    RightsAttestation,
    RightsBasis,
    Stage,
    StageName,
    StageStatus,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeClipStore:
    def __init__(self, clip: Clip | None) -> None:
        self._clip = clip

    def get(self, clip_id: str) -> Clip | None:
        if self._clip is not None and self._clip.id == clip_id:
            return self._clip
        return None


class FakePublicationStore:
    """An in-memory stand-in that also records the order of every write.

    The order is the assertion. "PENDING was written before the upload started"
    is the entire idempotency argument, and it is invisible to a store that only
    keeps final state.
    """

    def __init__(self) -> None:
        self.docs: dict[str, Publication] = {}
        self.writes: list[tuple[str, str]] = []

    def save(self, publication: Publication) -> None:
        self.docs[publication.id] = publication
        self.writes.append((publication.id, publication.state.value))

    def for_clip(self, clip_id: str) -> list[Publication]:
        return [p for p in self.docs.values() if p.clip_id == clip_id]

    def live_for_clip(self, clip_id: str, platform: str) -> Publication | None:
        for publication in self.for_clip(clip_id):
            if publication.platform.value == platform and publication.state in (
                PublicationState.PENDING,
                PublicationState.UPLOADING,
                PublicationState.PUBLISHED,
            ):
                return publication
        return None


class FakeYouTube:
    """A platform that counts videos, so double-posting is detectable."""

    def __init__(
        self,
        *,
        fail_upload_with: Exception | None = None,
        video_id: str = "vid-1",
    ) -> None:
        self.videos: dict[str, dict[str, Any]] = {}
        self.sessions: list[str] = []
        self.fail_upload_with = fail_upload_with
        self.video_id = video_id
        self.quota = QuotaLedger.today()
        # Set when a previous "attempt" succeeded on the platform but the worker
        # never learned about it — the lost-acknowledgement case.
        self.orphan: str | None = None

    def start_resumable_upload(self, **kwargs: Any) -> str:
        session = f"https://upload.example/session-{len(self.sessions) + 1}"
        self.sessions.append(session)
        self.metadata = kwargs
        return session

    def upload_bytes(self, session_url: str, clip: Path) -> UploadResult:
        if self.fail_upload_with is not None:
            raise self.fail_upload_with
        self.videos[self.video_id] = {"session": session_url}
        self.quota.spend(UPLOAD_QUOTA_UNITS)
        return UploadResult(
            video_id=self.video_id,
            url=f"https://www.youtube.com/watch?v={self.video_id}",
            quota_units=UPLOAD_QUOTA_UNITS,
        )

    def find_video(self, video_id: str) -> dict[str, Any] | None:
        if video_id == self.orphan:
            return {"id": video_id}
        return self.videos.get(video_id)


# ── Builders ─────────────────────────────────────────────────────────────────


def make_clip(
    tmp_path: Path, *, attested: bool = True, review: ReviewState = ReviewState.APPROVED
) -> Clip:
    rendered = tmp_path / "clip.mp4"
    rendered.write_bytes(b"\x00" * 4096)
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
        duration_sec=42.0,
        width_px=1080,
        height_px=1920,
        size_bytes=4096,
        render_profile="default",
        title="A hook worth watching",
        description="why it matters",
        review=review,
        rights=(
            RightsAttestation(
                basis=RightsBasis.OWN_CONTENT, attested_by="user-1", attested_at=NOW, note=None
            )
            if attested
            else None
        ),
        created_at=NOW,
    )


def make_job(*, clip_id: str | None = "clip-1", not_before: datetime | None = None) -> Job:
    return Job(
        id="job-pub-1",
        uid="user-1",
        type=JobType.PUBLISH,
        status=JobStatus.RUNNING,
        clip_id=clip_id,
        not_before=not_before,
        stages=[Stage(name=StageName.PUBLISH, lane=Lane.CPU, status=StageStatus.RUNNING)],
        attempts=1,
        max_attempts=3,
        created_at=NOW,
        updated_at=NOW,
    )


def make_context(
    job: Job, *, checkpoint: dict[str, Any] | None = None, publishing_enabled: bool = True
) -> StageContext:
    settings = Settings(
        use_emulators=True,
        publishing_enabled=publishing_enabled,
        youtube_default_privacy="unlisted",
    )
    return StageContext(
        job=job,
        stage_name=StageName.PUBLISH,
        checkpoint=checkpoint,
        settings=settings,
        # PUBLISH never touches the GPU, so the broker is present only to
        # satisfy the stage contract.
        broker=ModelBroker(reserve_mb=settings.vram_reserve_mb),
    )


def build(
    clip: Clip | None, platform: FakeYouTube, publications: FakePublicationStore
) -> PublishStage:
    return PublishStage(
        clips=FakeClipStore(clip),  # type: ignore[arg-type]
        publications=publications,  # type: ignore[arg-type]
        client_factory=lambda: platform,
    )


# ── The gate, at the point of upload ─────────────────────────────────────────


def test_the_gate_is_checked_before_a_credential_is_ever_read(tmp_path: Path) -> None:
    """The client factory must not run for a clip that cannot be published.

    This is not merely tidy: on a machine with no token file, constructing the
    client raises, and the operator would see a credentials error where the real
    answer is "this clip has no attestation".
    """

    def explode() -> FakeYouTube:
        raise AssertionError("no credential should be read for an unpublishable clip")

    stage = PublishStage(
        clips=FakeClipStore(make_clip(tmp_path, attested=False)),  # type: ignore[arg-type]
        publications=FakePublicationStore(),  # type: ignore[arg-type]
        client_factory=explode,
    )

    with pytest.raises(RightsViolationError) as caught:
        stage.run(make_context(make_job()))
    assert caught.value.code == "NO_ATTESTATION"


def test_publishing_disabled_refuses_at_the_worker_not_only_in_the_rules(
    tmp_path: Path,
) -> None:
    """Exit criterion 2, worker half, at the point that actually uploads.

    The worker holds Admin credentials and bypasses firestore.rules entirely, so
    this check is the only one on this path.
    """
    publications = FakePublicationStore()
    stage = build(make_clip(tmp_path), FakeYouTube(), publications)

    with pytest.raises(RightsViolationError) as caught:
        stage.run(make_context(make_job(), publishing_enabled=False))

    assert caught.value.code == "PUBLISHING_DISABLED"
    # Nothing was recorded: a refusal is not an attempt.
    assert publications.docs == {}


def test_an_unapproved_clip_is_refused_at_the_worker(tmp_path: Path) -> None:
    stage = build(
        make_clip(tmp_path, review=ReviewState.PENDING), FakeYouTube(), FakePublicationStore()
    )
    with pytest.raises(RightsViolationError) as caught:
        stage.run(make_context(make_job()))
    assert caught.value.code == "NOT_APPROVED"


# ── The happy path ───────────────────────────────────────────────────────────


def test_a_successful_publish_records_pending_before_it_uploads(tmp_path: Path) -> None:
    """The ordering that makes every retry case recoverable."""
    publications = FakePublicationStore()
    platform = FakeYouTube()
    outcome = build(make_clip(tmp_path), platform, publications).run(make_context(make_job()))

    states = [state for _, state in publications.writes]
    assert states == ["PENDING", "UPLOADING", "PUBLISHED"]
    assert outcome.metadata["videoId"] == "vid-1"
    assert len(platform.videos) == 1


def test_the_publication_carries_the_attestation_that_justified_this_upload(
    tmp_path: Path,
) -> None:
    """Exit criterion 5: the audit log reconstructs who authorised it and why.

    Copied onto the record rather than referenced, so a later edit to the clip
    cannot rewrite the reason a past upload happened.
    """
    publications = FakePublicationStore()
    build(make_clip(tmp_path), FakeYouTube(), publications).run(make_context(make_job()))

    published = next(iter(publications.docs.values()))
    assert published.rights is not None
    assert published.rights.basis is RightsBasis.OWN_CONTENT
    assert published.rights.attested_by == "user-1"
    assert published.rights.attested_at == NOW
    assert published.external_id == "vid-1"
    assert published.published_at is not None
    assert published.quota_units == UPLOAD_QUOTA_UNITS


def test_privacy_defaults_to_unlisted(tmp_path: Path) -> None:
    """Publishing to the world by accident is not recoverable the way an
    unlisted upload is — the link may already have been scraped."""
    platform = FakeYouTube()
    build(make_clip(tmp_path), platform, FakePublicationStore()).run(make_context(make_job()))
    assert platform.metadata["privacy"] == "unlisted"


def test_the_clip_title_and_description_reach_the_platform(tmp_path: Path) -> None:
    platform = FakeYouTube()
    build(make_clip(tmp_path), platform, FakePublicationStore()).run(make_context(make_job()))
    assert platform.metadata["title"] == "A hook worth watching"
    assert platform.metadata["description"] == "why it matters"


# ── Idempotency: exit criterion 3 ────────────────────────────────────────────


def test_a_retry_after_an_interrupted_upload_resumes_and_makes_one_video(
    tmp_path: Path,
) -> None:
    """Interrupt mid-upload, retry, and count the videos.

    The retry must reuse the checkpointed session rather than reserving a second
    one: two sessions is how a retry becomes two videos.
    """
    clip = make_clip(tmp_path)
    publications = FakePublicationStore()
    job = make_job()

    interrupted = FakeYouTube(
        fail_upload_with=YouTubeError("connection reset", retryable=True, code="UPLOAD_INCOMPLETE")
    )
    context = make_context(job)
    with pytest.raises(PublishStageError):
        build(clip, interrupted, publications).run(context)

    # The stage mutated its own copy; the runner would have persisted it. Model
    # that by carrying the checkpoint forward the way a real retry does.
    checkpoint = {"publicationId": next(iter(publications.docs)), **_checkpoint_after(interrupted)}

    resumed = FakeYouTube()
    outcome = build(clip, resumed, publications).run(make_context(job, checkpoint=checkpoint))

    assert len(resumed.videos) == 1
    assert resumed.sessions == []  # resumed, not restarted
    assert outcome.metadata["videoId"] == "vid-1"
    assert len(publications.docs) == 1  # one attempt record, not two


def _checkpoint_after(platform: FakeYouTube) -> dict[str, Any]:
    """The checkpoint the runner would have persisted after the failed attempt."""
    return {"uploadSessionUrl": platform.sessions[-1]} if platform.sessions else {}


def test_a_retry_reconciles_an_upload_the_platform_already_accepted(
    tmp_path: Path,
) -> None:
    """The case naive implementations get wrong.

    "Upload succeeded but the acknowledgement was lost" and "upload never
    happened" are indistinguishable from the worker's side. Only the platform
    can tell them apart, so the retry asks it — and uploads nothing.
    """
    clip = make_clip(tmp_path)
    publications = FakePublicationStore()
    publications.save(
        Publication(
            id="pub-1",
            clip_id=clip.id,
            uid=clip.uid,
            platform="YOUTUBE",
            state=PublicationState.UPLOADING,
            external_id="vid-orphan",
            created_at=NOW,
        )
    )

    platform = FakeYouTube()
    platform.orphan = "vid-orphan"

    outcome = build(clip, platform, publications).run(make_context(make_job()))

    assert outcome.skipped is True
    assert platform.videos == {}  # nothing was uploaded a second time
    assert publications.docs["pub-1"].state is PublicationState.PUBLISHED


def test_a_clip_already_published_is_skipped_not_republished(tmp_path: Path) -> None:
    clip = make_clip(tmp_path)
    publications = FakePublicationStore()
    publications.save(
        Publication(
            id="pub-1",
            clip_id=clip.id,
            uid=clip.uid,
            platform="YOUTUBE",
            state=PublicationState.PUBLISHED,
            external_id="vid-1",
            external_url="https://www.youtube.com/watch?v=vid-1",
            created_at=NOW,
        )
    )

    platform = FakeYouTube()
    outcome = build(clip, platform, publications).run(make_context(make_job()))

    assert outcome.skipped is True
    assert platform.sessions == []
    assert platform.videos == {}


def test_a_failed_attempt_stays_on_the_record(tmp_path: Path) -> None:
    """A repeatedly failing upload must not look like one that was never
    attempted — that is the difference the review UI needs to show."""
    publications = FakePublicationStore()
    platform = FakeYouTube(
        fail_upload_with=YouTubeError("no such channel", retryable=False, code="YOUTUBE_ERROR")
    )

    with pytest.raises(PublishStageError) as caught:
        build(make_clip(tmp_path), platform, publications).run(make_context(make_job()))

    assert caught.value.retryable is False
    record = next(iter(publications.docs.values()))
    assert record.state is PublicationState.FAILED
    assert record.error is not None
    assert record.error.retryable is False


# ── Preconditions ────────────────────────────────────────────────────────────


def test_a_publish_job_naming_no_clip_fails_without_retrying(tmp_path: Path) -> None:
    stage = build(make_clip(tmp_path), FakeYouTube(), FakePublicationStore())
    with pytest.raises(PublishStageError) as caught:
        stage.run(make_context(make_job(clip_id=None)))
    assert caught.value.retryable is False


def test_a_missing_rendered_file_says_to_re_render_rather_than_retrying(
    tmp_path: Path,
) -> None:
    """On the free tier the file never left this machine, so a missing file
    means a wiped workspace. Retrying cannot conjure it back."""
    clip = make_clip(tmp_path)
    Path(clip.local_path).unlink()

    stage = build(clip, FakeYouTube(), FakePublicationStore())
    with pytest.raises(PublishStageError) as caught:
        stage.run(make_context(make_job()))

    assert caught.value.retryable is False
    assert "Re-run the clip job" in str(caught.value)


def test_a_scheduled_publish_time_is_recorded_on_the_audit_record(tmp_path: Path) -> None:
    later = NOW + timedelta(hours=6)
    publications = FakePublicationStore()
    build(make_clip(tmp_path), FakeYouTube(), publications).run(
        make_context(make_job(not_before=later))
    )
    assert next(iter(publications.docs.values())).publish_at == later
