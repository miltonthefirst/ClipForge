"""The rights gate — Phase 8, exit criterion 2 (worker half).

The rules half lives in firebase/tests/publishing.rules.test.mjs. Both are
required, because they defend different paths: the worker uses Admin credentials
and bypasses rules entirely, and the client cannot be stopped from enqueueing
work by anything except a rule.

These are pure-function tests: no Firestore, no network, no tokens. That is the
whole reason `rights.py` imports nothing from the platform code.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from clipforge.publish.rights import (
    MAX_ATTESTATION_AGE_DAYS,
    RightsViolationError,
    check_publishable,
    require_publishable,
)
from clipforge_contracts import Clip, ClipLocation, ReviewState, RightsAttestation, RightsBasis

# Every test in this file is the unit tier. Without this marker CI's
# `pytest -m unit` silently deselects the whole file — the tests pass locally,
# run nowhere, and protect nothing.
pytestmark = pytest.mark.unit

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def make_clip(
    *,
    review: ReviewState | None = ReviewState.APPROVED,
    rights: RightsAttestation | None = None,
) -> Clip:
    return Clip(
        id="clip-1",
        uid="user-1",
        candidate_id="cand-1",
        source_id="src-1",
        job_id="job-1",
        location=ClipLocation.LOCAL,
        local_path="/workspace/clips/clip-1.mp4",
        playback_url=None,
        storage_path=None,
        duration_sec=42.0,
        width_px=1080,
        height_px=1920,
        size_bytes=8_000_000,
        render_profile="default",
        title="A hook",
        description="why",
        review=review,
        rights=rights,
        created_at=NOW,
    )


def attestation(
    *,
    basis: RightsBasis = RightsBasis.OWN_CONTENT,
    by: str | None = "user-1",
    at: datetime | None = NOW,
    note: str | None = None,
) -> RightsAttestation:
    return RightsAttestation(basis=basis, attested_by=by, attested_at=at, note=note)


def test_publishing_disabled_refuses_even_a_perfect_clip() -> None:
    """The default is off, and off means off.

    This is the criterion that matters most: a clip with impeccable paperwork
    must still not publish from a machine where nobody turned publishing on.
    """
    refusal = check_publishable(make_clip(rights=attestation()), publishing_enabled=False, now=NOW)
    assert refusal is not None
    assert refusal.code == "PUBLISHING_DISABLED"


def test_a_clip_with_no_attestation_is_refused() -> None:
    refusal = check_publishable(make_clip(rights=None), publishing_enabled=True, now=NOW)
    assert refusal is not None
    assert refusal.code == "NO_ATTESTATION"


@pytest.mark.parametrize("state", [ReviewState.PENDING, ReviewState.REJECTED])
def test_an_unapproved_clip_is_refused_however_good_its_paperwork(state: ReviewState) -> None:
    """Attestation is not approval. A rejected clip with a valid basis is still
    a clip a human said no to."""
    refusal = check_publishable(
        make_clip(review=state, rights=attestation()), publishing_enabled=True, now=NOW
    )
    assert refusal is not None
    assert refusal.code == "NOT_APPROVED"


def test_an_attestation_with_no_attestor_cannot_be_audited() -> None:
    refusal = check_publishable(
        make_clip(rights=attestation(by=None)), publishing_enabled=True, now=NOW
    )
    assert refusal is not None
    assert refusal.code == "NO_ATTESTOR"


def test_an_undated_attestation_is_refused() -> None:
    refusal = check_publishable(
        make_clip(rights=attestation(at=None)), publishing_enabled=True, now=NOW
    )
    assert refusal is not None
    assert refusal.code == "NO_ATTESTATION_TIME"


def test_a_stale_attestation_is_refused_because_rights_change() -> None:
    """A licence lapses; permission is withdrawn. An attestation made two years
    ago is evidence about two years ago."""
    old = NOW - timedelta(days=MAX_ATTESTATION_AGE_DAYS + 1)
    refusal = check_publishable(
        make_clip(rights=attestation(at=old)), publishing_enabled=True, now=NOW
    )
    assert refusal is not None
    assert refusal.code == "ATTESTATION_STALE"


def test_an_attestation_exactly_at_the_age_limit_still_passes() -> None:
    """The boundary is inclusive. Refusing on the exact day would make the
    limit a day shorter than it says it is."""
    edge = NOW - timedelta(days=MAX_ATTESTATION_AGE_DAYS)
    assert (
        check_publishable(make_clip(rights=attestation(at=edge)), publishing_enabled=True, now=NOW)
        is None
    )


def test_fair_use_needs_reasoning_but_the_other_bases_do_not() -> None:
    """Fair use is a judgement, not a status. An empty note would make the audit
    log say "because I said so" — which is precisely what it exists to avoid."""
    bare = check_publishable(
        make_clip(rights=attestation(basis=RightsBasis.FAIR_USE_ASSERTED)),
        publishing_enabled=True,
        now=NOW,
    )
    assert bare is not None
    assert bare.code == "FAIR_USE_NEEDS_REASONING"

    reasoned = check_publishable(
        make_clip(
            rights=attestation(
                basis=RightsBasis.FAIR_USE_ASSERTED,
                note="30s of a 90m lecture, transformed with commentary captions.",
            )
        ),
        publishing_enabled=True,
        now=NOW,
    )
    assert reasoned is None


def test_whitespace_is_not_reasoning() -> None:
    refusal = check_publishable(
        make_clip(rights=attestation(basis=RightsBasis.FAIR_USE_ASSERTED, note="   \n ")),
        publishing_enabled=True,
        now=NOW,
    )
    assert refusal is not None
    assert refusal.code == "FAIR_USE_NEEDS_REASONING"


@pytest.mark.parametrize(
    "basis",
    [
        RightsBasis.OWN_CONTENT,
        RightsBasis.LICENSED,
        RightsBasis.PERMISSION_GRANTED,
        RightsBasis.PUBLIC_DOMAIN,
    ],
)
def test_every_non_fair_use_basis_passes_when_complete(basis: RightsBasis) -> None:
    assert (
        check_publishable(
            make_clip(rights=attestation(basis=basis)), publishing_enabled=True, now=NOW
        )
        is None
    )


def test_require_publishable_returns_the_attestation_for_the_audit_record() -> None:
    """Returning it is the point: the caller copies it onto the publication, so
    the reason that justified *this* upload survives a later edit to the clip."""
    given = attestation(basis=RightsBasis.LICENSED, note="CC-BY 4.0")
    returned = require_publishable(make_clip(rights=given), publishing_enabled=True, now=NOW)
    assert returned == given


def test_a_refusal_is_never_retryable() -> None:
    """No amount of waiting turns an unattested clip into an attested one. A
    retryable refusal would burn all three attempts learning that."""
    with pytest.raises(RightsViolationError) as caught:
        require_publishable(make_clip(rights=None), publishing_enabled=True, now=NOW)

    assert caught.value.retryable is False
    assert caught.value.code == "NO_ATTESTATION"


def test_refusal_messages_say_what_to_do_not_just_what_is_wrong() -> None:
    """A gate that only says "no" makes the operator guess. Each message names
    the action that would clear it."""
    disabled = check_publishable(make_clip(rights=attestation()), publishing_enabled=False, now=NOW)
    assert disabled is not None
    assert "CLIPFORGE_PUBLISHING_ENABLED" in disabled.message

    unattested = check_publishable(make_clip(rights=None), publishing_enabled=True, now=NOW)
    assert unattested is not None
    assert "Record why" in unattested.message
