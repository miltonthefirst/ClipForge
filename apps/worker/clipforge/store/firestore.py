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
from collections.abc import Callable, Collection, Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from typing import Any

from clipforge_contracts import (
    AgentReport,
    CalibrationReport,
    Candidate,
    Clip,
    ClipPreview,
    Job,
    JobStatus,
    MetricSnapshot,
    Preference,
    PreferenceScope,
    PreferenceStatus,
    Publication,
    PublicationState,
    Source,
    SourceProvider,
    StageStatus,
    TranscriptRef,
    WorkerHeartbeat,
)
from google.api_core import exceptions as gcloud_exceptions
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore
from pydantic import BaseModel, ValidationError

from clipforge.config import Settings
from clipforge.observability import get_logger
from clipforge.scheduler import lease
from clipforge.scheduler.lease import Transition

log = get_logger(__name__)

JOBS = "jobs"
WORKERS = "workers"
AGENTS = "agents"
SOURCES = "sources"
CANDIDATES = "candidates"
CLIPS = "clips"
PREFERENCES = "preferences"
PREVIEW = "preview"
PUBLICATIONS = "publications"
EVENTS = "events"
METRICS = "metrics"
CALIBRATIONS = "calibrations"


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


def _to_document(
    model: Job
    | WorkerHeartbeat
    | AgentReport
    | Source
    | TranscriptRef
    | Candidate
    | Clip
    | ClipPreview
    | Preference
    | Publication
    | MetricSnapshot
    | CalibrationReport,
) -> dict[str, Any]:
    """Model to Firestore document.

    ``mode="python"`` rather than ``"json"`` so datetimes stay as datetimes and
    land as native Firestore timestamps. That matters: the reaper's query filters
    on ``leaseExpiresAt``, and a string comparison there would be wrong the
    moment a timezone offset differed.
    """
    document: dict[str, Any] = _unwrap_enums(model.model_dump(by_alias=True, mode="python"))
    return document


def _unwrap_enums(value: Any) -> Any:
    """Convert the Python types Firestore will not take, at the boundary.

    **StrEnum** is a str subclass, so Firestore would accept it — but it would
    round-trip as the enum's repr in some client versions. Converting explicitly
    removes the ambiguity.

    **A plain date** it will not take at all. Firestore has a timestamp type and
    no date type, and the client raises ``TypeError`` on
    ``datetime.date``. ``MetricSnapshot.date`` is a calendar day — the day a
    channel reported, in its own reporting timezone — and promoting it to a
    timestamp would attach a midnight and a timezone that the value does not
    have and that nothing could interpret correctly afterwards. It is stored as
    its ISO string, which is what the contract already says it is, what the
    snapshot ids are built from, and what the range queries compare against.

    The `datetime` check comes first because `datetime` *is* a `date`, and
    without it every timestamp in the system would be flattened to a day.
    """
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _unwrap_enums(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap_enums(v) for v in value]
    return value


def _is_contention(exc: BaseException) -> bool:
    """Whether a failed transaction lost a race rather than hit a real fault.

    Two shapes mean the same thing. ``Aborted`` is the direct one. The other is
    subtler and is what actually shows up under load: when the client library
    exhausts its own retries it wraps the last ``Aborted`` in a plain
    ``ValueError`` — "Failed to commit transaction in 5 attempts." — so a handler
    that catches only ``Aborted`` lets a lost race escape as a crash. Eight
    workers racing one document reproduces it.

    Matched on ``__cause__`` rather than on the message, so it neither depends on
    the library's wording nor swallows an unrelated ``ValueError``.
    """
    if isinstance(exc, gcloud_exceptions.Aborted):
        return True
    return isinstance(exc, ValueError) and isinstance(exc.__cause__, gcloud_exceptions.Aborted)


def _to_job(data: dict[str, Any]) -> Job:
    return _read(Job, data)


