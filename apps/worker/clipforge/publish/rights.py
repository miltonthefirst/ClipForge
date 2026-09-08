"""The rights gate.

Ingesting and analysing someone else's video locally is ordinary private use.
*Publishing* a derived clip of third-party material is a different act, and the
legal basis is the operator's to establish — it varies by jurisdiction, by the
source's licence, and by how transformative the clip is.

ClipForge does not pretend that question away and does not answer it either. It
makes the answer **explicit and auditable**: publishing is off by default, and no
clip reaches the publish queue without a recorded basis, an attesting user and a
timestamp.

The gate is enforced in **two independent places** — Firestore rules and this
module — and that redundancy is deliberate. The worker authenticates with the
Admin SDK, which bypasses rules entirely, so rules alone would protect nothing on
the path that actually performs the upload.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from clipforge_contracts import Clip, RightsAttestation, RightsBasis

__all__ = [
    "MAX_ATTESTATION_AGE_DAYS",
    "RightsRefusal",
    "RightsViolationError",
    "check_publishable",
    "require_publishable",
]

# Every basis is a positive assertion by a person. There is deliberately no
# "unverified" or "probably fine" value: a placeholder that looks like an
# attestation but asserts nothing is precisely the ambiguity a rights gate exists
# to remove.
ACCEPTED_BASES = frozenset(RightsBasis)

# How stale an attestation may be before it is worth re-confirming. Rights change
# — a licence lapses, permission is withdrawn — and an attestation made a year
# ago is evidence about last year.
MAX_ATTESTATION_AGE_DAYS = 365


@dataclass(frozen=True)
class RightsRefusal:
    """Why a clip may not be published, in terms the operator can act on."""

    code: str
    message: str


class RightsViolationError(RuntimeError):
    """A clip reached the publish path without a usable attestation."""

    # Never retryable. No amount of waiting turns an unattested clip into an
    # attested one; only a person can, and the job should say so rather than
    # burn its attempts discovering that three times.
    retryable = False

    def __init__(self, refusal: RightsRefusal) -> None:
        self.code = refusal.code
        self.refusal = refusal
        super().__init__(refusal.message)


def check_publishable(
    clip: Clip,
    *,
    publishing_enabled: bool,
    now: datetime | None = None,
) -> RightsRefusal | None:
    """Return why this clip may not be published, or ``None`` if it may.

    Returns rather than raises so a caller can list every blocked clip in one
    pass — the UI wants to explain a queue, not stop at the first problem.
    """
    now = now or datetime.now(UTC)

    if not publishing_enabled:
        return RightsRefusal(
            "PUBLISHING_DISABLED",
            "Publishing is disabled. Set CLIPFORGE_PUBLISHING_ENABLED=true once you have "
            "read the rights guidance in the README.",
        )

    if clip.review is not None and clip.review.value != "APPROVED":
        return RightsRefusal(
            "NOT_APPROVED",
            f"This clip is {clip.review.value}. Only an approved clip can be published.",
        )

    attestation = clip.rights
    if attestation is None:
        return RightsRefusal(
            "NO_ATTESTATION",
            "This clip has no rights attestation. Record why you may publish it before "
            "it can enter the publish queue.",
        )

    if attestation.basis not in ACCEPTED_BASES:
        return RightsRefusal(
            "UNKNOWN_BASIS",
            f"{attestation.basis!r} is not a recognised rights basis.",
        )

    if not attestation.attested_by:
        # An attestation with nobody's name against it cannot answer the one
        # question the audit log exists to answer.
        return RightsRefusal(
            "NO_ATTESTOR",
            "The rights attestation records no attesting user, so it cannot be audited.",
        )

    if attestation.attested_at is None:
        return RightsRefusal(
            "NO_ATTESTATION_TIME",
            "The rights attestation has no timestamp, so its age cannot be judged.",
        )

    age_days = (now - attestation.attested_at).days
    if age_days > MAX_ATTESTATION_AGE_DAYS:
        return RightsRefusal(
            "ATTESTATION_STALE",
            f"This attestation is {age_days} days old. Rights change — confirm it still "
            "holds before publishing.",
        )

    if attestation.basis is RightsBasis.FAIR_USE_ASSERTED and not (attestation.note or "").strip():
        # Fair use is a judgement, not a status, and it is the one basis whose
        # reasoning genuinely matters after the fact. An empty note here would
        # make the audit log say "because I said so".
        return RightsRefusal(
            "FAIR_USE_NEEDS_REASONING",
            "A fair-use assertion needs a note explaining the reasoning. It is a judgement, "
            "not a status, and the audit log should record why.",
        )

    return None


def require_publishable(
    clip: Clip, *, publishing_enabled: bool, now: datetime | None = None
) -> RightsAttestation:
    """Assert the clip may be published, and return the attestation that says so.

    Returning the attestation matters: the caller copies it onto the publication
    record, so the reason that justified *this* upload survives a later edit to
    the clip.
    """
    refusal = check_publishable(clip, publishing_enabled=publishing_enabled, now=now)
    if refusal is not None:
        raise RightsViolationError(refusal)
    assert clip.rights is not None  # noqa: S101 - guaranteed by check_publishable
    return clip.rights
