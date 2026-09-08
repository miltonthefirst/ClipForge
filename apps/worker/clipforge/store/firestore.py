"""Firestore adapter for jobs, events and worker heartbeats.

This module is deliberately thin. Every decision about *whether* a transition is
legal lives in :mod:`clipforge.scheduler.lease` as a pure function; the adapter
only reads, applies, and compare-and-swaps. It cannot get the rules wrong,
because it does not contain any.

Two things here are load-bearing and easy to get subtly wrong:

**The claim is transactional and re-checks its predicate inside the
transaction.** Firestore transactions are optimistic: if the document changed
between read and commit, the transaction *retries*. A claim that only checked
claimability before opening the transaction would therefore let a second worker
succeed on retry, and two workers would run the same job. Re-reading inside the
transaction is what makes the race safe.

**The worker uses Admin credentials, which bypass firestore.rules entirely.**
Nothing in the rules file constrains this code. That asymmetry is why ClipForge
has its own Firebase project — see docs/adr/0004-dedicated-firebase-project.md.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from clipforge_contracts import Job, JobStatus, Source, SourceProvider, WorkerHeartbeat
from google.api_core import exceptions as gcloud_exceptions
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore

from clipforge.config import Settings
from clipforge.scheduler import lease
from clipforge.scheduler.lease import Transition

JOBS = "jobs"
WORKERS = "workers"
SOURCES = "sources"
EVENTS = "events"


def firestore_client(settings: Settings) -> firestore.Client:
    """Build a Firestore client for either the emulator or the real project.

    The emulator path sets ``FIRESTORE_EMULATOR_HOST`` and uses anonymous
    credentials, so it needs no service account, no billing and no network.
    """
    if settings.use_emulators:
        os.environ["FIRESTORE_EMULATOR_HOST"] = settings.firestore_emulator_host
        return firestore.Client(
            project=settings.firebase_project_id,
            credentials=AnonymousCredentials(),  # type: ignore[no-untyped-call]
        )

    if settings.google_application_credentials:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = settings.google_application_credentials
    return firestore.Client(project=settings.firebase_project_id)


def _to_document(model: Job | WorkerHeartbeat | Source) -> dict[str, Any]:
    """Model to Firestore document.

    ``mode="python"`` rather than ``"json"`` so datetimes stay as datetimes and
    land as native Firestore timestamps. That matters: the reaper's query filters
    on ``leaseExpiresAt``, and a string comparison there would be wrong the
    moment a timezone offset differed.
    """
    document: dict[str, Any] = _unwrap_enums(model.model_dump(by_alias=True, mode="python"))
    return document


def _unwrap_enums(value: Any) -> Any:
    """StrEnum is a str subclass, so Firestore would accept it — but it would
    round-trip as the enum's repr in some client versions. Converting explicitly
    removes the ambiguity."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _unwrap_enums(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap_enums(v) for v in value]
    return value


def _to_job(data: dict[str, Any]) -> Job:
    return Job.model_validate(data)


