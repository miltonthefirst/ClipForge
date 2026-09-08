"""Transcript persistence: the artefact on disk, the reference in Firestore.

The split is forced by Firestore's 1 MiB document limit. A 60-minute word-level
transcript is roughly 9,000 words with four fields each, which comes
uncomfortably close — and the PWA never needs one whole anyway, because
`Candidate.transcriptExcerpt` carries the part a reviewer actually reads.

So the transcript lives on the worker and Firestore keeps a `TranscriptRef`:
language, duration, counts and a path. See
docs/adr/0009-spark-tier-local-artefacts.md.

Caching is keyed on ``(contentHash, modelVersion)`` rather than on the source id.
The same video re-submitted under a different URL is a different source but
identical bytes, and re-transcribing it would cost real GPU minutes for an
identical result.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clipforge_contracts import Transcript, TranscriptRef
from google.cloud import firestore

from clipforge.config import Settings
from clipforge.observability import get_logger
from clipforge.store.firestore import SOURCES, _to_document, _unwrap_enums

log = get_logger(__name__)

__all__ = ["TranscriptArchive", "TranscriptStore"]

TRANSCRIPTS = "transcripts"


def _safe(value: str) -> str:
    """Make a model version usable as a filename.

    Versions contain colons (`faster-whisper:large-v3-turbo:int8_float16`), which
    are illegal in Windows paths and silently truncate on some tools.
    """
    return "".join(c if c.isalnum() or c in "-._" else "-" for c in value)


class TranscriptArchive:
    """The transcript and its voice-activity map, on disk."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def paths_for(self, content_hash: str, model_version: str) -> tuple[Path, Path]:
        # Sharded by content hash so a workspace with thousands of transcripts
        # does not put thousands of entries in one directory.
        directory = self._root / content_hash[:2] / content_hash
        stem = _safe(model_version)
        return directory / f"{stem}.json", directory / f"{stem}.vad.json"

    def exists(self, content_hash: str, model_version: str) -> bool:
        transcript_path, _ = self.paths_for(content_hash, model_version)
        return transcript_path.is_file()

    def save(
        self,
        transcript: Transcript,
        speech_spans: tuple[tuple[float, float], ...],
        *,
        content_hash: str,
    ) -> tuple[Path, Path]:
        transcript_path, vad_path = self.paths_for(content_hash, transcript.model_version)
        transcript_path.parent.mkdir(parents=True, exist_ok=True)

        _write_atomic(
            transcript_path,
            json.dumps(transcript.model_dump(by_alias=True, mode="json"), indent=1),
        )
        _write_atomic(
            vad_path,
            json.dumps({"speechSpans": [list(span) for span in speech_spans]}, indent=1),
        )
        return transcript_path, vad_path

    def load(self, content_hash: str, model_version: str) -> Transcript | None:
        transcript_path, _ = self.paths_for(content_hash, model_version)
        if not transcript_path.is_file():
            return None
        try:
            return Transcript.model_validate_json(transcript_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # A corrupt cache entry must not be fatal: the correct response is to
            # transcribe again, not to fail a job over a file we wrote ourselves.
            log.warning("transcript.cache_unreadable", path=str(transcript_path), error=str(exc))
            return None

    def load_speech_spans(
        self, content_hash: str, model_version: str
    ) -> tuple[tuple[float, float], ...]:
        _, vad_path = self.paths_for(content_hash, model_version)
        if not vad_path.is_file():
            return ()
        try:
            payload = json.loads(vad_path.read_text(encoding="utf-8"))
            return tuple((float(a), float(b)) for a, b in payload.get("speechSpans", []))
        except (OSError, ValueError, TypeError):
            return ()


def _write_atomic(path: Path, text: str) -> None:
    staging = path.with_suffix(path.suffix + ".partial")
    staging.write_text(text, encoding="utf-8", newline="\n")
    staging.replace(path)


class TranscriptStore:
    """The `TranscriptRef` half, at ``sources/{sourceId}/transcripts/{modelVersion}``."""

    def __init__(self, client: firestore.Client, settings: Settings) -> None:
        self._db = client
        self._settings = settings

    def _document(self, source_id: str, model_version: str) -> Any:
        return (
            self._db.collection(SOURCES)
            .document(source_id)
            .collection(TRANSCRIPTS)
            .document(_safe(model_version))
        )

    def get(self, source_id: str, model_version: str) -> TranscriptRef | None:
        snapshot = self._document(source_id, model_version).get()
        if not snapshot.exists:
            return None
        return TranscriptRef.model_validate(snapshot.to_dict() or {})

    def save(self, ref: TranscriptRef) -> None:
        self._document(ref.source_id, ref.model_version).set(_unwrap_enums(_to_document(ref)))

    @staticmethod
    def describe(
        *,
        transcript: Transcript,
        source_id: str,
        local_path: Path,
        vad_path: Path,
    ) -> TranscriptRef:
        return TranscriptRef(
            source_id=source_id,
            model_version=transcript.model_version,
            local_path=str(local_path),
            vad_path=str(vad_path),
            language=transcript.language,
            duration_sec=transcript.duration_sec,
            segment_count=len(transcript.segments),
            word_count=sum(len(s.words or []) for s in transcript.segments),
            created_at=datetime.now(UTC),
        )
