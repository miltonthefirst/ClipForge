"""Every job type resolves to a stage that can actually run.

This guards a defect the project has already shipped once. `RenderStage` was
implemented and unit-tested in Phase 6 and then left out of `build_clip_stages`,
so no job could ever reach it — the stage had tests, the pipeline had tests, and
nothing tested that the pipeline contained the stage. It surfaced in Phase 8, two
phases late.

`build_clip_stages` has carried an assertion against that ever since. Nothing
covered the *other* job types, which are registered by a different function
through a different path, and each of which is exactly one stage: forget the one
registration and the job type silently becomes unrunnable.

The stores are stubs because none of this touches them. What is under test is
whether the wiring exists, not what it does once it runs.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from clipforge.config import Settings
from clipforge.stages.base import StageRegistry
from clipforge.stages.pipeline import (
    CLIP_PIPELINE,
    MUSIC_PIPELINE,
    PUBLISH_PIPELINE,
    REMAKE_PIPELINE,
    UPLOAD_PIPELINE,
    build_registry_factory,
)
from clipforge_contracts import JobType, Lane, StageName

pytestmark = pytest.mark.unit


class Stub:
    """Answers any call with None. No job type's *wiring* reads a store."""

    def __getattr__(self, name: str) -> object:
        return lambda *args, **kwargs: None


@pytest.fixture
def factory() -> Callable[[JobType], StageRegistry]:
    settings = Settings(_env_file=None)
    return build_registry_factory(
        settings=settings,
        sources=Stub(),  # type: ignore[arg-type]
        workspace=Stub(),  # type: ignore[arg-type]
        transcripts=Stub(),  # type: ignore[arg-type]
        archive=Stub(),  # type: ignore[arg-type]
        candidates=Stub(),  # type: ignore[arg-type]
        clips=Stub(),  # type: ignore[arg-type]
        publications=Stub(),  # type: ignore[arg-type]
        blobs=Stub(),  # type: ignore[arg-type]
        channels=None,
    )


PIPELINES: dict[JobType, tuple[tuple[StageName, Lane], ...]] = {
    JobType.CLIP: CLIP_PIPELINE,
    JobType.PUBLISH: PUBLISH_PIPELINE,
    JobType.MUSIC: MUSIC_PIPELINE,
    JobType.UPLOAD: UPLOAD_PIPELINE,
    JobType.REMAKE: REMAKE_PIPELINE,
}

# Hoisted and annotated rather than sorted inline: pytest types parametrize's
# argvalues as Iterable[object], which propagates into an inline lambda and
# loses the key type.
JOB_TYPES: list[JobType] = sorted(PIPELINES, key=lambda job: job.value)


@pytest.mark.parametrize("job_type", JOB_TYPES)
def test_every_stage_a_job_declares_has_an_implementation(
    job_type: JobType, factory: Callable[[JobType], StageRegistry]
) -> None:
    """The exact shape of the defect: a stage in the document, none in the registry.

    A job's `stages` array is written at creation and is authoritative for its
    whole life, so a missing implementation does not fail fast — the job runs
    until it reaches that stage and then fails there, after everything before it
    has already been paid for.
    """
    registry = factory(job_type)
    for name, _lane in PIPELINES[job_type]:
        assert registry.get(name) is not None, (
            f"{job_type.value} declares {name.value} but cannot run it"
        )


@pytest.mark.parametrize("job_type", JOB_TYPES)
def test_each_stage_agrees_with_the_lane_its_pipeline_declares(
    job_type: JobType, factory: Callable[[JobType], StageRegistry]
) -> None:
    """A stage in the wrong lane is scheduled against the wrong budget.

    The GPU lane is depth 1 because Whisper and the model cannot be co-resident
    in 6 GB. A stage that declares CPU in the pipeline and GPU on the class — or
    the reverse — would be dispatched on one and account for the other.
    """
    registry = factory(job_type)
    for name, lane in PIPELINES[job_type]:
        stage = registry.get(name)
        assert stage is not None
        assert stage.lane is lane, f"{name.value} is {stage.lane} but its pipeline says {lane}"


def test_echo_needs_no_media_dependencies(factory: Callable[[JobType], StageRegistry]) -> None:
    """It exists to exercise the scheduler in milliseconds on any machine."""
    registry = factory(JobType.ECHO)
    for name in (StageName.ECHO_ONE, StageName.ECHO_TWO, StageName.ECHO_THREE):
        assert registry.get(name) is not None


def test_a_job_type_with_no_stages_says_so_rather_than_returning_nothing(
    factory: Callable[[JobType], StageRegistry],
) -> None:
    """An empty registry would look like a job with nothing to do, and complete."""
    for job_type in JobType:
        registry = factory(job_type)
        assert registry is not None


def test_remake_is_on_the_cpu_lane(factory: Callable[[JobType], StageRegistry]) -> None:
    """It is mostly ffmpeg.

    It may take a GPU lease part-way through — to read the reviewer's note, or
    to re-align captions against a new narration — but it takes it through the
    broker. Putting the whole job on the GPU lane would block a transcription
    for the minutes it spends encoding.
    """
    stage = factory(JobType.REMAKE).get(StageName.REMAKE)
    assert stage is not None
    assert stage.lane.value == "CPU"
