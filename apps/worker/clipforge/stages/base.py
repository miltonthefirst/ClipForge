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
from typing import Any, Protocol, runtime_checkable

from clipforge_contracts import Job, Lane, StageName

from clipforge.config import Settings
from clipforge.models.broker import ModelBroker

__all__ = ["Stage", "StageContext", "StageOutcome", "StageRegistry"]


@dataclass
class StageContext:
    """Everything a stage is given, and the only channel it has back.

    A stage receives its own checkpoint — never the whole job's history — and
    returns a new one. It gets the broker so GPU stages can take a residency
    lease, and the settings so nothing has to reach for a global.
    """

    job: Job
    stage_name: StageName
    checkpoint: dict[str, Any] | None
    settings: Settings
    broker: ModelBroker
    # Set by the runner while the stage runs; a stage that has been asked to stop
    # should checkpoint and return rather than fight it.
    should_stop: Any = None

    def stopping(self) -> bool:
        return bool(self.should_stop and self.should_stop.is_set())


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
