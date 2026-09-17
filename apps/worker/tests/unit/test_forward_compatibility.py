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

import json
from datetime import UTC, datetime

import pytest
from clipforge.store.firestore import _read
from clipforge_contracts import Clip, ClipLocation, Job, ReviewState
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


def job_document(**options: object) -> dict[str, object]:
    """A MUSIC job document as Firestore holds it, with nested music options."""
    return {
        "id": "job-1",
        "uid": "user-1",
        "type": "MUSIC",
        "status": "QUEUED",
        "clipId": "clip-1",
        "stages": [
            {"name": "MUSIC", "lane": "CPU", "status": "PENDING"},
        ],
        "attempts": 0,
        "maxAttempts": 1,
        "createdAt": NOW.isoformat(),
        "updatedAt": NOW.isoformat(),
        "musicOptions": {
            "source": "https://youtu.be/kdQJnqHGI8c",
            "mode": "REPLACE",
            "captions": "REMOVE",
            "alignToBeat": True,
            "rights": {"basis": "PUBLIC_DOMAIN", "attestedBy": "u1"},
            **options,
        },
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


# ─────────────────────────────────────────────────────────────────────────────
# Fields that were removed, in documents that still carry them
# ─────────────────────────────────────────────────────────────────────────────
#
# The mirror image of version skew, and the one that arrives on the day a field
# is deleted rather than months later. Every clip, job and publication already
# in Firestore when `rights` was removed still holds it, and those documents
# have to keep reading.


def test_a_field_removed_from_the_schema_is_dropped_like_any_other() -> None:
    """Live documents written before the removal. There is no migration."""
    clip = _read(
        Clip,
        document(rights={"basis": "PUBLIC_DOMAIN", "attestedBy": "u1", "attestedAt": None}),
    )
    assert clip.id == "clip-1"
    assert not hasattr(clip, "rights")


def test_a_removed_field_nested_inside_an_object_takes_only_itself() -> None:
    """The bug this guards: stripping `musicOptions.rights` once took all of it.

    Reading only the first path segment meant an unknown field *inside* a nested
    object removed the whole object. A MUSIC job would then arrive with no
    options at all and fail claiming it had never been given any — a lie about a
    document that was merely older than the code.
    """
    job = _read(Job, job_document())
    assert job.music_options is not None, "the whole options block was thrown away"
    assert job.music_options.source == "https://youtu.be/kdQJnqHGI8c"
    assert job.music_options.mode.value == "REPLACE"
    assert job.music_options.captions.value == "REMOVE"


def test_unknown_fields_at_two_depths_at_once_are_both_dropped() -> None:
    job = _read(Job, job_document() | {"inventedLater": True})

    assert job.music_options is not None
    assert job.music_options.mode.value == "REPLACE"


def test_pruning_does_not_mutate_the_document_it_was_given() -> None:
    """`data` is the dict the Firestore client handed us; it is not ours."""
    original = job_document()
    before = json.dumps(original, sort_keys=True)

    _read(Job, original)

    assert json.dumps(original, sort_keys=True) == before
