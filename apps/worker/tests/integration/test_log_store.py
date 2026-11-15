"""Error groups in the Firestore emulator: one group per problem, a count, the
newest 25 occurrences, and a resolved group reopened by a recurrence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from clipforge.logs.store import GROUPS, KEEP_OCCURRENCES, LogStore
from google.cloud import firestore

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def test_the_same_problem_is_one_group_with_a_count(client: firestore.Client) -> None:
    store = LogStore(client)
    first = store.ingest("worker", "error", "ffmpeg exited 1 on job 123", now=T0)
    second = store.ingest(
        "worker", "error", "ffmpeg exited 1 on job 456", now=T0 + timedelta(seconds=1)
    )

    assert first.is_new and not second.is_new
    assert first.fingerprint == second.fingerprint
    group = client.collection(GROUPS).document(first.fingerprint).get().to_dict() or {}
    assert group["count"] == 2
    assert group["status"] == "unresolved"


def test_a_resolved_group_is_reopened_by_a_recurrence(client: firestore.Client) -> None:
    store = LogStore(client)
    fp = store.ingest("agent", "error", "listener died", now=T0).fingerprint
    client.collection(GROUPS).document(fp).update(
        {"status": "resolved", "resolvedAt": T0, "resolvedBy": "admin"}
    )

    again = store.ingest("agent", "error", "listener died", now=T0 + timedelta(minutes=1))

    assert again.reopened
    group = client.collection(GROUPS).document(fp).get().to_dict() or {}
    assert group["status"] == "unresolved" and group["resolvedBy"] is None


def test_only_the_newest_occurrences_are_kept(client: firestore.Client) -> None:
    store = LogStore(client)
    fp = ""
    for n in range(KEEP_OCCURRENCES + 5):
        fp = store.ingest("worker", "error", "disk full", now=T0 + timedelta(seconds=n)).fingerprint

    kept = list(client.collection(GROUPS).document(fp).collection("occurrences").stream())
    assert len(kept) == KEEP_OCCURRENCES
    oldest = min((d.to_dict() or {})["timestamp"] for d in kept)
    assert oldest == T0 + timedelta(seconds=5)
