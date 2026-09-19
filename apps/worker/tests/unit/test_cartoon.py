"""The renderer: every pose and prop draws, and the same scene draws the same frame.

Determinism is the property the whole compose record leans on, so it is
pinned by hashing pixels rather than by trusting the docstring. Everything
here draws to memory; ffmpeg is the integration tier's business.
"""

from __future__ import annotations

import hashlib

import pytest
from clipforge.media.cartoon import Canvas, draw_frame, frame_count
from clipforge.synth.scenes import TimedScene
from clipforge_contracts import SceneMood, StickActor, StickMood, StickPose, StickProp

pytestmark = pytest.mark.unit

SMALL = Canvas(width=270, height=480)


def scene(
    *,
    pose: StickPose = StickPose.STAND,
    mood: StickMood = StickMood.NEUTRAL,
    props: tuple[StickProp, ...] = (),
    actors: int = 1,
    tone: SceneMood = SceneMood.CALM,
    label: str | None = None,
    index: int = 0,
) -> TimedScene:
    return TimedScene(
        index=index,
        line="A line.",
        actors=tuple(StickActor(name=f"figure {i}", pose=pose, mood=mood) for i in range(actors)),
        props=props,
        mood=tone,
        label=label,
        start_sec=0.0,
        end_sec=3.0,
    )


def digest(scene: TimedScene, t: float, *, seed: int = 0) -> str:
    image = draw_frame(scene, t, seed=seed, canvas=SMALL, supersample=1)
    return hashlib.sha256(image.tobytes()).hexdigest()


def test_a_frame_is_the_size_asked_for_and_the_same_twice() -> None:
    image = draw_frame(scene(), 1.0, canvas=SMALL, supersample=2)
    assert image.size == (270, 480)
    assert digest(scene(), 1.0) == digest(scene(), 1.0)


def test_time_the_seed_and_the_scene_each_change_the_picture() -> None:
    base = digest(scene(), 0.5)
    assert digest(scene(), 1.5) != base
    assert digest(scene(), 0.5, seed=7) != base
    assert digest(scene(mood=StickMood.HAPPY), 0.5) != base
    assert digest(scene(props=(StickProp.BALL,)), 0.5) != base
    assert digest(scene(tone=SceneMood.URGENT), 0.5) != base


@pytest.mark.parametrize("pose", list(StickPose))
def test_every_pose_draws(pose: StickPose) -> None:
    for t in (0.0, 0.7, 2.9):
        image = draw_frame(scene(pose=pose, actors=2), t, canvas=SMALL, supersample=1)
        assert image.getbbox() is not None


@pytest.mark.parametrize("mood", list(StickMood))
def test_every_mood_has_a_face(mood: StickMood) -> None:
    plain = digest(scene(actors=1), 1.0)
    assert digest(scene(mood=mood, actors=1), 1.0) != plain or mood is StickMood.NEUTRAL


@pytest.mark.parametrize("prop", list(StickProp))
def test_every_prop_draws_beside_one_two_and_three_figures(prop: StickProp) -> None:
    for actors in (0, 1, 3):
        image = draw_frame(
            scene(props=(prop,), actors=actors, label="GOAL"), 1.3, canvas=SMALL, supersample=1
        )
        assert image.getbbox() is not None


def test_three_props_of_every_kind_of_place_fit_in_one_frame() -> None:
    crowded = scene(props=(StickProp.SUN, StickProp.BUILDING, StickProp.TROPHY), actors=3)
    image = draw_frame(crowded, 2.0, canvas=SMALL, supersample=1)
    assert image.size == (270, 480)


def test_frame_count_rounds_to_whole_frames_and_never_to_none() -> None:
    assert frame_count(3.0, 30) == 90
    assert frame_count(0.01, 30) == 1
    assert frame_count(2.55, 24) == 61