def _read[T: BaseModel](model: type[T], data: dict[str, Any]) -> T:
    """One Firestore document, as a model, tolerating fields we do not know.

    The contracts set ``extra="forbid"`` and that is right for *writing*: it
    turns a mistyped field name into an error rather than a value that silently
    goes nowhere. On *reading* it is the wrong rule, and the difference cost a
    live failure.

    A worker holds the models it imported at start-up. Add a field to the
    schema, let a newer process write documents carrying it, and every
    already-running reader begins failing on documents that are perfectly
    valid — with a pydantic error naming the new field, which reads like
    corruption rather than like a version skew. That is exactly what happened
    when ``lineageId`` and ``version`` were added to ``Clip`` and backfilled
    onto every existing document: a worker six hours older than the schema
    rejected all seventeen of them.

    So the compatibility rule is **tolerate additions, refuse changes**. An
    unknown field is dropped and logged once; anything else — a missing
    required field, a value of the wrong type — still raises, because those
    are the failures that mean something is genuinely wrong rather than merely
    newer.

    The log line is the part that matters operationally. It names the fields,
    which is enough to tell an operator their worker is behind the schema and
    wants restarting.

    **Unknown fields are stripped at the depth they occur.** An earlier version
    read only the first path segment, so an unrecognised field *inside* a nested
    object — ``musicOptions.rights``, say — stripped the whole of
    ``musicOptions``. The document then failed for a different reason, or worse
    validated with a required block silently missing, which is the one outcome
    tolerance must never produce. Removing a nested field from the schema is
    exactly the case that hits this, and it is not rare.
    """
    try:
        return model.model_validate(data)
    except ValidationError as first:
        paths = [
            tuple(str(part) for part in error["loc"])
            for error in first.errors()
            if error["type"] == "extra_forbidden" and error["loc"]
        ]
        if not paths or len(paths) != len(first.errors()):
            # Something other than an unrecognised field is wrong. Raise the
            # original error rather than a second one from a stripped retry,
            # which would describe the symptom and not the cause.
            raise
        log.warning(
            "store.unknown_fields",
            model=model.__name__,
            fields=sorted(".".join(path) for path in paths),
            detail="written by a newer version of the contracts; restart the worker to use them",
        )
        return model.model_validate(_without(data, paths))


def _without(data: dict[str, Any], paths: list[tuple[str, ...]]) -> dict[str, Any]:
    """A copy of ``data`` with each dotted path removed, and nothing else changed.

    Copy-on-descend rather than ``deepcopy``: a document holds a poster as
    base64 and a transcript as a list of segments, and duplicating megabytes to
    delete one key is a cost paid on every read of every skewed document.
    A path through anything that is not a dict is left alone — an index into a
    list means the shape changed, not that a field was added, and that is a
    genuine error the retry should still raise.
    """
    pruned = dict(data)
    for path in paths:
        cursor: dict[str, Any] = pruned
        for segment in path[:-1]:
            branch = cursor.get(segment)
            if not isinstance(branch, dict):
                cursor = {}
                break
            cursor[segment] = branch = dict(branch)
            cursor = branch
        cursor.pop(path[-1], None)
    return pruned


# The cap on `Stage.progress` in packages/contracts/schemas/clipforge.json.
# Enforced on the way in rather than trusted: `model_copy` does not validate, so
# an over-long note would be written happily and then fail validation on every
# subsequent read — turning a cosmetic field into a job document nobody can load.
PROGRESS_MAX_CHARS = 120


def clamp_progress(note: str | None) -> str | None:
    """A note cut to the length the contract allows, or ``None`` for silence.

    Shared with :meth:`clipforge.scheduler.runner.StageRunner._record_failure`,
    which is the other place a note is put on a stage. Two copies of this rule
    could drift, and the one that drifted would write a document that no
    subsequent read can load.
    """
    if not note:
        return None
    return note[:PROGRESS_MAX_CHARS]


