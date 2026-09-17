"""Collecting clips a decision has already settled.

Every test here is about *not* collecting something. That is the asymmetry the
module is built around: keeping a clip too long costs disk, and collecting one
too early costs a render that cannot be made again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from clipforge.media.trash import Trash
from clipforge.scheduler.tidy import collectable, tidy_reviewed_clips
from clipforge_contracts import Clip, ClipLocation, ReviewState

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def clip(
    clip_id: str,
    *,
    review: ReviewState = ReviewState.PENDING,
    superseded_at: datetime | None = None,
    lineage: str | None = None,
    version: int = 1,
    local_path: str = "nowhere.mp4",
) -> Clip:
    return Clip(
        id=clip_id,
        uid="u",
        candidate_id="c",
        location=ClipLocation.LOCAL,
        review=review,
        superseded_at=superseded_at,
        lineage_id=lineage,
        version=version,
        local_path=local_path,
        created_at=NOW,
    )


# ── The rule ─────────────────────────────────────────────────────────────────


def test_a_rejected_clip_is_collected_without_waiting() -> None:
    """Somebody looked at it and said no. There is nothing to reconsider."""
    assert collectable(
        clip("old", review=ReviewState.REJECTED, lineage="L", version=1),
        now=NOW,
        grace_days=7,
        newest_ids={"new"},
    )


def test_a_superseded_clip_waits_out_its_grace_period() -> None:
    """Nobody judged this one; it lost to a later cut while they were looking
    at something else. The wait is what makes that reversible."""
    fresh = clip("v1", superseded_at=NOW - timedelta(days=2), lineage="L", version=1)
    stale = clip("v1", superseded_at=NOW - timedelta(days=8), lineage="L", version=1)

    assert not collectable(fresh, now=NOW, grace_days=7, newest_ids={"v2"})
    assert collectable(stale, now=NOW, grace_days=7, newest_ids={"v2"})


def test_the_newest_version_is_never_collected() -> None:
    """The survivor is the decision, not a casualty of it.

    This is the assertion that stops an approval deleting the clip it approved,
    and stops a rejected lineage vanishing before the reviewer can see what they
    turned down.
    """
    newest = clip("v5", review=ReviewState.REJECTED, lineage="L", version=5)
    assert not collectable(newest, now=NOW, grace_days=7, newest_ids={"v5"})


def test_a_clip_nobody_has_decided_about_is_left_alone() -> None:
    assert not collectable(clip("v1", lineage="L"), now=NOW, grace_days=7, newest_ids={"v2"})


def test_an_approved_clip_is_not_collected_by_being_old() -> None:
    """APPROVED is the one state that means keep."""
    old = clip("v1", review=ReviewState.APPROVED, lineage="L", version=1)
    assert not collectable(old, now=NOW, grace_days=0, newest_ids={"v2"})


# ── The pass ─────────────────────────────────────────────────────────────────


def test_the_file_goes_to_the_bin_and_the_record_goes_away(tmp_path: Path) -> None:
    """Both halves, in that order: a record removed first is a file nobody can
    find again."""
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"\x00" * 2048)
    deleted: list[str] = []

    report = tidy_reviewed_clips(
        [
            clip("v1", review=ReviewState.REJECTED, lineage="L", version=1, local_path=str(media)),
            clip("v2", review=ReviewState.REJECTED, lineage="L", version=2),
        ],
        trash=Trash(tmp_path / "bin"),
        delete_record=deleted.append,
        now=NOW,
        grace_days=7,
    )

    assert deleted == ["v1"]
    assert report.rejected == 1
    assert report.binned_bytes == 2048
    assert not media.exists(), "the file was deleted rather than binned"
    assert len(Trash(tmp_path / "bin").items()) == 1


def test_a_clip_whose_file_is_already_gone_still_loses_its_record(tmp_path: Path) -> None:
    """The workspace collector may have taken it first. That is not a reason to
    keep a record pointing at nothing."""
    deleted: list[str] = []
    tidy_reviewed_clips(
        [
            clip(
                "v1",
                review=ReviewState.REJECTED,
                lineage="L",
                version=1,
                local_path=str(tmp_path / "gone.mp4"),
            ),
            clip("v2", lineage="L", version=2),
        ],
        trash=Trash(tmp_path / "bin"),
        delete_record=deleted.append,
        now=NOW,
    )
    assert deleted == ["v1"]


def test_one_clip_that_will_not_move_does_not_strand_the_rest(tmp_path: Path) -> None:
    deleted: list[str] = []

    def refuse(clip_id: str) -> None:
        if clip_id == "v1":
            raise OSError("in use by another process")
        deleted.append(clip_id)

    report = tidy_reviewed_clips(
        [
            clip("v1", review=ReviewState.REJECTED, lineage="A", version=1),
            clip("a2", lineage="A", version=2),
            clip("v3", review=ReviewState.REJECTED, lineage="B", version=1),
            clip("b2", lineage="B", version=2),
        ],
        trash=Trash(tmp_path / "bin"),
        delete_record=refuse,
        now=NOW,
    )

    assert deleted == ["v3"]
    assert len(report.failures) == 1
    assert "v1" in report.failures[0]


def test_a_lineage_of_one_is_never_collected(tmp_path: Path) -> None:
    """It is its own newest version, so there is nothing it lost to."""
    deleted: list[str] = []
    tidy_reviewed_clips(
        [clip("only", review=ReviewState.REJECTED)],
        trash=Trash(tmp_path / "bin"),
        delete_record=deleted.append,
        now=NOW,
    )
    assert deleted == []
