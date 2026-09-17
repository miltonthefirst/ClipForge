"""The stage contract.

A job is an ordered pipeline of **idempotent, checkpointed** stages, not one
monolithic unit (docs/PLAN.md decision D2). That falls directly out of the VRAM
budget: you must be able to resume after a model swap or a crash without
re-downloading a 2 GB source or re-running a twenty-minute transcription.

Two rules make that work, and both are the stage author's responsibility:

**A stage must be idempotent.** It may be run again after a crash that happened
anywhere inside it, including immediately before its checkpoint was persisted.

**A checkpoint is opaque to the scheduler.** It is persisted and handed back
verbatim, never interpreted. A stage that needs the scheduler to understand its
checkpoint is a stage that has put scheduling logic in the wrong place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Protocol, runtime_checkable

from clipforge_contracts import Job, Lane, StageName

from clipforge.config import Settings
from clipforge.models.broker import ModelBroker

__all__ = ["Stage", "StageContext", "StageOutcome", "StageProgress", "StageRegistry"]


class StageProgress:
    """What a running stage is currently doing, held in memory and nowhere else.

    Saying something has to be free, or a stage ends up rationing how often it
    speaks and the note becomes as uninformative as the silence it replaced. So
    nothing here writes: the note is published by whatever write happens next,
    which in practice is the 30-second lease heartbeat — a full-document write
    the job was paying for anyway (docs/adr/0004-dedicated-firebase-project.md
    makes throttled progress writes a requirement, not a preference).

    One instance per running job. The stage thread sets it and the heartbeat
    thread reads it, which is why the lock is here rather than a bare attribute.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._note: str | None = None

    def set(self, note: str) -> None:
        with self._lock:
            self._note = note

    def clear(self) -> None:
        with self._lock:
            self._note = None

    def current(self) -> str | None:
        with self._lock:
            return self._note


@dataclass
class StageContext:
    """Everything a stage is given, and the channels it has back.

    A stage receives its own checkpoint — never the whole job's history — and
    returns a new one. It gets the broker so GPU stages can take a residency
    lease, and the settings so nothing has to reach for a global.

    :meth:`progress` is the only thing a stage can say *while* it runs. The
    outcome is the rest, and it arrives after the work is over — which was the
    whole problem: a MUSIC job that ran for thirty minutes produced two events
    and then nothing, and was indistinguishable from a hung one.
    """

    job: Job
    stage_name: StageName
    checkpoint: dict[str, Any] | None
    settings: Settings
    broker: ModelBroker
    # Set by the runner while the stage runs; a stage that has been asked to stop
    # should checkpoint and return rather than fight it.
    should_stop: Any = None
    # Set by the runner. Absent in a test that builds a context by hand, which is
    # why `progress` tolerates it being None rather than making every caller check.
    notes: StageProgress | None = None

    def stopping(self) -> bool:
        return bool(self.should_stop and self.should_stop.is_set())

    def progress(self, note: str) -> None:
        """Say what this stage is doing now, in one sentence.

        Call it before anything slow. It costs nothing — the note goes to memory
        and rides out on the next lease heartbeat — so the only thing to weigh is
        whether the sentence tells a reviewer something true. Narrating a step
        that takes 50 ms is worse than saying nothing, because a ticker that
        moves at the wrong granularity teaches people to distrust it.
        """
        if self.notes is not None:
            self.notes.set(note)


@dataclass
class StageOutcome:
    """What a stage produced: its resume state, and anything it measured."""

    checkpoint: dict[str, Any] | None = None
    peak_vram_mb: int | None = None
    detail: str | None = None
    # A stage that stopped early on request rather than finishing. The runner
    # leaves it PENDING so the next attempt resumes it, instead of marking it
    # DONE and silently skipping the work.
    incomplete: bool = False
    # A stage whose work was already done — a cache hit. Recorded as SKIPPED
    # rather than DONE so the distinction between "we did this" and "this was
    # already there" survives into the job document, which is what makes a cache
    # claim checkable rather than merely asserted.
    skipped: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Stage(Protocol):
    """One step of a pipeline."""

    name: StageName
    lane: Lane

    def run(self, context: StageContext) -> StageOutcome:
        """Do the work. Must be safe to call again after a crash."""
        ...


class StageRegistry:
    """Maps a stage name to its implementation.

    A registry rather than a match statement so the ECHO job type can register
    three artificial stages that exercise the whole harness without any media
    code existing yet — which is exactly what Phase 2 needs and Phase 2's
    "explicitly out of scope" forbids doing any other way.
    """

    def __init__(self) -> None:
        self._stages: dict[StageName, Stage] = {}

    def register(self, stage: Stage) -> Stage:
        if stage.name in self._stages:
            raise ValueError(f"stage {stage.name.value} is already registered")
        self._stages[stage.name] = stage
        return stage

    def get(self, name: StageName) -> Stage:
        try:
            return self._stages[name]
        except KeyError:
            raise KeyError(
                f"no implementation registered for stage {name.value}. "
                f"Registered: {sorted(s.value for s in self._stages)}"
            ) from None

    def __contains__(self, name: object) -> bool:
        return name in self._stages
