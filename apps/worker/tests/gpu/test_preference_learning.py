"""What the local model does with a correction, against the real model.

The GPU tier, because it needs Ollama. It is the only place the shape of
`LlmPreferenceProposal` can be judged, and the shape is the whole feature: an
earlier version asked only for a list of preferences, and qwen3.5:4b returned an
empty one on *every* case measured — including a note that plainly taught two
things. An empty array satisfies an array schema trivially, so it is the cheapest
answer available, and a small model takes the cheapest answer.

Making it commit to `recurring` and justify it *before* the list exists turns the
same model into one that answers. That is the same lesson `LlmRemakeNote` records
about nullable fields, arrived at independently.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from clipforge.analysis.preferences import dedupe, propose
from clipforge.config import Settings
from clipforge.models.ollama import OllamaClient
from clipforge_contracts import (
    AppliedRemake,
    AppliedVoice,
    Clip,
    ClipLocation,
    FramingMode,
    NoteTopic,
    Preference,
    PreferenceScope,
    PreferenceStatus,
    ReviewState,
    SpeechMode,
)

pytestmark = pytest.mark.gpu

NOW = datetime(2026, 9, 13, tzinfo=UTC)
SOURCE_TITLE = "Le Bayern fait le show a domicile - LDC 2026/2027"


@pytest.fixture(scope="module")
def client() -> OllamaClient:
    settings = Settings(_env_file=None)
    client = OllamaClient(host=settings.ollama_host, model=settings.ollama_model)
    if not client.is_available():
        pytest.skip(f"Ollama not reachable at {settings.ollama_host}")
    return client


def clip() -> Clip:
    return Clip(
        id="clip-1",
        uid="user-1",
        candidate_id="cand-1",
        source_id="src-1",
        location=ClipLocation.LOCAL,
        local_path="x.mp4",
        review=ReviewState.PENDING,
        created_at=NOW,
    )


def tracked_and_voiced() -> AppliedRemake:
    return AppliedRemake(
        framing_mode=FramingMode.TRACK,
        start_sec=66.7,
        end_sec=83.7,
        voice=AppliedVoice(
            mode=SpeechMode.REPLACE,
            voice="af_heart",
            language="en-us",
            engine="kokoro-v1.0-onnx",
            translated=True,
        ),
    )


def held(lesson: str, status: PreferenceStatus) -> Preference:
    return Preference(
        id="held",
        uid="user-1",
        scope=PreferenceScope.SOURCE,
        source_id="src-1",
        category=NoteTopic.FRAMING,
        lesson=lesson,
        status=status,
        created_at=NOW,
    )


def learn(
    client: OllamaClient, note: str, applied: AppliedRemake, known: list[Preference]
) -> tuple[list[Preference], list[Preference]]:
    proposed = propose(
        client,
        note=note,
        applied=applied,
        clip=clip(),
        source_title=SOURCE_TITLE,
        known=known,
        uid="user-1",
    )
    return proposed, dedupe(proposed, known)


def test_a_recurring_ask_is_learned(client: OllamaClient) -> None:
    """The reviewer's actual note, verbatim.

    They asked for English commentary on a French source. The next clip from
    that source will need the same thing, and a system that cannot hold it makes
    them type it every time.
    """
    _, fresh = learn(
        client,
        "Let's change commentary voice to English and also try to follow the ball "
        "with view area for this clip",
        tracked_and_voiced(),
        [],
    )
    assert fresh, "a note teaching a standing preference must produce one"
    assert all(p.status is PreferenceStatus.PROPOSED for p in fresh), "never auto-applied"
    assert all(p.source_id == "src-1" for p in fresh if p.scope is PreferenceScope.SOURCE)


def test_a_complaint_about_one_moment_teaches_nothing(client: OllamaClient) -> None:
    """ "It cuts in late on this one" is about this one.

    The expensive mistake is a wrong standing rule, so silence here matters as
    much as speech in the test above.
    """
    _, fresh = learn(
        client,
        "it cuts in about three seconds too late on this one",
        AppliedRemake(framing_mode=FramingMode.AS_RENDERED, start_sec=10, end_sec=20),
        [],
    )
    assert fresh == []


def test_something_already_accepted_is_not_proposed_again(client: OllamaClient) -> None:
    """The model may re-derive it; dedupe is the guarantee that it stops there."""
    known = [
        held(
            "this channel's wide pitch shots need the window to follow the ball",
            PreferenceStatus.ACCEPTED,
        )
    ]
    _, fresh = learn(client, "follow the ball again please", tracked_and_voiced(), known)
    assert fresh == []


def test_something_already_rejected_never_comes_back(client: OllamaClient) -> None:
    """The difference between a system that learns and one that nags.

    Deduping against accepted preferences alone would let a turned-down
    suggestion return on every correction of the same kind of clip.
    """
    known = [
        held(
            "narration should sit over the crowd noise rather than replace it",
            PreferenceStatus.REJECTED,
        )
    ]
    _, fresh = learn(
        client, "keep the crowd noise under the narration", tracked_and_voiced(), known
    )
    assert fresh == []
