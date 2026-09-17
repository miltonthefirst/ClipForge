"""Collecting the clips a review decision has already settled.

## Why the reviewer should not have to do this

A decision is about the video, not about the attempt. Approving the fifth cut
of a clip is a statement about all five: the first four were the route to it.
Rejecting it rejects the idea, not the latest render of the idea.

The PWA writes that down — `supersededAt` on the versions a decision passed
over, `REJECTED` across a lineage that was turned down — and stops there,
because removing a record and removing 400 MB of video are different acts with
different costs (see :mod:`clipforge.media.trash`). This is where the second
one happens, on the machine that actually holds the files.

## Why superseded clips wait and rejected ones do not

A rejection is a judgement someone made deliberately about the finished thing.
Being superseded is a side effect of approving something else, and the reviewer
never looked at it in that moment — so it keeps a grace period, and
``CLIPFORGE_SUPERSEDED_GRACE_DAYS`` is how long there is to change your mind.
Both end the same way: the record goes, and the file goes to the bin rather
than to nothing, because the bin is the part of this that is reversible.

## What is never collected

A clip that was published, or is the newest version of its lineage, or has no
decision recorded at all. The rule is deliberately conservative: this runs
unattended, and the cost of keeping a clip too long is disk, while the cost of
collecting one too early is a render nobody can get back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from clipforge_contracts import Clip, ReviewState

from clipforge.media.trash import Trash, TrashError
from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = ["TidyReport", "collectable", "tidy_reviewed_clips"]


@dataclass
class TidyReport:
    """What one pass did, in terms a log line can carry."""

    rejected: int = 0
    superseded: int = 0
    binned_bytes: int = 0
    kept_newest: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.rejected + self.superseded


def collectable(clip: Clip, *, now: datetime, grace_days: int, newest_ids: set[str]) -> bool:
    """Whether this clip's decision has been made and has finished waiting.

    Split out from the pass itself so the rule can be tested without a store, a
    bin, or a clock — the three things that make a deletion hard to write a test
    for and easy to get wrong.
    """
    # The survivor of a lineage is the decision, not a casualty of it. Approving
    # v5 must never collect v5, and rejecting a lineage still leaves its newest
    # version for the operator to see what they turned down.
    if clip.id in newest_ids:
        return False

    if clip.review is ReviewState.REJECTED:
        return True

    if clip.superseded_at is None:
        return False

    # Aware by contract: Clip rejects a naive datetime at construction, so
    # there is no half-hour of drift to guard against here.
    return now - clip.superseded_at >= timedelta(days=grace_days)


def tidy_reviewed_clips(
    clips: list[Clip],
    *,
    trash: Trash,
    delete_record: object,
    now: datetime | None = None,
    grace_days: int = 7,
) -> TidyReport:
    """Bin the files and drop the records of clips whose decision is settled.

    `delete_record` is a callable taking a clip id, rather than a store, so this
    can be driven against a fake without one — and so the caller keeps the
    choice of whether a failure to delete a record should stop the pass.

    The file goes first. A record removed before its file is a file nobody can
    find again; a file binned before its record is a record pointing at the bin,
    which the next pass tidies anyway.
    """
    now = now or datetime.now(UTC)
    report = TidyReport()

    newest = _newest_per_lineage(clips)
    report.kept_newest = len(newest)

    for clip in clips:
        if not collectable(clip, now=now, grace_days=grace_days, newest_ids=newest):
            continue

        try:
            path = Path(clip.local_path) if clip.local_path else None
            if path is not None and path.is_file():
                item = trash.put(path, kind="clip", record_id=clip.id)
                report.binned_bytes += item.size_bytes
            delete_record(clip.id)  # type: ignore[operator]
        except (OSError, TrashError) as exc:
            # One clip that will not move is not a reason to leave the rest.
            report.failures.append(f"{clip.id}: {exc}")
            log.warning("tidy.clip_failed", clip=clip.id, error=str(exc)[:200])
            continue

        if clip.review is ReviewState.REJECTED:
            report.rejected += 1
        else:
            report.superseded += 1

    log.info(
        "tidy.done",
        rejected=report.rejected,
        superseded=report.superseded,
        freed_mb=round(report.binned_bytes / 1_048_576, 1),
        failures=len(report.failures),
    )
    return report


def _newest_per_lineage(clips: list[Clip]) -> set[str]:
    """The id of the highest version in each lineage.

    A clip written before lineages existed is its own root, which is the same
    rule the PWA's queue uses — the two must agree or a clip is either collected
    while it is still on screen or shown after it was collected.
    """
    best: dict[str, Clip] = {}
    for clip in clips:
        key = clip.lineage_id or clip.id
        held = best.get(key)
        if held is None or (clip.version or 1) > (held.version or 1):
            best[key] = clip
    return {clip.id for clip in best.values()}
