"""Keeping score of the media on this machine.

A source earns its place by being used. That is the whole idea here: the
workspace has a budget, the collector has to be able to reclaim something, and
the only honest signal for what a reviewer will want again is what they have
wanted before.

## Why music is here at all

It was not, until now. :mod:`clipforge.media.sources` says so in as many words —
a track "is not clipped, not transcribed, and not tracked; it is fetched, used,
and left in tmp for the workspace to sweep". That reasoning held exactly as long
as nobody used the same track twice. The moment they did, the system re-fetched
a file it had deleted an hour earlier, and could not answer "which beds do I
actually use?" because nothing was counting.

## Why registration is idempotent on the external id

The same YouTube track fetched twice is one source with two uses, not two
sources with one each. The id is derived from the provider and the external id
rather than generated, so a second fetch finds the first record instead of
racing to create a sibling — which is what would happen with a random id and two
jobs in the CPU lane.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from clipforge_contracts import Source, SourceKind, SourceProvider

from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = ["music_source_id", "remember_music"]


def music_source_id(uid: str, reference: str) -> str:
    """A stable id for a track, so the same one is one record.

    Hashed rather than the reference itself: a YouTube id is safe as a document
    id but a Windows path is not, and the two have to land in the same shape.
    """
    digest = hashlib.sha256(f"{uid}:{reference}".encode()).hexdigest()
    return f"music-{digest[:24]}"


def remember_music(
    sources: object,
    *,
    uid: str,
    reference: str,
    path: Path,
    title: str | None,
    now: datetime | None = None,
) -> str | None:
    """Record that a track was used, and return its source id.

    `sources` is duck-typed rather than imported as `SourceStore`, to keep the
    media layer from depending on the store — the same reason the rest of this
    package takes paths and binaries rather than clients.

    Never raises. Keeping score is bookkeeping: a reviewer whose clip was scored
    correctly should not see it fail because a counter could not be written.
    """
    now = now or datetime.now(UTC)
    source_id = music_source_id(uid, reference)
    try:
        existing = sources.get(source_id)  # type: ignore[attr-defined]
        if existing is not None:
            sources.record_use(source_id, now=now)  # type: ignore[attr-defined]
            return source_id

        sources.save(  # type: ignore[attr-defined]
            Source(
                id=source_id,
                uid=uid,
                kind=SourceKind.MUSIC,
                provider=(
                    SourceProvider.LOCAL if Path(reference).is_file() else SourceProvider.YOUTUBE
                ),
                external_id=reference[:200],
                url=reference if reference.startswith("http") else None,
                title=title,
                local_path=str(path),
                size_bytes=path.stat().st_size if path.is_file() else None,
                # One, not zero: this call is the first use, and a track created
                # by being used has already been used once.
                use_count=1,
                last_accessed_at=now,
                created_at=now,
            )
        )
        return source_id
    except Exception as exc:  # noqa: BLE001 - bookkeeping never fails the job
        log.warning("library.music_not_recorded", reference=reference[:120], error=str(exc)[:200])
        return None
