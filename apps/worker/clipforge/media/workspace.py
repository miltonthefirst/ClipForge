"""The workspace: where source media lives, and how it stops filling the disk.

Sources are the largest thing ClipForge touches — a 60-minute 1080p video is
1-3 GB — and on the free tier rendered clips never leave the machine either. So
garbage collection is not a nicety here, it is the only thing standing between a
long run and a full disk.

The policy is deliberately simple and deliberately conservative:

**Least-recently-used, and only sources.** Clips are the output; deleting one
would destroy work the user has not reviewed yet. Only downloaded source media is
reclaimable, because it can always be fetched again.

**Never evict what is in use or pinned.** A source belonging to a running job, or
explicitly pinned, is skipped even if that means failing to reach the target.
Reporting a shortfall is safer than deleting the file a stage is mid-read on.

**Report, do not raise, when the target cannot be met.** A GC pass that cannot
free enough space is a fact the caller should act on, not an exception thrown
from underneath an unrelated stage.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = ["EvictionCandidate", "GcOutcome", "Workspace"]

BYTES_PER_GB = 1024**3


@dataclass(frozen=True)
class EvictionCandidate:
    """A source file the GC may reclaim."""

    source_id: str
    path: Path
    size_bytes: int
    last_accessed_at: datetime
    pinned: bool = False
    in_use: bool = False

    @property
    def reclaimable(self) -> bool:
        return not self.pinned and not self.in_use


@dataclass
class GcOutcome:
    """What a pass actually achieved."""

    freed_bytes: int = 0
    evicted: list[str] = field(default_factory=list)
    skipped_pinned: list[str] = field(default_factory=list)
    skipped_in_use: list[str] = field(default_factory=list)
    target_met: bool = True

    @property
    def freed_gb(self) -> float:
        return round(self.freed_bytes / BYTES_PER_GB, 3)


class Workspace:
    """Owns the on-disk layout and the disk budget."""

    def __init__(self, root: Path, *, max_gb: int) -> None:
        self._root = root.expanduser().resolve()
        self._max_bytes = max_gb * BYTES_PER_GB
        for child in ("sources", "clips", "transcripts", "music", "tmp", "trash"):
            (self._root / child).mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def sources_dir(self) -> Path:
        return self._root / "sources"

    @property
    def clips_dir(self) -> Path:
        return self._root / "clips"

    @property
    def transcripts_dir(self) -> Path:
        return self._root / "transcripts"

    @property
    def music_dir(self) -> Path:
        """Tracks fetched to score clips with.

        Its own directory rather than `tmp`, which `clear_tmp` empties at every
        worker start — so a bed the reviewer chose yesterday was downloaded
        again today, and the cache that was supposed to make a remake cheap
        never survived a restart. Beside `sources` rather than inside it because
        the GC walks that one by source id, and a track is a source of a
        different kind.
        """
        return self._root / "music"

    @property
    def tmp_dir(self) -> Path:
        return self._root / "tmp"

    @property
    def trash_dir(self) -> Path:
        """Where files go when the operator removes them. See media/trash.py."""
        return self._root / "trash"

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    # ── Measurement ──────────────────────────────────────────────────────────

    def used_bytes(self) -> int:
        """Total bytes under the workspace root.

        Walks rather than caching. The workspace is also written to by ffmpeg and
        yt-dlp, which do not report back, so a cached figure would drift and the
        drift would always be in the dangerous direction.
        """
        total = 0
        trash = self.trash_dir
        for path in self._root.rglob("*"):
            if not path.is_file():
                continue
            if trash in path.parents:
                # The bin is deliberately outside the budget, and this is the
                # line that makes it so. Counting it would be more truthful
                # about the disk and catastrophic in practice: `collect` evicts
                # SOURCES, so a full bin would have it delete live downloads to
                # make room for deleted ones. Live data is never evicted to
                # house dead data.
                #
                # The price is that the bin can fill a disk while this reports
                # the workspace comfortable, so its size is shown wherever the
                # bin is shown and emptying it stays the operator's call.
                continue
            try:
                total += path.stat().st_size
            except OSError:
                # A file vanished mid-walk — another stage cleaning up. Not
                # worth failing a GC pass over.
                continue
        return total

    def free_disk_bytes(self) -> int:
        return shutil.disk_usage(self._root).free

    def over_budget_by(self) -> int:
        """Bytes to reclaim to get back under the cap. Zero when comfortable."""
        return max(0, self.used_bytes() - self._max_bytes)

    # ── Collection ───────────────────────────────────────────────────────────

    def collect(
        self,
        candidates: Iterable[EvictionCandidate],
        *,
        need_bytes: int | None = None,
        now: datetime | None = None,
    ) -> GcOutcome:
        """Reclaim space, oldest access first.

        ``need_bytes`` defaults to whatever the cap demands. Passing it
        explicitly is how a stage asks for headroom *before* starting a download
        it already knows the size of — which is the only way to avoid failing
        halfway through a 2 GB fetch.
        """
        del now  # Accepted for symmetry with the rest of the codebase's clocks.
        target = self.over_budget_by() if need_bytes is None else need_bytes
        outcome = GcOutcome(target_met=target <= 0)
        if target <= 0:
            return outcome

        ordered: Sequence[EvictionCandidate] = sorted(candidates, key=lambda c: c.last_accessed_at)

        for candidate in ordered:
            if candidate.pinned:
                outcome.skipped_pinned.append(candidate.source_id)
                continue
            if candidate.in_use:
                outcome.skipped_in_use.append(candidate.source_id)
                continue

            freed = self._remove(candidate)
            if freed == 0:
                continue

            outcome.freed_bytes += freed
            outcome.evicted.append(candidate.source_id)
            log.info(
                "workspace.evicted",
                source_id=candidate.source_id,
                freed_mb=freed // (1024 * 1024),
            )

            if outcome.freed_bytes >= target:
                outcome.target_met = True
                return outcome

        outcome.target_met = outcome.freed_bytes >= target
        if not outcome.target_met:
            # Deliberately a warning, not an exception: everything left is
            # pinned or in use, and deleting either would be worse than being
            # short of space.
            log.warning(
                "workspace.gc_shortfall",
                needed_mb=target // (1024 * 1024),
                freed_mb=outcome.freed_bytes // (1024 * 1024),
                pinned=len(outcome.skipped_pinned),
                in_use=len(outcome.skipped_in_use),
            )
        return outcome

    def _remove(self, candidate: EvictionCandidate) -> int:
        try:
            if not candidate.path.is_file():
                return 0
            size = candidate.path.stat().st_size
            candidate.path.unlink()
        except OSError as exc:
            log.warning("workspace.evict_failed", source_id=candidate.source_id, error=str(exc))
            return 0
        return size

    def clear_tmp(self) -> int:
        """Empty the scratch directory. Called at startup: anything in there
        belongs to a run that is already over."""
        freed = 0
        for path in self.tmp_dir.rglob("*"):
            if path.is_file():
                try:
                    freed += path.stat().st_size
                    path.unlink()
                except OSError:
                    continue
        return freed

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)
