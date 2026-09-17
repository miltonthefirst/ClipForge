"""Keeping score of the tracks a reviewer scores clips with.

The counting is what makes the Sources list orderable and what decides which
downloads survive the collector, so the two things worth pinning down are that
the same track is one record, and that a failure to count never costs a clip.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from clipforge.media.library import music_source_id, remember_music
from clipforge_contracts import Source, SourceKind, SourceProvider

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
TRACK = "https://www.youtube.com/watch?v=kdQJnqHGI8c"


class FakeSources:
    def __init__(self) -> None:
        self.saved: dict[str, Source] = {}
        self.uses: list[str] = []

    def get(self, source_id: str) -> Source | None:
        return self.saved.get(source_id)

    def save(self, source: Source) -> None:
        self.saved[source.id] = source

    def record_use(self, source_id: str, *, now: datetime | None = None) -> None:
        self.uses.append(source_id)


def track_file(tmp_path: Path) -> Path:
    path = tmp_path / "music-kdQJnqHGI8c.m4a"
    path.write_bytes(b"\x00" * 4096)
    return path


def test_the_same_track_twice_is_one_source_used_twice(tmp_path: Path) -> None:
    """Not two records with one use each.

    The id is derived rather than generated precisely so a second fetch finds
    the first record — two jobs can score two clips with the same bed in the
    same second, and the CPU lane runs three deep.
    """
    sources = FakeSources()
    path = track_file(tmp_path)

    first = remember_music(sources, uid="u", reference=TRACK, path=path, title="Slow Burn", now=NOW)
    second = remember_music(sources, uid="u", reference=TRACK, path=path, title=None, now=NOW)

    assert first == second
    assert len(sources.saved) == 1
    assert sources.uses == [first], "the second fetch should count as a use, not a new record"


def test_a_track_is_recorded_as_music_and_already_used_once(tmp_path: Path) -> None:
    """Created by being used, so zero would be wrong the moment it is written."""
    sources = FakeSources()
    source_id = remember_music(
        sources, uid="u", reference=TRACK, path=track_file(tmp_path), title="Slow Burn", now=NOW
    )

    saved = sources.saved[str(source_id)]
    assert saved.kind is SourceKind.MUSIC
    assert saved.use_count == 1
    assert saved.provider is SourceProvider.YOUTUBE
    assert saved.title == "Slow Burn"
    assert saved.size_bytes == 4096


def test_a_file_the_reviewer_typed_is_recorded_as_local(tmp_path: Path) -> None:
    sources = FakeSources()
    path = track_file(tmp_path)
    source_id = remember_music(
        sources, uid="u", reference=str(path), path=path, title="bed", now=NOW
    )

    assert sources.saved[str(source_id)].provider is SourceProvider.LOCAL


def test_two_reviewers_using_one_track_keep_separate_records(tmp_path: Path) -> None:
    """The id is scoped to the uid, like every other source in the system."""
    assert music_source_id("one", TRACK) != music_source_id("two", TRACK)


def test_a_store_that_will_not_write_does_not_cost_the_clip(tmp_path: Path) -> None:
    """Bookkeeping is not worth failing a mix that already succeeded.

    By the time this runs the track is fetched, the music is mixed and the file
    is written — raising here would throw all of it away over a counter.
    """

    class Broken:
        def get(self, source_id: str) -> Source | None:
            raise RuntimeError("firestore is unreachable")

    assert (
        remember_music(
            Broken(), uid="u", reference=TRACK, path=track_file(tmp_path), title=None, now=NOW
        )
        is None
    )