def _with_progress(job: Job, note: str | None) -> Job:
    """The running stage's note, on the copy of the job about to be written.

    Applied even when the note is ``None``: this says what the stage is saying
    *now*, and a stage that has gone quiet must stop appearing to talk. Only a
    RUNNING stage can carry one — a note on a finished stage would be a claim
    about work that is already over.
    """
    for index, stage in enumerate(job.stages):
        if stage.status is not StageStatus.RUNNING:
            continue
        stages = list(job.stages)
        stages[index] = stage.model_copy(update={"progress": clamp_progress(note)})
        return job.model_copy(update={"stages": stages})
    return job


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

    def queued(self, limit: int = 25) -> list[Job]:
        """The oldest waiting jobs, FIFO.

        Deliberately narrow: it selects on status and orders by creation, so the
        result set is bounded. An unfiltered listen over `jobs` would bill a read
        per document on every reconnect — the single easiest way to burn the
        free daily quota.

        Scheduled jobs (``notBefore`` in the future) are returned here and
        skipped by the caller rather than excluded by the query. Excluding them
        in Firestore would mean a range filter on ``notBefore``, which would have
        to be the first ordering field — and that would silently replace FIFO
        with schedule order for every job. The window is widened instead, and the
        honest limit is that more than ``limit`` future-dated jobs queued ahead
        of a ready one would delay it until their time comes. At this project's
        scale — YouTube's quota allows about six publishes a day — that is not a
        queue depth this can reach.
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
        except (gcloud_exceptions.Aborted, ValueError) as exc:
            if not _is_contention(exc):
                raise
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
            if not lease.is_due(job, now):
                # A scheduled publish that is not due yet. Skipped before
                # opening a transaction: `try_claim` would reject it anyway, and
                # a write attempt per poll per scheduled job is pure cost.
                continue
            claimed = self.try_claim(job.id, now=now)
            if claimed is not None:
                return claimed

        for job in self.expired(now):
            claimed = self.try_claim(job.id, now=now)
            if claimed is not None:
                return claimed

        return None

    def renew(
        self, job_id: str, *, now: datetime | None = None, progress: str | None = None
    ) -> Job | None:
        """Extend this worker's lease. Returns ``None`` if the lease was lost —
        reaped and reclaimed by someone else — which the caller must treat as a
        signal to abandon the job rather than keep working on it.

        ``progress`` is what the running stage is saying about itself right now,
        and this is the only place it can be applied. Two reasons, both firm.
        This transaction re-reads the stored document and writes it whole, so a
        note the caller set on its own copy of the job would be read straight
        back over. And the heartbeat is the only write that happens *while* a
        stage runs — every other write is a stage boundary — so it is the only
        one that can report a ten-minute stage before it is over. Riding it is
        what makes the note free.
        """
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
            renewed = _with_progress(transition.job, progress)
            transaction.set(job_ref, _to_document(renewed))
            return renewed

        try:
            return _renew(self._db.transaction())  # type: ignore[no-any-return]
        except (gcloud_exceptions.Aborted, ValueError) as exc:
            if not _is_contention(exc):
                raise
            return None

    def reap(self, *, now: datetime | None = None, skip: Collection[str] = ()) -> list[Job]:
        """Reclaim every job whose worker stopped heartbeating.

        This is the reaper. It is bound to a periodic worker task rather than a
        Cloud Function — the logic is a pure function either way, so the binding
        is a deployment choice, and the worker task costs nothing.
        See docs/adr/0006-lease-based-job-claiming.md.

        ``skip`` is how a worker refuses to reap its own work. An expired lease
        normally means "the owner is gone", but the owner can also be *right
        here* and merely unable to renew — a stalled network is enough. Without
        this the worker reclaims a job it is actively running, and does the
        whole thing twice: observed as a re-render and re-upload of two clips
        that were already finished. A caller can only vouch for its own jobs, so
        this is a parameter rather than a rule.
        """
        now = now or datetime.now(UTC)
        reaped: list[Job] = []

        for job in self.expired(now):
            if job.id in skip:
                continue
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
            except (gcloud_exceptions.Aborted, ValueError) as exc:
                if not _is_contention(exc):
                    raise
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
        return _read(Source, snapshot.to_dict() or {})

    def find_by_external_id(self, *, provider: SourceProvider, external_id: str) -> Source | None:
        """Dedupe *before* downloading.

        Re-submitting a known video must not re-fetch two gigabytes, so this is
        keyed on the identity an adapter can derive with no network access.
        Content hashing catches the remaining case — the same video under two
        URLs — but only after the bytes have already arrived.

        Deliberately not scoped to a uid. ClipForge is one shared workspace with
        one workspace directory on one machine, so a video someone else already
        downloaded is *already here*; keying the lookup on who asked would send
        the worker to fetch a second copy of a file sitting next to the first,
        and transcribe it again on top. The cache is a property of the machine,
        not of the account.
        """
        query = (
            self._db.collection(SOURCES)
            .where(filter=firestore.FieldFilter("provider", "==", provider.value))
            .where(filter=firestore.FieldFilter("externalId", "==", external_id))
            .limit(1)
        )
        for doc in query.stream():
            return _read(Source, doc.to_dict() or {})
        return None

    def find_by_content_hash(self, *, content_hash: str) -> Source | None:
        """The same bytes under another name — shared, for the same reason."""
        query = (
            self._db.collection(SOURCES)
            .where(filter=firestore.FieldFilter("contentHash", "==", content_hash))
            .limit(1)
        )
        for doc in query.stream():
            return _read(Source, doc.to_dict() or {})
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
        return [_read(Source, doc.to_dict() or {}) for doc in query.stream()]


class CandidateStore:
    """LLM-proposed clip windows at ``candidates/{candidateId}``."""

    def __init__(self, client: firestore.Client, settings: Settings) -> None:
        self._db = client
        self._settings = settings

    def get(self, candidate_id: str) -> Candidate | None:
        """One candidate, by id.

        The MUSIC stage needs it: removing burned-in captions means cutting the
        segment again from the source, and the candidate is what recorded where
        in the source that segment was.
        """
        snapshot = self._db.collection(CANDIDATES).document(candidate_id).get()
        if not snapshot.exists:
            return None
        return _read(Candidate, snapshot.to_dict() or {})

    def for_job(self, job_id: str) -> list[Candidate]:
        query = self._db.collection(CANDIDATES).where(
            filter=firestore.FieldFilter("jobId", "==", job_id)
        )
        return [_read(Candidate, doc.to_dict() or {}) for doc in query.stream()]

    def all(self) -> list[Candidate]:
        """Every candidate in the workspace, newest first.

        For rescoring, which is a whole-history operation by definition: the
        point of D5 is that a changed weighting re-ranks work already done, and a
        re-rank over the most recent job only would answer a different and much
        less interesting question. Workspace-wide rather than uid-scoped for the
        same reason as everything else here — a re-ranking that skipped another
        account's candidates would report a movement that is not the movement.
        """
        query = self._db.collection(CANDIDATES)
        found = [_read(Candidate, doc.to_dict() or {}) for doc in query.stream()]
        return sorted(found, key=lambda c: c.created_at, reverse=True)

    def rescore(self, totals: Sequence[tuple[str, int]]) -> int:
        """Write new totals, and touch nothing else.

        Only ``total`` moves. ``subScores`` are what the model said, and
        ``modelVersion`` and ``promptVersion`` are what said it — a recalibration
        that overwrote any of those would destroy the attribution that makes a
        score traceable to the thing that produced it, which is the one property
        Phase 9 is explicitly warned not to break.
        """
        written = 0
        for start in range(0, len(totals), 400):
            written += self._rescore_chunk(totals[start : start + 400])
        return written

    def _rescore_chunk(self, chunk: Sequence[tuple[str, int]]) -> int:
        """Write one batch, falling back to one-at-a-time if any row is gone.

        `update` fails on a document that no longer exists, and a batch fails
        whole. Without this, one candidate deleted while a long rescore was
        running would abort the rest — after earlier batches had already
        committed, leaving the collection half re-ranked with no way to tell
        which half. Re-running would then be scored against a mixture.

        The fallback writes the survivors and skips the missing, which is the
        only outcome that leaves the collection consistent.
        """
        batch = self._db.batch()
        for candidate_id, total in chunk:
            batch.update(self._db.collection(CANDIDATES).document(candidate_id), {"total": total})
        try:
            batch.commit()
        except gcloud_exceptions.NotFound:
            written = 0
            for candidate_id, total in chunk:
                try:
                    self._db.collection(CANDIDATES).document(candidate_id).update({"total": total})
                except gcloud_exceptions.NotFound:
                    log.info("rescore.candidate_gone", candidate_id=candidate_id)
                    continue
                written += 1
            return written
        return len(chunk)

    def replace_for_job(self, job_id: str, candidates: list[Candidate]) -> None:
        """Write this job's candidates, removing any from a previous attempt.

        Replace rather than append: ANALYZE is idempotent and a retry must not
        leave the review queue holding two generations of proposals for the same
        job, half of which were superseded.
        """
        batch = self._db.batch()
        for existing in self.for_job(job_id):
            batch.delete(self._db.collection(CANDIDATES).document(existing.id))
        for candidate in candidates:
            batch.set(
                self._db.collection(CANDIDATES).document(candidate.id),
                _to_document(candidate),
            )
        batch.commit()


class ClipStore:
    """Rendered clips at ``clips/{clipId}``, with previews in a subcollection."""

    def __init__(self, client: firestore.Client, settings: Settings) -> None:
        self._db = client
        self._settings = settings

    def get(self, clip_id: str) -> Clip | None:
        snapshot = self._db.collection(CLIPS).document(clip_id).get()
        if not snapshot.exists:
            return None
        return _read(Clip, snapshot.to_dict() or {})

    def for_job(self, job_id: str) -> list[Clip]:
        query = self._db.collection(CLIPS).where(
            filter=firestore.FieldFilter("jobId", "==", job_id)
        )
        return [_read(Clip, doc.to_dict() or {}) for doc in query.stream()]

    def preview(self, clip_id: str) -> ClipPreview | None:
        snapshot = (
            self._db.collection(CLIPS)
            .document(clip_id)
            .collection(PREVIEW)
            .document("poster")
            .get()
        )
        if not snapshot.exists:
            return None
        return _read(ClipPreview, snapshot.to_dict() or {})

    def save(self, clip: Clip, *, preview: ClipPreview | None = None) -> None:
        """Write a clip and its poster together.

        One batch, deliberately. On the free tier the poster is most of what a
        phone review has to go on, so a clip that appeared in the queue without
        one would render as a broken card — worse than not appearing yet.
        """
        batch = self._db.batch()
        clip_ref = self._db.collection(CLIPS).document(clip.id)
        batch.set(clip_ref, _to_document(clip))
        if preview is not None:
            batch.set(clip_ref.collection(PREVIEW).document("poster"), _to_document(preview))
        batch.commit()


class PreferenceStore:
    """What the system has learned about a reviewer, at ``preferences/{id}``.

    Reads are filtered in Python rather than by a composite query. The
    collection is small by construction — a preference is proposed only after a
    remake that applied feedback, capped at three per remake, and deduped
    against everything already held — so one bounded read beats maintaining an
    index for a handful of rows.
    """

    def __init__(self, client: firestore.Client, settings: Settings) -> None:
        self._db = client
        self._settings = settings

    def for_source(self, source_id: str | None, *, limit: int = 200) -> list[Preference]:
        """Everything that could apply to a clip from this source.

        Its own preferences plus every EVERYTHING-scoped one, in whatever status
        — the caller filters to ACCEPTED when applying, and the learner needs
        the rejected ones too so it does not propose them a second time.
        """
        rows = [
            _read(Preference, doc.to_dict() or {})
            for doc in self._db.collection(PREFERENCES).limit(limit).stream()
        ]
        return [
            row
            for row in rows
            if row.scope is PreferenceScope.EVERYTHING
            or (source_id is not None and row.source_id == source_id)
        ]

    def accepted_for_source(self, source_id: str | None) -> list[Preference]:
        return [
            row for row in self.for_source(source_id) if row.status is PreferenceStatus.ACCEPTED
        ]

    def save_all(self, preferences: list[Preference]) -> None:
        if not preferences:
            return
        batch = self._db.batch()
        for preference in preferences:
            batch.set(
                self._db.collection(PREFERENCES).document(preference.id),
                _to_document(preference),
            )
        batch.commit()

    def mark_applied(self, preferences: list[Preference]) -> None:
        """Count a preference that actually shaped a remake.

        The number that says whether it is earning its place: one that never
        fires is noise, and one that fires on everything is a default the
        pipeline should adopt outright rather than keep asking about.
        """
        if not preferences:
            return
        batch = self._db.batch()
        for preference in preferences:
            batch.update(
                self._db.collection(PREFERENCES).document(preference.id),
                {"timesApplied": (preference.times_applied or 0) + 1},
            )
        batch.commit()


class PublicationStore:
    """Publish attempts at ``clips/{clipId}/publications/{pubId}``.

    This collection is the audit log, which is why it is a subcollection of the
    clip rather than a top-level one: "what happened to this clip?" is the
    question it exists to answer, and a query is not needed to answer it.

    Nothing here ever holds a credential. The document records *what was
    published, where, and when* — the token that performed the upload stays in
    a file on the worker (D7, and Phase 8 exit criterion 4).
    """

    def __init__(self, client: firestore.Client, settings: Settings) -> None:
        self._db = client
        self._settings = settings

    def _collection(self, clip_id: str) -> Any:
        return self._db.collection(CLIPS).document(clip_id).collection(PUBLICATIONS)

    def get(self, clip_id: str, publication_id: str) -> Publication | None:
        snapshot = self._collection(clip_id).document(publication_id).get()
        if not snapshot.exists:
            return None
        return _read(Publication, snapshot.to_dict() or {})

    def for_clip(self, clip_id: str) -> list[Publication]:
        return [
            _read(Publication, doc.to_dict() or {}) for doc in self._collection(clip_id).stream()
        ]

    def save(self, publication: Publication) -> None:
        self._collection(publication.clip_id).document(publication.id).set(
            _to_document(publication)
        )

    def all_published(self) -> list[Publication]:
        """Every completed publication in the workspace, across all clips.

        **Not scoped by uid**, and that is deliberate rather than lazy. ClipForge
        is one shared workspace — the review queue stopped filtering by uid for
        the same reason, and the note there records the cost: "filtering here was
        what made one system look like two". The first two clips this project
        ever published went out under two different accounts, so a uid-scoped
        version of this query would have measured one of them and silently
        ignored the other, leaving a dashboard that showed half a channel while
        looking complete.

        A collection-group query, which is the one place the audit-log shape
        costs something: publications are a subcollection because "what happened
        to this clip" is the common question, and Phase 9 asks the uncommon one —
        "what happened to everything we published".

        Filtered on state rather than on `externalId != null` because Firestore
        cannot index an inequality against null, and PUBLISHED is what the state
        machine guarantees an external id alongside.
        """
        found: list[Publication] = []
        for doc in self._db.collection_group(PUBLICATIONS).stream():
            publication = _read(Publication, doc.to_dict() or {})
            if publication.state is PublicationState.PUBLISHED and publication.external_id:
                found.append(publication)
        return found

    def live_for_clip(self, clip_id: str, platform: str) -> Publication | None:
        """An attempt on this platform that is not finished and not abandoned.

        This is the idempotency lookup. A retry finds the PENDING or UPLOADING
        record its predecessor wrote and continues *that* attempt, which is the
        whole reason the record is written before the upload starts. Without it,
        a crash between "upload succeeded" and "record the video id" would be
        indistinguishable from "upload never happened" — and the recovery would
        be a second video on the channel.
        """
        for publication in self.for_clip(clip_id):
            if publication.platform.value != platform:
                continue
            if publication.state in (
                PublicationState.PENDING,
                PublicationState.UPLOADING,
                PublicationState.PUBLISHED,
            ):
                return publication
        return None


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
        return _read(WorkerHeartbeat, snapshot.to_dict() or {})

    def all(self) -> Sequence[WorkerHeartbeat]:
        return [
            _read(WorkerHeartbeat, doc.to_dict() or {})
            for doc in self._db.collection(WORKERS).stream()
        ]


class AgentStore:
    """The wish and the report at ``agents/{agentId}``.

    One document, two writers, and a line between them that is the whole reason
    this collection exists. The PWA — a phone, usually — writes ``desired`` and
    nothing else. The agent writes everything else and never writes ``desired``,
    so a Start pressed while the agent was mid-heartbeat is not quietly undone by
    a report that was assembled before the press. That is what ``merge=True``
    with the wish removed buys, and it is cheaper and easier to reason about than
    a transaction around a document written every minute.

    The one exception is creation. The rules forbid the client from creating this
    document, precisely so that "no agent has ever run here" stays distinguishable
    from "the agent is not reporting right now" — so the agent creates it, and
    that first write is the only one that may say what is wanted (``STOPPED``: a
    freshly installed agent must not start a worker nobody asked for).
    """

    def __init__(self, client: firestore.Client, settings: Settings) -> None:
        self._db = client
        self._settings = settings

    @property
    def agent_id(self) -> str:
        """One agent per machine, named like the worker it supervises.

        Sharing `worker_id` is deliberate: an operator reading `agents/tower`
        beside `workers/tower` should not have to be told they are the same
        machine.
        """
        return self._settings.worker_id

    def _ref(self) -> Any:
        return self._db.collection(AGENTS).document(self.agent_id)

    def read(self) -> dict[str, Any] | None:
        """The raw document, or ``None`` if this machine has never reported.

        Deliberately *not* parsed into :class:`AgentReport`. The caller wants
        three fields out of it, and validating the whole model here would mean a
        document written by a newer agent — one field this build has never heard
        of — could stop an older one from reading the wish. A supervisor that
        refuses to notice "stop" because it failed to parse a field it does not
        use is the worst possible failure mode for this class.
        """
        snapshot = self._ref().get()
        if not snapshot.exists:
            return None
        return snapshot.to_dict() or {}

    def publish(self, report: AgentReport, *, claim: bool = False) -> None:
        """Write what the agent knows, leaving what the client owns alone."""
        document = _to_document(report)
        if not claim:
            for owned_by_the_client in ("desired", "requestedBy", "requestedAt"):
                document.pop(owned_by_the_client, None)
        self._ref().set(document, merge=True)

    def watch(self, on_change: Callable[[dict[str, Any] | None], None]) -> Callable[[], None]:
        """Call ``on_change`` whenever the document changes, until unsubscribed.

        A listener rather than a poll, for responsiveness and for cost in that
        order: tapping Start on a phone should not wait out a poll interval, and
        Firestore bills a read per *delivered* document — an idle listener
        delivers nothing, while a poll pays whether or not anything happened.

        The callback arrives on a background thread owned by the client library.
        It must not block, which is why the agent uses it only to record the
        latest wish and wake its own loop.
        """

        def _callback(snapshots: Any, _changes: Any, _read_time: Any) -> None:
            for snapshot in snapshots:
                on_change(snapshot.to_dict() if snapshot.exists else None)

        watch = self._ref().on_snapshot(_callback)
        return watch.unsubscribe  # type: ignore[no-any-return]


def iter_all_jobs(client: firestore.Client) -> Iterator[Job]:
    """Every job, for tests and diagnostics. Never used on a hot path."""
    for doc in client.collection(JOBS).stream():
        yield _to_job(doc.to_dict() or {})


class MetricStore:
    """Daily performance snapshots at ``metrics/{publicationId}_{date}``.

    A composite id rather than a generated one, and that is the whole design.
    Polling is not transactional and will be run twice, by a cron that overlapped
    with itself or by an operator who ran the command by hand — and a generated
    id would turn each of those into a duplicate row that quietly doubles a
    clip's view count in every aggregate downstream. With the day in the id, a
    second poll of the same day overwrites rather than accumulates.

    The collection is top-level because every question Phase 9 asks spans clips.
    See the contract for why that beats a subcollection plus a collection-group
    index.
    """

    def __init__(self, client: firestore.Client, settings: Settings) -> None:
        self._db = client
        self._settings = settings

    @staticmethod
    def document_id(publication_id: str, day: date) -> str:
        return f"{publication_id}_{day.isoformat()}"

    def get(self, publication_id: str, day: date) -> MetricSnapshot | None:
        snapshot = (
            self._db.collection(METRICS).document(self.document_id(publication_id, day)).get()
        )
        if not snapshot.exists:
            return None
        return _read(MetricSnapshot, snapshot.to_dict() or {})

    def for_publication(
        self, publication_id: str, *, since: date | None = None
    ) -> list[MetricSnapshot]:
        """Snapshots for one publication, oldest day first, optionally windowed.

        `since` is a real filter and has to be. A report that names a window in
        its own header and then computes over all history is not slightly
        imprecise — it is a report whose stated scope is false, which is the one
        thing this phase cannot afford to be.
        """
        query: Any = self._db.collection(METRICS).where(
            filter=firestore.FieldFilter("publicationId", "==", publication_id)
        )
        if since is not None:
            query = query.where(filter=firestore.FieldFilter("date", ">=", since.isoformat()))
        found = [_read(MetricSnapshot, doc.to_dict() or {}) for doc in query.stream()]
        return sorted(found, key=lambda s: s.date)

    def all(self, *, since: date | None = None) -> list[MetricSnapshot]:
        """Every snapshot in the workspace, for the dashboard and the report.

        Workspace-wide for the same reason :meth:`PublicationStore.all_published`
        is: a snapshot carries the uid of whoever published the clip, and two
        people publishing from one ClipForge are still one channel's performance.
        Filtering by the reader's own uid would show them only the half of the
        channel they happened to upload themselves.
        """
        query: Any = self._db.collection(METRICS)
        if since is not None:
            query = query.where(filter=firestore.FieldFilter("date", ">=", since.isoformat()))
        found = [_read(MetricSnapshot, doc.to_dict() or {}) for doc in query.stream()]
        return sorted(found, key=lambda s: (s.publication_id, s.date))

    def save_all(self, snapshots: Sequence[MetricSnapshot]) -> int:
        """Write snapshots, refusing to overwrite a day that has already settled.

        A settled snapshot is never rewritten. YouTube restates the last two or
        three days and then stops, so a day outside that window is final — and
        rewriting it anyway would mean a calibration run today and the same run
        tomorrow could disagree about what happened last month, which destroys
        the only property that makes the report worth keeping.

        Returns how many were actually written, which is what the caller reports
        rather than the number it offered.
        """
        written = 0
        batch = self._db.batch()
        pending = 0
        for snapshot in snapshots:
            existing = self.get(snapshot.publication_id, snapshot.date)
            if existing is not None and not existing.partial:
                continue
            reference = self._db.collection(METRICS).document(
                self.document_id(snapshot.publication_id, snapshot.date)
            )
            batch.set(reference, _to_document(snapshot))
            written += 1
            pending += 1
            # Firestore caps a batch at 500 writes. A long backfill of a year of
            # daily snapshots crosses that easily.
            if pending >= 400:
                batch.commit()
                batch = self._db.batch()
                pending = 0
        if pending:
            batch.commit()
        return written

    def missing_days(self, publication_id: str, *, start: date, end: date) -> list[date]:
        """Which days in the range have no snapshot at all.

        This is exit criterion 1 made checkable. "Accrues daily snapshots without
        gaps" is not observable from a count — a publication with 20 snapshots
        over 25 days looks healthy until someone asks which five are missing.
        """
        held = {snapshot.date for snapshot in self.for_publication(publication_id)}
        span = (end - start).days
        return [
            day
            for offset in range(span + 1)
            for day in (start + timedelta(days=offset),)
            if day not in held
        ]


class CalibrationStore:
    """Written calibration reports at ``calibrations/{reportId}``.

    Append-only by convention and by rules. A report is evidence about what was
    believed on a date, and overwriting one would leave no way to see that the
    conclusion changed — which is the single thing this collection exists to
    make visible.
    """

    def __init__(self, client: firestore.Client, settings: Settings) -> None:
        self._db = client
        self._settings = settings

    def save(self, report: CalibrationReport) -> None:
        self._db.collection(CALIBRATIONS).document(report.id).set(_to_document(report))

    def get(self, report_id: str) -> CalibrationReport | None:
        snapshot = self._db.collection(CALIBRATIONS).document(report_id).get()
        if not snapshot.exists:
            return None
        return _read(CalibrationReport, snapshot.to_dict() or {})

    def latest(self) -> CalibrationReport | None:
        """The most recent report, whoever ran it.

        `uid` on a report records who generated it, not whose data it covers —
        the analysis is workspace-wide. Querying by the reader's uid would hide a
        colleague's report and then quietly claim none had been run.
        """
        query = (
            self._db.collection(CALIBRATIONS)
            .order_by("generatedAt", direction=firestore.Query.DESCENDING)
            .limit(1)
        )
        for doc in query.stream():
            return _read(CalibrationReport, doc.to_dict() or {})
        return None
