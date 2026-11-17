"""What a finished job tells its owner (and, for YouTube sign-in, the admins).

Called by the worker once a job has ended, COMPLETED or FAILED for good
(clipforge/scheduler/worker.py). A retry is not an ending, so a job that fails
once and succeeds on its second attempt says only that its clips are ready.
"""

from __future__ import annotations

from collections.abc import Callable

from clipforge_contracts import Job, JobStatus, JobType, NotificationKind

from clipforge.notify import Notifier

__all__ = ["JOB_LABELS", "on_job_end", "wants_youtube_sign_in"]

# Job types whose success is a clip someone will want to look at.
MAKES_CLIPS = {
    JobType.CLIP,
    JobType.REMAKE,
    JobType.COMPOSE,
    JobType.COMPILE,
    JobType.MUSIC,
    JobType.MUSIC_VIDEO,
}

JOB_LABELS = {
    JobType.CLIP: "Clipping",
    JobType.REMAKE: "A remake",
    JobType.COMPOSE: "A drawn video",
    JobType.COMPILE: "A compilation",
    JobType.MUSIC: "Adding music",
    JobType.PUBLISH: "Publishing",
    JobType.UPLOAD: "An upload",
    JobType.RESEARCH: "Research",
    JobType.ECHO: "A test job",
    JobType.MUSIC_VIDEO: "A music video",
}

# How a YouTube credential problem reads once it is a stage error's message:
# an expired or revoked refresh token ("invalid_grant"), a channel never
# authorised, or the credential store refusing.
_SIGN_IN_WORDS = (
    "invalid_grant",
    "reauthor",
    "re-author",
    "not authorised",
    "not authorized",
    "needs_auth",
    "sign in again",
    "refresh token",
    "credential",
)


def wants_youtube_sign_in(job: Job) -> bool:
    message = (job.error.message if job.error else "").lower()
    return job.type is JobType.PUBLISH and any(word in message for word in _SIGN_IN_WORDS)


def on_job_end(job: Job, notifier: Notifier, clips_for_job: Callable[[str], int]) -> None:
    label = JOB_LABELS.get(job.type, "A job")
    if job.status is JobStatus.FAILED:
        reason = job.error.message if job.error else "No reason was recorded."
        notifier.notify(
            NotificationKind.JOB_FAILED,
            title=f"{label} failed",
            body=reason,
            link=f"/jobs/{job.id}",
            to=[job.uid],
            fold=job.id,
        )
        if wants_youtube_sign_in(job):
            notifier.notify(
                NotificationKind.YOUTUBE_SIGN_IN,
                title="YouTube needs signing in again",
                body="A publish failed on the channel's sign-in. Reconnect it on the YouTube page, on the PC.",
                link="/settings/youtube",
                fold="youtube-sign-in",
            )
        return

    if job.status is not JobStatus.COMPLETED:
        return
    if job.type is JobType.PUBLISH and job.clip_id:
        notifier.notify(
            NotificationKind.PUBLISHED,
            title="A clip went out to YouTube",
            body="It is up, with the privacy it was published with.",
            link=f"/publish/{job.clip_id}",
            to=[job.uid],
            fold=job.id,
        )
    elif job.type in MAKES_CLIPS:
        count = clips_for_job(job.id)
        if count:
            noun = "clip is" if count == 1 else "clips are"
            notifier.notify(
                NotificationKind.CLIPS_READY,
                title=f"{count} {noun} ready to review",
                body=f"{label} finished.",
                link="/review",
                to=[job.uid],
                fold=job.id,
            )