class JobStore:
    """Job persistence and the transactional lease protocol."""

    def __init__(self, client: firestore.Client, settings: Settings) -> None:
        self._db = client
        self._settings = settings

    @property
    def worker_id(self) -> str:
        """The identity this store claims jobs as.

        Exposed so callers cannot end up with an identity that disagrees with the
        one actually written to `workerId`. That mismatch is silent and nasty: a
        worker would claim as one id, then fail to recognise its own jobs when
        releasing them on shutdown, and strand them until the reaper ran.
        """
        return self._settings.worker_id

    # ── Reads ────────────────────────────────────────────────────────────────

    def get(self, job_id: str) -> Job | None:
        snapshot = self._db.collection(JOBS).document(job_id).get()
        if not snapshot.exists:
            return None
        return _to_job(snapshot.to_dict() or {})

    def queued(self, limit: int = 10) -> list[Job]:
        """The oldest waiting jobs, FIFO.

        Deliberately narrow: it selects on status and orders by creation, so the
        result set is bounded. An unfiltered listen over `jobs` would bill a read
        per document on every reconnect — the single easiest way to burn the
        free daily quota.
        """
        query = (
            self._db.collection(JOBS)
            .where(filter=firestore.FieldFilter("status", "==", JobStatus.QUEUED.value))
            .order_by("createdAt")
            .limit(limit)
        )
        return [_to_job(doc.to_dict() or {}) for doc in query.stream()]

    def expired(self, now: datetime, limit: int = 50) -> list[Job]:
        """RUNNING jobs whose lease has lapsed — the reaper's input."""
        query = (
            self._db.collection(JOBS)
            .where(filter=firestore.FieldFilter("status", "==", JobStatus.RUNNING.value))
            .where(filter=firestore.FieldFilter("leaseExpiresAt", "<=", now))
            .limit(limit)
        )
        return [_to_job(doc.to_dict() or {}) for doc in query.stream()]

    def events(self, job_id: str) -> list[dict[str, Any]]:
        """The append-only event log, in the order things actually happened.

        Ordered by ``(at, seq)``, not by ``at`` alone. One transition can emit
        several events at the identical instant — a reap emits LEASE_EXPIRED and
        REQUEUED together — and ordering by timestamp alone leaves Firestore to
        break the tie on document id, which is a random uuid. The log would then
        read in a different order on different reads.
        """
        docs = (
            self._db.collection(JOBS)
            .document(job_id)
            .collection(EVENTS)
            .order_by("at")
            .order_by("seq")
            .stream()
        )
        return [doc.to_dict() or {} for doc in docs]

    # ── Writes ───────────────────────────────────────────────────────────────

    def create(self, job: Job) -> None:
        self._db.collection(JOBS).document(job.id).set(_to_document(job))

    def apply(self, transition: Transition) -> None:
        """Persist a transition computed outside a transaction.

        Used for terminal transitions the owning worker drives (completion,
        failure), where no other writer is competing for the document.
        """
        batch = self._db.batch()
        job_ref = self._db.collection(JOBS).document(transition.job.id)
        batch.set(job_ref, _to_document(transition.job))
        for event in transition.events:
            batch.set(
                job_ref.collection(EVENTS).document(event.id),
                _unwrap_enums(event.model_dump(by_alias=True, mode="python")),
            )
        batch.commit()

    def try_claim(self, job_id: str, *, now: datetime | None = None) -> Job | None:
        """Attempt to claim one job. Returns the claimed job, or ``None`` if
        another worker got there first.

        ``None`` is an ordinary outcome under contention, not an error.
        """
        now = now or datetime.now(UTC)
        worker_id = self._settings.worker_id
        lease_seconds = self._settings.lease_seconds
        job_ref = self._db.collection(JOBS).document(job_id)

        @firestore.transactional
        def _claim(transaction: firestore.Transaction) -> Job | None:
            # Re-read INSIDE the transaction. This is the whole trick: on a
            # retry caused by a competing write, this read sees the winner's
            # RUNNING state and the claim is correctly abandoned.
            snapshot = job_ref.get(transaction=transaction)
            if not snapshot.exists:
                return None

            current = _to_job(snapshot.to_dict() or {})
            if not lease.is_claimable(current, now):
                return None

            transition = lease.claim(
                current, worker_id=worker_id, now=now, lease_seconds=lease_seconds
            )
            transaction.set(job_ref, _to_document(transition.job))
            for event in transition.events:
                transaction.set(
                    job_ref.collection(EVENTS).document(event.id),
                    _unwrap_enums(event.model_dump(by_alias=True, mode="python")),
                )
            return transition.job

        try:
            return _claim(self._db.transaction())  # type: ignore[no-any-return]
        except gcloud_exceptions.Aborted:
            # Firestore exhausted its retries under heavy contention. Another
            # worker won; there is nothing to report.
            return None

    def claim_next(self, *, now: datetime | None = None) -> Job | None:
        """Claim the oldest available job, trying each candidate in turn.

        Waiting jobs are tried first, then jobs abandoned by a worker that
        stopped heartbeating. Including the second group matters: without it a
        crashed job is only recoverable once the reaper happens to run, so a
        worker could sit idle next to work it is entitled to take. The extra
        query only happens when the queue is empty, so the hot path stays at one
        read.
        """
        now = now or datetime.now(UTC)

        for job in self.queued():
            claimed = self.try_claim(job.id, now=now)
            if claimed is not None:
                return claimed

        for job in self.expired(now):
            claimed = self.try_claim(job.id, now=now)
            if claimed is not None:
                return claimed

        return None

    def renew(self, job_id: str, *, now: datetime | None = None) -> Job | None:
        """Extend this worker's lease. Returns ``None`` if the lease was lost —
        reaped and reclaimed by someone else — which the caller must treat as a
        signal to abandon the job rather than keep working on it."""
        now = now or datetime.now(UTC)
        worker_id = self._settings.worker_id
        lease_seconds = self._settings.lease_seconds
        job_ref = self._db.collection(JOBS).document(job_id)

        @firestore.transactional
        def _renew(transaction: firestore.Transaction) -> Job | None:
            snapshot = job_ref.get(transaction=transaction)
            if not snapshot.exists:
                return None
            current = _to_job(snapshot.to_dict() or {})
            if current.status is not JobStatus.RUNNING or current.worker_id != worker_id:
                return None
            transition = lease.renew(
                current, worker_id=worker_id, now=now, lease_seconds=lease_seconds
            )
            transaction.set(job_ref, _to_document(transition.job))
            return transition.job

        try:
            return _renew(self._db.transaction())  # type: ignore[no-any-return]
        except gcloud_exceptions.Aborted:
            return None

    def reap(self, *, now: datetime | None = None) -> list[Job]:
        """Reclaim every job whose worker stopped heartbeating.

        This is the reaper. It is bound to a periodic worker task rather than a
        Cloud Function — the logic is a pure function either way, so the binding
        is a deployment choice, and the worker task costs nothing.
        See docs/adr/0006-lease-based-job-claiming.md.
        """
        now = now or datetime.now(UTC)
        reaped: list[Job] = []

        for job in self.expired(now):
            job_ref = self._db.collection(JOBS).document(job.id)

            @firestore.transactional
            def _reap(transaction: firestore.Transaction, ref: Any = job_ref) -> Job | None:
                snapshot = ref.get(transaction=transaction)
                if not snapshot.exists:
                    return None
                current = _to_job(snapshot.to_dict() or {})
                # Re-check inside the transaction: the owner may have renewed
                # between the query and here, in which case the job is healthy
                # and must be left alone.
                transition = lease.reap(current, now=now)
                if transition is None:
                    return None
                transaction.set(ref, _to_document(transition.job))
                for event in transition.events:
                    transaction.set(
                        ref.collection(EVENTS).document(event.id),
                        _unwrap_enums(event.model_dump(by_alias=True, mode="python")),
                    )
                return transition.job

            try:
                result = _reap(self._db.transaction())
            except gcloud_exceptions.Aborted:
                continue
            if result is not None:
                reaped.append(result)

        return reaped


