"""The ECHO job type: three artificial stages that touch nothing.

Phase 2's scope note is explicit that this phase must not touch media. ECHO is
how the scheduler, the lease, the checkpointing and the resume path get exercised
end to end anyway — with no yt-dlp, no Whisper, no Ollama and no ffmpeg.

It is not throwaway scaffolding. It stays as the harness that Phase 3 onward can
use to test scheduler behaviour without waiting twenty minutes for a real
pipeline, and it is what makes the "kill the worker mid-stage and watch it
resume" criterion a *test* rather than a manual demonstration.

The stages deliberately span both lanes: two CPU and one nominally GPU, so lane
assignment and broker acquisition are covered too.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from clipforge_contracts import Job, JobStatus, JobType, Lane, StageName, StageStatus
from clipforge_contracts import Stage as ContractStage

from clipforge.observability import get_logger
from clipforge.stages.base import Stage, StageContext, StageOutcome, StageRegistry

log = get_logger(__name__)

__all__ = ["ECHO_STAGES", "echo_registry", "echo_stages", "new_echo_job", "registry_for"]

# A hook for the resumability test: when this env var names a stage, that stage
# aborts the process hard the first time it runs. Killing the worker from inside
# the stage is the only way to reproduce a genuine mid-stage crash — an external
# taskkill races the stage and makes the test flaky.
CRASH_ENV = "CLIPFORGE_ECHO_CRASH_AT"

# Records which stages ran in this process, so a test can assert that a resumed
# job did NOT re-run a stage that was already DONE.
RAN_ENV = "CLIPFORGE_ECHO_RAN_FILE"


def _record_run(stage_name: StageName) -> None:
    path = os.environ.get(RAN_ENV)
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:  # noqa: PTH123 - append-only trace file
        handle.write(f"{stage_name.value}\n")


def _maybe_crash(stage_name: StageName) -> None:
    if os.environ.get(CRASH_ENV) == stage_name.value:
        # os._exit, not sys.exit: this must look like a power cut, skipping
        # finally blocks, atexit handlers and the graceful-shutdown path. A clean
        # exit would release the lease and prove nothing about crash recovery.
        os._exit(137)


@dataclass
class EchoStage:
    """A stage that sleeps briefly, checkpoints, and returns."""

    name: StageName
    lane: Lane
    work_seconds: float = 0.01
    required_vram_mb: int = 0

    def run(self, context: StageContext) -> StageOutcome:
        _record_run(self.name)
        log.info("echo.start", stage=self.name.value, checkpoint=context.checkpoint)

        _maybe_crash(self.name)

        # Every GPU-lane stage takes a broker lease, including one that needs no
        # VRAM. Gating on `required_vram_mb` would mean this stage silently took
        # the CPU path and the broker was never exercised at all — which is
        # exactly what it looked like it was doing.
        if self.lane is Lane.GPU:
            with context.broker.acquire(self.name.value, self.required_vram_mb) as leased:
                time.sleep(self.work_seconds)
                peak = leased.observe()
            return StageOutcome(
                checkpoint={"echoed": self.name.value},
                peak_vram_mb=peak or None,
                detail=f"{self.name.value} completed on the GPU lane",
            )

        time.sleep(self.work_seconds)
        return StageOutcome(
            checkpoint={"echoed": self.name.value},
            detail=f"{self.name.value} completed on the CPU lane",
        )


ECHO_STAGES: tuple[EchoStage, ...] = (
    EchoStage(name=StageName.ECHO_ONE, lane=Lane.CPU),
    EchoStage(name=StageName.ECHO_TWO, lane=Lane.CPU),
    # Nominally GPU so the broker is exercised, but with a zero VRAM requirement
    # so it runs on a machine with no NVIDIA device — which is where CI runs.
    EchoStage(name=StageName.ECHO_THREE, lane=Lane.GPU, required_vram_mb=0),
)


def echo_registry() -> StageRegistry:
    registry = StageRegistry()
    for stage in ECHO_STAGES:
        registry.register(stage)
    return registry


def echo_stages() -> list[ContractStage]:
    """The `stages` array for a fresh ECHO job."""
    return [
        ContractStage(name=stage.name, lane=stage.lane, status=StageStatus.PENDING)
        for stage in ECHO_STAGES
    ]


def new_echo_job(*, uid: str, job_id: str | None = None, max_attempts: int = 3) -> Job:
    """Build a fresh ECHO job, ready to be written to Firestore."""
    now = datetime.now(UTC)
    return Job(
        id=job_id or uuid.uuid4().hex,
        uid=uid,
        type=JobType.ECHO,
        status=JobStatus.QUEUED,
        stages=echo_stages(),
        attempts=0,
        max_attempts=max_attempts,
        created_at=now,
        updated_at=now,
    )


def registry_for(job_type: JobType) -> StageRegistry:
    """The stage implementations for a job type.

    CLIP is not implemented here: its stages arrive in Phases 3-6. Raising a
    clear error beats a job that claims successfully and then fails with a
    KeyError deep inside the runner.
    """
    if job_type is JobType.ECHO:
        return echo_registry()
    raise NotImplementedError(
        f"job type {job_type.value} has no stage implementations yet "
        "(the CLIP pipeline lands in Phases 3-6)"
    )


# Re-exported for the runner's type checking.
_: type[Stage] = EchoStage
