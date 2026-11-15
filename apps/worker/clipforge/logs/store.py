"""Error groups in Firestore, at ``logGroups/{fingerprint}`` (the logging blueprint).

Written by the worker and the agent through the Admin SDK. ClipForge has no
Cloud Functions, so this is the blueprint's ``ingestLogEntry`` running in the
process that had the error, as Sarungano's Python store does with SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from clipforge.logs.fingerprint import compute_log_fingerprint

__all__ = ["GROUPS", "KEEP_OCCURRENCES", "Ingested", "LogStore"]

GROUPS = "logGroups"
OCCURRENCES = "occurrences"
KEEP_OCCURRENCES = 25
MESSAGE_MAX = 2000
# A Python traceback keeps its tail: the raising frame and the exception line
# are at the end (the blueprint, 1.2.0).
STACK_MAX = 8000


@dataclass(frozen=True)
class Ingested:
    fingerprint: str
    is_new: bool
    reopened: bool
    watched: bool
    count: int


class LogStore:
    def __init__(self, client: Any) -> None:
        self._db = client

    def ingest(
        self,
        app: str,
        level: str,
        message: str,
        *,
        stack: str | None = None,
        category: str | None = None,
        extra: dict[str, str] | None = None,
        now: datetime | None = None,
    ) -> Ingested:
        from google.cloud import firestore as gcf

        now = now or datetime.now(UTC)
        message = message[:MESSAGE_MAX]
        stack = stack[-STACK_MAX:] if stack else None
        fingerprint = compute_log_fingerprint(level, app, message, stack)
        group_ref = self._db.collection(GROUPS).document(fingerprint)
        occurrence = {"timestamp": now, "extra": dict(extra or {})}

        @gcf.transactional
        def write(transaction: Any) -> Ingested:
            snapshot = group_ref.get(transaction=transaction)
            existing = snapshot.to_dict() if snapshot.exists else None
            if existing is None:
                transaction.set(
                    group_ref,
                    {
                        "fingerprint": fingerprint,
                        "app": app,
                        "level": level,
                        "category": category,
                        "message": message,
                        "stack": stack,
                        "count": 1,
                        "firstSeenAt": now,
                        "lastSeenAt": now,
                        "lastOccurrence": occurrence,
                        "status": "unresolved",
                        "resolvedAt": None,
                        "resolvedBy": None,
                        "watched": False,
                    },
                )
                result = Ingested(fingerprint, True, False, False, 1)
            else:
                reopened = existing.get("status") == "resolved"
                count = int(existing.get("count") or 0) + 1
                changes: dict[str, Any] = {
                    "count": count,
                    "lastSeenAt": now,
                    "lastOccurrence": occurrence,
                    "stack": stack or existing.get("stack"),
                }
                if reopened:
                    changes.update({"status": "unresolved", "resolvedAt": None, "resolvedBy": None})
                transaction.update(group_ref, changes)
                result = Ingested(
                    fingerprint, False, reopened, bool(existing.get("watched")), count
                )
            occurrence_ref = group_ref.collection(OCCURRENCES).document()
            transaction.set(occurrence_ref, {**occurrence, "id": occurrence_ref.id})
            return result

        result: Ingested = write(self._db.transaction())
        self._trim(group_ref)
        return result

    def _trim(self, group_ref: Any) -> None:
        """Keep the newest 25 occurrences. By offset, never `count - cap`:
        that was the blueprint's bug that wiped the whole history."""
        from google.cloud import firestore as gcf

        older = (
            group_ref.collection(OCCURRENCES)
            .order_by("timestamp", direction=gcf.Query.DESCENDING)
            .offset(KEEP_OCCURRENCES)
            .limit(50)
        )
        for snapshot in older.stream():
            snapshot.reference.delete()
