"""The publish gate: what has to be true before a clip leaves this machine.

Two conditions, and they are both about *this operator's intent*:

1. **Publishing is switched on.** `CLIPFORGE_PUBLISHING_ENABLED` defaults to
   false, so a fresh install cannot post anything to the internet by accident —
   including a misconfigured one, a test run, or a worker somebody started to
   see what it did.
2. **A person approved the clip.** The review queue exists so that what goes out
   is something a human watched. A clip that is PENDING has not been watched and
   a clip that is REJECTED was watched and turned down; neither is a candidate
   for upload, and treating "the job exists" as consent would make the review
   queue decorative.

## Why this is enforced here and not only in Firestore rules

The worker authenticates with the Admin SDK, which bypasses security rules
entirely. Rules are what stop a *client* from queueing a publish job; they have
no bearing at all on the path that actually performs the upload. So the same two
conditions are checked in both places on purpose, and the duplication is the
feature — remove either one and a whole class of caller is ungated.

## What used to be here

A rights-attestation gate: publishing refused until the operator recorded a
basis (owned, licensed, permission, fair use, public domain), who attested it
and when, re-confirmed yearly. It was removed at the operator's request. The
judgement it recorded was always theirs to make; it was never a legal check, and
asking them to restate it per clip added a click without adding a fact.
Deciding whether footage may be published remains their call — it is just no
longer a field.
"""

from __future__ import annotations

from dataclasses import dataclass

from clipforge_contracts import Clip

__all__ = [
    "PublishRefusal",
    "PublishRefusedError",
    "check_publishable",
    "require_publishable",
]


@dataclass(frozen=True)
class PublishRefusal:
    """Why a clip may not be published, in terms the operator can act on."""

    code: str
    message: str


class PublishRefusedError(RuntimeError):
    """A clip reached the publish path without meeting the conditions."""

    # Never retryable. No amount of waiting approves a clip; only a person can,
    # and the job should say so rather than burn its attempts discovering that
    # three times.
    retryable = False

    def __init__(self, refusal: PublishRefusal) -> None:
        self.code = refusal.code
        self.refusal = refusal
        super().__init__(refusal.message)


def check_publishable(clip: Clip, *, publishing_enabled: bool) -> PublishRefusal | None:
    """Return why this clip may not be published, or ``None`` if it may.

    Returns rather than raises so a caller can list every blocked clip in one
    pass — the UI wants to explain a queue, not stop at the first problem.

    Takes no clock. Both conditions are facts about the present, and the
    attestation-ageing rule that needed one is gone.
    """
    if not publishing_enabled:
        return PublishRefusal(
            "PUBLISHING_DISABLED",
            "Publishing is disabled. Set CLIPFORGE_PUBLISHING_ENABLED=true to turn it on.",
        )

    if clip.review is not None and clip.review.value != "APPROVED":
        return PublishRefusal(
            "NOT_APPROVED",
            f"This clip is {clip.review.value}. Only an approved clip can be published.",
        )

    return None


def require_publishable(clip: Clip, *, publishing_enabled: bool) -> None:
    """Assert the clip may be published, or raise saying why not."""
    refusal = check_publishable(clip, publishing_enabled=publishing_enabled)
    if refusal is not None:
        raise PublishRefusedError(refusal)
