"""Generated music coming into the library (docs/MUSIC-PLAN.md).

What matters: the same file is one track however often it is imported, a
person's decision survives a re-import, and nothing that is not audio, or not
there, becomes a track.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from clipforge.config import Settings
from clipforge.store.firestore import TrackStore
from clipforge.tracks import import_manifest, track_id_for
from clipforge_contracts import Track, TrackStatus

pytestmark = pytest.mark.unit

ENTRY = {
    "prompt": "warm lo-fi hip hop, mellow Rhodes piano, instrumental",
    "seconds": 30,
    "model": "ACE-Step 1.5 acestep-v15-turbo (int8)",
    "licence": "MIT; the model card permits commercial use of the output",
    "seed": 1234,
    "steps": 8,
    "generateSeconds": 9.6,
}


class MemoryStore:
    def __init__(self) -> None:
        self.tracks: dict[str, Track] = {}

    def upsert(self, track: Track) -> bool:
        new = track.id not in self.tracks
        self.tracks[track.id] = track
        return new


def manifest_with(tmp_path: Path, files: dict[str, bytes], **extra: Any) -> Path:
    entries = []
    for name, content in files.items():
        path = tmp_path / "out" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        entries.append({**ENTRY, "file": str(path), **extra})
    manifest = tmp_path / "out" / "manifest.json"
    manifest.write_text(json.dumps({"tracks": entries}), encoding="utf-8")
    return manifest


def test_a_track_is_copied_into_the_music_folder_and_recorded(tmp_path: Path) -> None:
    manifest = manifest_with(tmp_path, {"a.wav": b"RIFF-one"})
    store = MemoryStore()

    result = import_manifest(manifest, tmp_path / "music", store)

    assert len(result.added) == 1
    track = store.tracks[result.added[0]]
    assert track.status == TrackStatus.NEW
    assert Path(track.local_path).parent == (tmp_path / "music" / "generated").resolve()
    assert Path(track.local_path).read_bytes() == b"RIFF-one"
    assert track.seconds == 30
    assert "30 s" in track.title


def test_the_same_file_twice_is_one_track(tmp_path: Path) -> None:
    manifest = manifest_with(tmp_path, {"a.wav": b"RIFF-same"})
    store = MemoryStore()

    first = import_manifest(manifest, tmp_path / "music", store)
    second = import_manifest(manifest, tmp_path / "music", store)

    assert first.added == second.updated
    assert second.added == []
    assert len(store.tracks) == 1


def test_the_id_follows_the_bytes_not_the_name(tmp_path: Path) -> None:
    one, two = tmp_path / "x.wav", tmp_path / "y.wav"
    one.write_bytes(b"same")
    two.write_bytes(b"same")
    assert track_id_for(one) == track_id_for(two)
    assert track_id_for(one).startswith("trk_")


def test_what_is_missing_or_not_audio_is_skipped(tmp_path: Path) -> None:
    manifest = manifest_with(tmp_path, {"notes.txt": b"hello"})
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["tracks"].append({**ENTRY, "file": str(tmp_path / "gone.wav")})
    manifest.write_text(json.dumps(data), encoding="utf-8")

    result = import_manifest(manifest, tmp_path / "music", MemoryStore())

    assert result.added == []
    assert len(result.skipped) == 2


def test_a_re_import_keeps_what_a_person_decided() -> None:
    """The store never writes status, decidedAt, decidedBy or createdAt over
    a document that has them."""
    written: list[tuple[dict[str, Any], bool]] = []

    class Snapshot:
        exists = True

    class Ref:
        def get(self) -> Snapshot:
            return Snapshot()

        def set(self, document: dict[str, Any], merge: bool = False) -> None:
            written.append((document, merge))

    class Collection:
        def document(self, _id: str) -> Ref:
            return Ref()

    class Client:
        def collection(self, _name: str) -> Collection:
            return Collection()

    store = TrackStore(Client(), Settings(use_emulators=True))  # type: ignore[arg-type]
    track = Track(
        id="trk_1",
        title="Lo-fi (30 s)",
        prompt="lo-fi",
        seconds=30,
        instrumental=True,
        model="m",
        licence="MIT",
        local_path="P:/music/generated/trk_1.wav",
        status=TrackStatus.NEW,
        created_at="2026-11-15T12:00:00Z",
    )

    assert store.upsert(track) is False
    document, merge = written[0]
    assert merge is True
    for kept in ("status", "decidedAt", "decidedBy", "createdAt"):
        assert kept not in document
    assert document["title"] == "Lo-fi (30 s)"
