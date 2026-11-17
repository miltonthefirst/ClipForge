"""Bringing generated music into the library (docs/MUSIC-PLAN.md).

The generator (``packages/ngoma``, its own process and environment) writes audio
files and a manifest saying what each one is: the prompt, the length, the model,
the licence, how long it took. This reads that manifest, copies each file into
the worker's ``workspace/music/generated``, and records it at ``tracks/{id}``,
where a person can listen to it and keep it or not.

The id is taken from the audio's bytes, so importing the same file twice is one
track, and a re-import never resets a decision already made about it
(:class:`clipforge.store.firestore.TrackStore`).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from clipforge_contracts import Track, TrackStatus

__all__ = ["AUDIO_SUFFIXES", "ImportResult", "import_manifest", "track_id_for"]

AUDIO_SUFFIXES = frozenset({".wav", ".flac", ".mp3", ".m4a"})


class TrackWriter(Protocol):
    def upsert(self, track: Track) -> bool: ...


@dataclass(frozen=True)
class ImportResult:
    added: list[str]
    updated: list[str]
    skipped: list[str]


def track_id_for(path: Path) -> str:
    """``trk_`` and the first 20 hex digits of the file's SHA-256."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"trk_{digest.hexdigest()[:20]}"


def import_manifest(manifest: Path, music_dir: Path, store: TrackWriter) -> ImportResult:
    """Copy and record every track the manifest lists.

    A manifest is ``{"tracks": [{...}, ...]}``; each entry needs ``file``
    (absolute, or relative to the manifest), ``prompt``, ``seconds``, ``model``
    and ``licence``, and may carry ``title``, ``bpm``, ``key``, ``instrumental``,
    ``seed``, ``steps``, ``generateSeconds``, ``series`` and ``createdAt``.
    """
    entries = json.loads(manifest.read_text(encoding="utf-8")).get("tracks", [])
    target = music_dir / "generated"
    target.mkdir(parents=True, exist_ok=True)
    added: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []

    for entry in entries:
        source = Path(str(entry.get("file", "")))
        if not source.is_absolute():
            source = manifest.parent / source
        if not source.is_file() or source.suffix.lower() not in AUDIO_SUFFIXES:
            skipped.append(str(source))
            continue
        track_id = track_id_for(source)
        destination = target / f"{track_id}{source.suffix.lower()}"
        if not destination.exists():
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            shutil.copyfile(source, temporary)
            temporary.replace(destination)
        track = _track(track_id, destination, entry)
        (added if store.upsert(track) else updated).append(track_id)

    return ImportResult(added=added, updated=updated, skipped=skipped)


def _track(track_id: str, path: Path, entry: dict[str, Any]) -> Track:
    seconds = float(entry["seconds"])
    title = str(entry.get("title") or f"{str(entry['prompt'])[:60]} ({seconds:g} s)")
    created = entry.get("createdAt")
    return Track(
        id=track_id,
        title=title[:120],
        prompt=str(entry["prompt"])[:512],
        seconds=seconds,
        bpm=_int_or_none(entry.get("bpm")),
        key=(str(entry["key"])[:20] if entry.get("key") else None),
        instrumental=bool(entry.get("instrumental", True)),
        seed=_int_or_none(entry.get("seed")),
        steps=_int_or_none(entry.get("steps")),
        model=str(entry["model"])[:120],
        licence=str(entry["licence"])[:200],
        generate_seconds=(
            float(entry["generateSeconds"]) if entry.get("generateSeconds") is not None else None
        ),
        series=(str(entry["series"])[:60] if entry.get("series") else None),
        local_path=str(path.resolve()),
        status=TrackStatus.NEW,
        created_at=datetime.fromisoformat(created) if created else datetime.now(UTC),
    )


def _int_or_none(value: Any) -> int | None:
    return int(value) if value is not None else None