class SourceStore:
    """Ingested sources at ``sources/{sourceId}``."""

    def __init__(self, client: firestore.Client, settings: Settings) -> None:
        self._db = client
        self._settings = settings

    def get(self, source_id: str) -> Source | None:
        snapshot = self._db.collection(SOURCES).document(source_id).get()
        if not snapshot.exists:
            return None
        return Source.model_validate(snapshot.to_dict() or {})

    def find_by_external_id(
        self, *, uid: str, provider: SourceProvider, external_id: str
    ) -> Source | None:
        """Dedupe *before* downloading.

        Re-submitting a known video must not re-fetch two gigabytes, so this is
        keyed on the identity an adapter can derive with no network access.
        Content hashing catches the remaining case — the same video under two
        URLs — but only after the bytes have already arrived.
        """
        query = (
            self._db.collection(SOURCES)
            .where(filter=firestore.FieldFilter("uid", "==", uid))
            .where(filter=firestore.FieldFilter("provider", "==", provider.value))
            .where(filter=firestore.FieldFilter("externalId", "==", external_id))
            .limit(1)
        )
        for doc in query.stream():
            return Source.model_validate(doc.to_dict() or {})
        return None

    def find_by_content_hash(self, *, uid: str, content_hash: str) -> Source | None:
        query = (
            self._db.collection(SOURCES)
            .where(filter=firestore.FieldFilter("uid", "==", uid))
            .where(filter=firestore.FieldFilter("contentHash", "==", content_hash))
            .limit(1)
        )
        for doc in query.stream():
            return Source.model_validate(doc.to_dict() or {})
        return None

    def save(self, source: Source) -> None:
        self._db.collection(SOURCES).document(source.id).set(_to_document(source))

    def touch(self, source_id: str, *, now: datetime | None = None) -> None:
        """Record that a stage read the file, for least-recently-used eviction.

        A separate, tiny write rather than part of a larger update: it happens on
        every stage that opens the media, and the workspace GC is worthless
        without it.
        """
        self._db.collection(SOURCES).document(source_id).update(
            {"lastAccessedAt": now or datetime.now(UTC)}
        )

    def eviction_candidates(self, *, uid: str | None = None) -> list[Source]:
        """Downloaded sources the GC may consider.

        Local-file sources are excluded by the caller, not here: the workspace
        does not own those files and must never delete them.
        """
        collection = self._db.collection(SOURCES)
        query: firestore.Query | firestore.CollectionReference = (
            collection.where(filter=firestore.FieldFilter("uid", "==", uid))
            if uid is not None
            else collection
        )
        return [Source.model_validate(doc.to_dict() or {}) for doc in query.stream()]


class WorkerStore:
    """Heartbeat and capability advertisement at ``workers/{workerId}``."""

    def __init__(self, client: firestore.Client, settings: Settings) -> None:
        self._db = client
        self._settings = settings

    def announce(self, heartbeat: WorkerHeartbeat) -> None:
        self._db.collection(WORKERS).document(heartbeat.worker_id).set(_to_document(heartbeat))

    def get(self, worker_id: str) -> WorkerHeartbeat | None:
        snapshot = self._db.collection(WORKERS).document(worker_id).get()
        if not snapshot.exists:
            return None
        return WorkerHeartbeat.model_validate(snapshot.to_dict() or {})

    def all(self) -> Sequence[WorkerHeartbeat]:
        return [
            WorkerHeartbeat.model_validate(doc.to_dict() or {})
            for doc in self._db.collection(WORKERS).stream()
        ]


def iter_all_jobs(client: firestore.Client) -> Iterator[Job]:
    """Every job, for tests and diagnostics. Never used on a hot path."""
    for doc in client.collection(JOBS).stream():
        yield _to_job(doc.to_dict() or {})
