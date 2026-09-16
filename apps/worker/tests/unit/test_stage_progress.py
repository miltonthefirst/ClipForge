"""What a running stage says about itself, and what it costs to say it.

A MUSIC job ran for thirty minutes and produced exactly two events — CLAIMED and
STAGE_STARTED — because until now a stage had no way to say anything until it
had finished. Watching it was indistinguishable from watching a hung one.

The channel that fixes that has one hard constraint: it must not buy a single
Firestore write. Throttled progress writes are a requirement rather than a
preference (docs/adr/0004-dedicated-firebase-project.md), and a write per tick
is the easiest way there is to burn the free daily quota.

So three things are pinned here. Saying something is free — a stage that
narrates a hundred times writes exactly as often as one that says nothing. The
note reaches the document anyway, carried by the lease renewal that was going to
happen regardless. And it stops being there the moment it stops being true,
because a finished stage still claiming to be "Mixing" is a worse lie than
silence.

An in-memory store rather than the emulator: the renewal path this fake mimics
is three lines long — re-read the stored document, apply, write it whole — and
that shape is precisely the reason the note cannot be set on the runner's own
copy of the job.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from clipforge.config import Settings
from clipforge.models.broker import ModelBroker
from clipforge.scheduler import lease
from clipforge.scheduler.lease import Transition
from clipforge.scheduler.runner import StageRunner
from clipforge.scheduler.worker import Worker
from clipforge.stages.base import StageContext, StageOutcome, StageProgress, StageRegistry
from clipforge.store.firestore import PROGRESS_MAX_CHARS, JobStore, _with_progress
from clipforge_contracts import (
    Job,
    JobStatus,
    JobType,
    Lane,
    Stage,
    StageName,
    StageStatus,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
JOB_ID = "job-1"
WORKER_ID = "worker-1"


def claimed_job(*, progress: str | None = None, status: StageStatus = StageStatus.PENDING) -> Job:
    """A single-stage MUSIC job, already claimed by this worker."""
    return Job(
        id=JOB_ID,
        uid="user-1",
        type=JobType.MUSIC,
        status=JobStatus.RUNNING,
        worker_id=WORKER_ID,
        lease_expires_at=T0,
        stages=[Stage(name=StageName.MUSIC, lane=Lane.CPU, status=status, progress=progress)],
        attempts=1,
        max_attempts=3,
        created_at=T0,
        updated_at=T0,
    )


class NarratingStage:
    """A stand-in for a stage that takes minutes. The channel is what is tested.

    ``during`` runs while the stage is still inside ``run`` — the only window in
    which a note exists at all, and therefore the only place a heartbeat can be
    made to land on one.

    ``then_raises`` makes it die where a real stage dies: after it has said what
    it was doing, and before it could say anything else.
    """

    name = StageName.MUSIC
    lane = Lane.CPU

    def __init__(
        self,
        *notes: str,
        during: Callable[[StageContext], None] | None = None,
        then_raises: Exception | None = None,
    ) -> None:
        self._notes = notes
        self._during = during
        self._then_raises = then_raises

    def run(self, context: StageContext) -> StageOutcome:
        for note in self._notes:
            context.progress(note)
        if self._during is not None:
            self._during(context)
        if self._then_raises is not None:
            raise self._then_raises
        return StageOutcome(detail="narrated")


class RecordingJobStore:
    """Enough of ``JobStore`` to run a job, recording writes instead of making them.

    ``renew`` reproduces the one detail the whole design turns on: it re-reads
    the stored document and writes it whole. Anything the runner set on its own
    copy of the job would be read straight back over, which is why the note is
    applied here instead.
    """

    worker_id = WORKER_ID

    def __init__(self, job: Job) -> None:
        self.document = job
        self.writes: list[Job] = []
        self.renewals: list[str | None] = []
        self.heartbeats: list[Job] = []
        self._unclaimed: Job | None = job

    def claim_next(self, *, now: datetime | None = None) -> Job | None:
        claimed, self._unclaimed = self._unclaimed, None
        return claimed

    def get(self, job_id: str) -> Job | None:
        return self.document if self.document.id == job_id else None

    def apply(self, transition: Transition) -> None:
        self.document = transition.job
        self.writes.append(transition.job)

    def renew(
        self, job_id: str, *, now: datetime | None = None, progress: str | None = None
    ) -> Job | None:
        self.renewals.append(progress)
        self.document = _with_progress(self.document, progress)
        self.heartbeats.append(self.document)
        return self.document


class SilentWorkerStore:
    def announce(self, heartbeat: object) -> None:
        return None


def run_stage(stage: NarratingStage, *, job: Job, notes: StageProgress) -> RecordingJobStore:
    """Run one stage through the real runner against the recording store."""
    store = RecordingJobStore(job)
    registry = StageRegistry()
    registry.register(stage)
    StageRunner(
        store=store,  # type: ignore[arg-type]
        registry=registry,
        settings=Settings(use_emulators=True),
        broker=ModelBroker(reserve_mb=0),
        progress=notes,
    ).run(job)
    return store


def build_worker(store: RecordingJobStore, stage: NarratingStage) -> Worker:
    registry = StageRegistry()
    registry.register(stage)
    return Worker(
        settings=Settings(use_emulators=True),
        jobs=store,  # type: ignore[arg-type]
        workers=SilentWorkerStore(),  # type: ignore[arg-type]
        broker=ModelBroker(reserve_mb=0),
        registry_factory=lambda _type: registry,
    )


# ─────────────────────────────────────────────────────────────────────────────
# The fake is worth only what it resembles
# ─────────────────────────────────────────────────────────────────────────────


def test_the_recording_store_takes_and_returns_what_the_real_store_does() -> None:
    """Every test below drives the real runner and the real worker against that
    fake, past a `# type: ignore[arg-type]` that tells the type checker to stop
    asking. Nothing else here would notice a method whose real counterpart had
    grown an argument, and two of them had: `claim_next` and `renew` both take a
    `now` the fake was quietly not accepting."""
    for name in ("claim_next", "get", "apply", "renew"):
        fake = inspect.signature(getattr(RecordingJobStore, name))
        real = inspect.signature(getattr(JobStore, name))
        assert fake == real, name


# ─────────────────────────────────────────────────────────────────────────────
# Saying something costs nothing
# ─────────────────────────────────────────────────────────────────────────────


def test_narrating_a_hundred_times_writes_no_more_often_than_saying_nothing() -> None:
    """The constraint the whole design exists to satisfy. If a note cost a write
    this would be a progress bar that bills by the tick."""
    silent = run_stage(NarratingStage(), job=claimed_job(), notes=StageProgress())
    chatty = run_stage(
        NarratingStage(*[f"step {i}" for i in range(100)]),
        job=claimed_job(),
        notes=StageProgress(),
    )

    assert silent.writes, "the baseline must not be vacuous"
    assert len(chatty.writes) == len(silent.writes)
    assert chatty.renewals == silent.renewals


def test_a_stage_can_narrate_with_nobody_listening() -> None:
    """A context built by hand in a test has no runner behind it, and a stage
    must not have to know which kind of context it was handed."""
    context = StageContext(
        job=claimed_job(),
        stage_name=StageName.MUSIC,
        checkpoint=None,
        settings=Settings(use_emulators=True),
        broker=ModelBroker(reserve_mb=0),
    )

    context.progress("Fetching the track")  # must not raise


# ─────────────────────────────────────────────────────────────────────────────
# Reaching the document
# ─────────────────────────────────────────────────────────────────────────────


def test_what_a_stage_says_while_it_runs_reaches_the_stored_document() -> None:
    """The whole path, with only the store faked: the stage says something, the
    heartbeat thread picks it up from memory, and the renewal it was going to
    make anyway carries it into the job document."""
    store = RecordingJobStore(claimed_job())
    worker: list[Worker] = []

    def _heartbeat(_context: StageContext) -> None:
        worker[0].renew_active([JOB_ID])

    built = build_worker(store, NarratingStage("Fetching the track", during=_heartbeat))
    worker.append(built)

    final = built.run_once()

    assert final is not None and final.status is JobStatus.COMPLETED
    assert store.renewals == ["Fetching the track", None], "the between-stage renewal says nothing"
    published = store.heartbeats[0].stages[0]
    assert published.status is StageStatus.RUNNING
    assert published.progress == "Fetching the track"


def test_the_latest_note_is_the_one_that_is_published() -> None:
    """Notes are not a queue. A stage that has moved on to mixing must not still
    be reporting the download it finished four minutes ago."""
    store = RecordingJobStore(claimed_job())
    worker: list[Worker] = []

    def _heartbeat(_context: StageContext) -> None:
        worker[0].renew_active([JOB_ID])

    stage = NarratingStage(
        "Fetching the track", "Mixing the music into the clip", during=_heartbeat
    )
    built = build_worker(store, stage)
    worker.append(built)

    built.run_once()

    assert store.heartbeats[0].stages[0].progress == "Mixing the music into the clip"


def test_only_a_running_stage_is_given_a_note() -> None:
    """A note on a finished stage would be a claim about work that is over, and
    on a pending one a claim about work that has not started."""
    job = claimed_job().model_copy(
        update={
            "stages": [
                Stage(name=StageName.MUSIC, lane=Lane.CPU, status=StageStatus.DONE),
                Stage(name=StageName.UPLOAD, lane=Lane.CPU, status=StageStatus.PENDING),
            ]
        }
    )

    unchanged = _with_progress(job, "Mixing the music into the clip")

    assert [stage.progress for stage in unchanged.stages] == [None, None]


def test_a_note_longer_than_the_contract_allows_is_cut_rather_than_written_whole() -> None:
    """The length is not the hazard. `model_copy` does not validate, so an
    over-long note would be written happily and then rejected by every
    subsequent read — a cosmetic field turning the job document unloadable."""
    job = claimed_job(status=StageStatus.RUNNING)

    stage = _with_progress(job, "x" * 400).stages[0]

    assert stage.progress is not None
    assert len(stage.progress) == PROGRESS_MAX_CHARS
    Stage.model_validate(stage.model_dump(by_alias=True, mode="json"))


# ─────────────────────────────────────────────────────────────────────────────
# And stopping being there
# ─────────────────────────────────────────────────────────────────────────────


def test_a_finished_stage_stops_claiming_to_be_mixing() -> None:
    """The stage-end write clears the note, at no extra cost: it is the write
    that records the result, and the note is simply absent from what it stores."""
    store = RecordingJobStore(claimed_job())
    worker: list[Worker] = []

    def _heartbeat(_context: StageContext) -> None:
        worker[0].renew_active([JOB_ID])

    built = build_worker(store, NarratingStage("Mixing the music into the clip", during=_heartbeat))
    worker.append(built)
    built.run_once()

    assert store.heartbeats[0].stages[0].progress is not None, "it was published"
    assert store.document.stages[0].status is StageStatus.DONE
    assert store.document.stages[0].progress is None


def test_the_note_is_dropped_the_moment_the_stage_returns() -> None:
    """Held in memory, the note outlives the write that published it. Left set,
    the next heartbeat would republish it against the stage that follows."""
    notes = StageProgress()

    run_stage(NarratingStage("Mixing the music into the clip"), job=claimed_job(), notes=notes)

    assert notes.current() is None


def test_a_note_left_behind_by_a_crashed_attempt_is_not_shown_as_news() -> None:
    """A worker killed mid-mix leaves "Mixing" in the document. The retry starts
    by saying nothing, rather than by inheriting the last thing the dead attempt
    said and presenting it as the state of this one."""
    stale = claimed_job(status=StageStatus.RUNNING, progress="Mixing the music into the clip")

    store = run_stage(NarratingStage(), job=stale, notes=StageProgress())

    assert store.writes[0].stages[0].status is StageStatus.RUNNING
    assert store.writes[0].stages[0].progress is None


# ─────────────────────────────────────────────────────────────────────────────
# Even when the runner is not there to stop it
# ─────────────────────────────────────────────────────────────────────────────


def mid_mix_job() -> Job:
    """A job exactly as a killed worker leaves it: RUNNING, and still talking."""
    return claimed_job(status=StageStatus.RUNNING, progress="Mixing the music into the clip")


@pytest.mark.parametrize(
    "end_it",
    [
        pytest.param(lambda job: lease.reap(job, now=T0 + timedelta(seconds=1)), id="reaped"),
        pytest.param(lambda job: lease.release(job, now=T0), id="released"),
        pytest.param(lambda job: lease.cancel(job, now=T0), id="cancelled"),
    ],
)
def test_a_job_nobody_is_left_to_update_stops_claiming_to_be_mixing(
    end_it: Callable[[Job], Transition | None],
) -> None:
    """These three paths end a stage with its runner gone, so nothing else is
    coming to withdraw the note — and the document they write is the last one the
    job ever gets. Ctrl-C a worker mid-mix and the job sat in the queue saying
    "Mixing the music into the clip" for good, which in this deployment is a lie
    with no expiry date: a QUEUED job means no worker is running to rewrite it."""
    ended = end_it(mid_mix_job())

    assert ended is not None
    assert ended.job.status is not JobStatus.RUNNING
    assert ended.job.stages[0].progress is None


def test_a_reaped_job_that_has_run_out_of_attempts_also_stops_talking() -> None:
    """The reaper's other branch. It fails the job outright rather than
    requeueing it, and the stage is left marked RUNNING — so a rule that keyed on
    the job's status alone would have missed exactly this document, which is the
    one that is never written again."""
    exhausted = mid_mix_job().model_copy(update={"attempts": 2, "max_attempts": 3})

    reaped = lease.reap(exhausted, now=T0 + timedelta(seconds=1))

    assert reaped is not None and reaped.job.status is JobStatus.FAILED
    assert reaped.job.stages[0].progress is None


def test_a_note_the_runner_chose_to_keep_survives_the_job_ending() -> None:
    """The clearing rule keys on RUNNING, and that is what keeps it away from the
    note below: a note on a FAILED stage was written deliberately, by the one
    caller that knew both that the stage was over and where it had got to."""
    died = claimed_job(status=StageStatus.FAILED, progress="Mixing the music into the clip")

    cancelled = lease.cancel(died, now=T0)

    assert cancelled.job.stages[0].progress == "Mixing the music into the clip"


# ─────────────────────────────────────────────────────────────────────────────
# What a stage died doing
# ─────────────────────────────────────────────────────────────────────────────


def test_a_stage_that_dies_keeps_the_note_it_died_on() -> None:
    """The one thing an error message usually cannot say. "Mixing the music into
    the clip" beside a non-zero ffmpeg exit says which pass died; the exit code
    on its own says only that a command failed."""
    store = run_stage(
        NarratingStage(
            "Fetching the track",
            "Mixing the music into the clip",
            then_raises=RuntimeError("ffmpeg exited with 1"),
        ),
        job=claimed_job(),
        notes=StageProgress(),
    )

    stage = store.document.stages[0]
    assert stage.status is StageStatus.FAILED
    assert stage.error is not None
    assert stage.progress == "Mixing the music into the clip"


def test_the_note_kept_on_a_failed_stage_is_cut_the_same_way_the_heartbeat_cuts_it() -> None:
    """A second truncation is a second chance to forget one. This note is written
    by a different caller than the heartbeat's, into the same 120-character
    field, and `model_copy` validates nothing — an over-long one would commit and
    then fail every read of the job document afterwards."""
    store = run_stage(
        NarratingStage("x" * 400, then_raises=RuntimeError("ffmpeg exited with 1")),
        job=claimed_job(),
        notes=StageProgress(),
    )

    stage = store.document.stages[0]
    assert stage.progress is not None
    assert len(stage.progress) == PROGRESS_MAX_CHARS
    Stage.model_validate(stage.model_dump(by_alias=True, mode="json"))


def test_a_worker_stopped_mid_stage_hands_back_a_job_that_says_nothing() -> None:
    """The Ctrl-C path, end to end. `_release_active` re-reads the stored
    document — which by then carries the last heartbeat's note against a stage
    still marked RUNNING — and hands exactly that back to the queue."""
    store = RecordingJobStore(mid_mix_job())
    worker = build_worker(store, NarratingStage())
    worker._active[JOB_ID] = store.document

    worker._release_active()

    assert store.document.status is JobStatus.QUEUED
    assert store.document.stages[0].progress is None
