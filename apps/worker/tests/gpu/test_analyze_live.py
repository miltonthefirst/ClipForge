"""Selection against the real local model.

Phase 5, exit criterion 2 — every response parses against the schema after at
most one repair, with the pre-repair rate recorded. The mocked half of this lives
in tests/unit/test_ollama.py, which can script malformed responses precisely;
what *this* proves is the thing mocks cannot, that the actual model at the actual
context size honours the constraint.

Opt-in (`-m gpu`): it needs Ollama running with the configured model pulled.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from clipforge.analysis.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_prompt
from clipforge.analysis.windows import WindowSpec, build_windows, render_window
from clipforge.config import Settings
from clipforge.models.ollama import OllamaClient
from clipforge.stages.analyze import select_candidates
from clipforge_contracts import LlmClipResponse, Transcript, TranscriptSegment, TranscriptWord

pytestmark = pytest.mark.gpu

# Enough windows to be a rate rather than an anecdote, few enough to run in
# tolerable time on a 6 GB card.
WINDOW_COUNT = 10


@pytest.fixture(scope="module")
def client() -> OllamaClient:
    settings = Settings()
    ollama = OllamaClient(
        host=settings.ollama_host,
        model=settings.ollama_model,
        num_ctx=settings.ollama_num_ctx,
    )
    if not ollama.is_available():
        pytest.skip(f"Ollama is not reachable at {settings.ollama_host}")
    return ollama


def _transcript() -> Transcript:
    """A synthetic interview with real sentence structure.

    Deliberately not the LibriVox fixture: that is 26 seconds, which is one
    window, and one window is not a rate.
    """
    lines = [
        "The thing nobody tells you about running your own hardware is the electricity bill.",
        "I genuinely thought it would be the noise, or the heat, but it was the meter.",
        "So I started measuring everything, and the numbers were not what I expected at all.",
        "A GPU sitting idle costs almost nothing. A GPU at full load for an hour costs real money.",
        "Which means the expensive thing is not owning the card, it is keeping it busy.",
        "And that completely inverts how you think about scheduling work on it.",
        "You stop trying to maximise utilisation and start trying to minimise wasted passes.",
        "Every job you run twice is money you set on fire for no reason whatsoever.",
        "That is why caching matters more than throughput on a machine like this one.",
        "The fastest transcription is the one you already did and never have to repeat.",
    ]
    segments: list[TranscriptSegment] = []
    cursor = 0.0
    for index, text in enumerate(lines * 4):
        words = text.split()
        per_word = 0.42
        segment_words = [
            TranscriptWord(
                text=word,
                start_sec=cursor + i * per_word,
                end_sec=cursor + i * per_word + per_word * 0.85,
            )
            for i, word in enumerate(words)
        ]
        end = cursor + len(words) * per_word
        segments.append(
            TranscriptSegment(
                index=index, text=text, start_sec=cursor, end_sec=end, words=segment_words
            )
        )
        cursor = end + 0.4

    return Transcript(
        source_id="live-fixture",
        model_version="test",
        language="en",
        duration_sec=cursor,
        segments=segments,
        created_at=datetime.now(UTC),
    )


def test_every_response_parses_after_at_most_one_repair(client: OllamaClient) -> None:
    """Phase 5, exit criterion 2."""
    transcript = _transcript()
    windows = build_windows(transcript, WindowSpec(120.0, 30.0))[:WINDOW_COUNT]
    assert windows, "the fixture produced no windows"

    for window in windows:
        client.generate_structured(
            schema_model=LlmClipResponse,
            system=SYSTEM_PROMPT,
            prompt=build_prompt(
                window_text=render_window(window),
                start_sec=window.start_sec,
                end_sec=window.end_sec,
            ),
        )

    summary = client.stats.summary()
    print(f"\nprompt {PROMPT_VERSION} · {summary}")

    assert summary["failed"] == 0, f"a window failed twice: {summary}"
    assert summary["successRate"] == 1.0
    # The pre-repair rate is recorded rather than asserted tightly: it is the
    # trend that matters, and a hard floor here would fail on a model update
    # rather than on a real regression.
    assert summary["firstAttemptRate"] >= 0.5, (
        f"more than half of responses needed repair, which suggests the prompt or "
        f"the schema has drifted from what the model can satisfy: {summary}"
    )


def test_the_whole_selection_runs_end_to_end_against_the_real_model(
    client: OllamaClient,
) -> None:
    """Not an accuracy assertion — a 4B model's taste is not something to gate a
    build on. What this checks is that the *contract* holds against real output:
    boundaries land inside the source, durations obey the filter, and nothing
    overlaps beyond the threshold."""
    transcript = _transcript()
    speech = tuple((s.start_sec, s.end_sec) for s in transcript.segments)

    def propose(window: object) -> LlmClipResponse:
        return client.generate_structured(
            schema_model=LlmClipResponse,
            system=SYSTEM_PROMPT,
            prompt=build_prompt(
                window_text=render_window(window),  # type: ignore[arg-type]
                start_sec=window.start_sec,  # type: ignore[attr-defined]
                end_sec=window.end_sec,  # type: ignore[attr-defined]
            ),
        )

    selected = select_candidates(transcript, speech, propose=propose, limit=5)

    duration = transcript.duration_sec or 0.0
    for candidate in selected:
        assert 0.0 <= candidate.start_sec < candidate.end_sec <= duration
        assert 15.0 <= candidate.duration_sec <= 75.0
        assert 0 <= candidate.total <= 100
        assert candidate.hook.strip()
    assert len(selected) <= 5
