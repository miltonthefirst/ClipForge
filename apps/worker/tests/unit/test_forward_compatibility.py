"""Reading a document written by a newer version of the contracts.

A live failure, reproduced. `lineageId` and `version` were added to `Clip` and
backfilled onto every existing document while a worker six hours older was
running. The contracts set `extra="forbid"`, so that worker rejected all
seventeen clips with a pydantic error naming the new fields — which reads like
corruption rather than like version skew, and it took a process restart to fix
something that was never actually broken.

The rule that replaces it: **tolerate additions, refuse changes.**
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from clipforge.store.firestore import _read
from clipforge_contracts import Clip, ClipLocation, ReviewState
from pydantic import ValidationError

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 13, tzinfo=UTC)


def document(**extra: object) -> dict[str, object]:
    """A clip document as Firestore holds it."""
    return {
        "id": "clip-1",
        "uid": "user-1",
        "candidateId": "cand-1",
        "sourceId": "src-1",
        "location": "LOCAL",
        "localPath": "x.mp4",
        "review": "PENDING",
        "createdAt": NOW.isoformat(),
        **extra,
    }


def test_a_field_this_worker_has_never_heard_of_is_dropped() -> None:
    """The exact shape of the failure: a newer writer, an older reader.

    `somethingAddedLater` stands in for whatever the next schema change adds.
    The document is valid; this process is simply behind, and behind is not the
    same as broken.
    """
    clip = _read(Clip, document(somethingAddedLater="whatever", andAnother=3))
    assert clip.id == "clip-1"
    assert clip.review is ReviewState.PENDING
    assert clip.location is ClipLocation.LOCAL


def test_the_fields_it_does_know_survive_the_drop() -> None:
    clip = _read(Clip, document(lineageId="root-1", version=3, futureThing=True))
    assert clip.lineage_id == "root-1"
    assert clip.version == 3


def test_an_ordinary_document_is_untouched() -> None:
    clip = _read(Clip, document())
    assert clip.id == "clip-1"


def test_a_missing_required_field_still_raises() -> None:
    """Refuse changes. A document without an id is wrong, not merely newer."""
    broken = document()
    del broken["id"]
    with pytest.raises(ValidationError):
        _read(Clip, broken)


def test_a_field_of_the_wrong_type_still_raises() -> None:
    with pytest.raises(ValidationError):
        _read(Clip, document(version="not a number"))


def test_a_real_error_beside_an_unknown_field_still_raises() -> None:
    """The original error is raised, not a second one from a stripped retry.

    Stripping first and re-validating would report the missing field and say
    nothing about the unknown one, describing the symptom instead of the cause.
    """
    broken = document(somethingNew="x")
    del broken["uid"]
    with pytest.raises(ValidationError) as caught:
        _read(Clip, broken)
    assert "uid" in str(caught.value)
