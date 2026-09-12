"""The Ollama client: schema constraint, the repair loop, and its statistics.

Phase 5, exit criterion 2 — across many windows, every response parses after at
most one repair, and the **pre-repair** rate is recorded separately. That
separation is the point: a metric that folded repairs into successes would hide
the exact signal worth watching, which is a repair rate that climbs after a
prompt or model change.

Driven through `httpx.MockTransport`, so no Ollama server is involved and the
malformed responses can be scripted precisely.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from clipforge.models.ollama import OllamaClient, OllamaError
from clipforge_contracts import LlmClipResponse

VALID_CLIP = {
    "startSec": 10.0,
    "endSec": 40.0,
    "subScores": {
        "hook": 20,
        "curiosity": 16,
        "standalone": 16,
        "emotion": 12,
        "pacing": 8,
        "shareability": 8,
    },
    "hook": "a strong opening",
    "reason": "clear payoff",
}


def envelope(body: object) -> dict[str, object]:
    """Ollama wraps the model's text in a `response` field."""
    return {"response": json.dumps(body) if not isinstance(body, str) else body}


def client_with(handler: Callable[[httpx.Request], httpx.Response]) -> OllamaClient:
    return OllamaClient(
        host="http://ollama.invalid",
        model="qwen3.5:4b",
        transport=httpx.MockTransport(handler),
    )


def scripted(*responses: object) -> Callable[[httpx.Request], httpx.Response]:
    """Return each scripted body in turn, repeating the last one thereafter."""
    remaining = list(responses)

    def handler(_request: httpx.Request) -> httpx.Response:
        body = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return httpx.Response(200, json=envelope(body))

    return handler


def generate(client: OllamaClient) -> LlmClipResponse:
    return client.generate_structured(
        schema_model=LlmClipResponse, system="system", prompt="prompt"
    )


# ─────────────────────────────────────────────────────────────────────────────
# The request itself
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_the_schema_is_sent_so_the_runtime_enforces_the_shape() -> None:
    """Constraining at the runtime moves the failure from 'we got something we
    cannot use' to 'the model was not allowed to say that'."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=envelope({"clips": []}))

    generate(client_with(handler))

    assert "format" in captured
    assert captured["format"] == LlmClipResponse.model_json_schema()


@pytest.mark.unit
def test_the_model_is_released_as_soon_as_the_call_returns() -> None:
    """`keep_alive=0` is not an optimisation. At 6 GB a resident LLM makes the
    next transcription impossible, so the broker's lease is only meaningful if
    the model actually leaves."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=envelope({"clips": []}))

    generate(client_with(handler))
    assert captured["keep_alive"] == 0


@pytest.mark.unit
def test_thinking_is_disabled_so_structured_output_is_not_empty() -> None:
    """Found against the real model, not by reading docs.

    A reasoning model puts its chain of thought in a separate `thinking` field
    and — when constrained by `format` — returns an EMPTY `response`. qwen3.5:4b,
    the configured default, does exactly that: the call succeeds, the model
    reasons at length, and nothing usable comes back.
    """
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=envelope({"clips": []}))

    generate(client_with(handler))
    assert captured["think"] is False


@pytest.mark.unit
def test_generation_is_deterministic_by_default() -> None:
    """Selection is a judgement, not a creative act — and the golden test needs a
    fixed transcript to produce a stable candidate set."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=envelope({"clips": []}))

    generate(client_with(handler))
    options = captured["options"]
    assert isinstance(options, dict)
    assert options["temperature"] == 0.0
    assert options["seed"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 2 — parsing, repair, and the recorded rates
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_a_valid_response_parses_first_time() -> None:
    client = client_with(scripted({"clips": [VALID_CLIP]}))
    result = generate(client)

    assert len(result.clips) == 1
    assert client.stats.first_attempt_ok == 1
    assert client.stats.repaired == 0
    assert client.stats.first_attempt_rate == 1.0


@pytest.mark.unit
def test_a_semantically_invalid_response_is_repaired_once() -> None:
    """Constrained decoding guarantees the shape, not the semantics — a model can
    still return a sub-score above its rubric maximum."""
    over_max = {**VALID_CLIP, "subScores": {**VALID_CLIP["subScores"], "hook": 99}}  # type: ignore[dict-item]
    client = client_with(scripted({"clips": [over_max]}, {"clips": [VALID_CLIP]}))

    result = generate(client)

    assert len(result.clips) == 1
    assert client.stats.first_attempt_ok == 0
    assert client.stats.repaired == 1
    assert client.stats.success_rate == 1.0


@pytest.mark.unit
def test_the_repair_prompt_carries_the_validation_error_back() -> None:
    """Telling the model only that it was wrong is far less effective than
    telling it exactly how."""
    prompts: list[str] = []

    responses = [{"clips": [{**VALID_CLIP, "hook": None}]}, {"clips": [VALID_CLIP]}]

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        prompts.append(payload["prompt"])
        return httpx.Response(200, json=envelope(responses[min(len(prompts) - 1, 1)]))

    generate(client_with(handler))

    assert len(prompts) == 2
    assert "rejected" in prompts[1]
    assert "hook" in prompts[1]


@pytest.mark.unit
def test_failing_validation_twice_is_not_retried_a_third_time() -> None:
    """Deterministic settings mean a third attempt produces the same thing. This
    is a prompt or model problem, not a transient one."""
    bad = {"clips": [{**VALID_CLIP, "startSec": "not a number"}]}
    client = client_with(scripted(bad))

    with pytest.raises(OllamaError) as caught:
        generate(client)

    assert caught.value.retryable is False
    assert client.stats.failed == 1


@pytest.mark.unit
def test_the_pre_repair_rate_is_recorded_separately() -> None:
    """Phase 5, exit criterion 2. A rising repair rate is the early warning that
    a prompt or model change has degraded — and folding repairs into successes
    would hide exactly that."""
    over_max = {**VALID_CLIP, "subScores": {**VALID_CLIP["subScores"], "hook": 99}}  # type: ignore[dict-item]
    sequence = [
        {"clips": [VALID_CLIP]},
        {"clips": [VALID_CLIP]},
        {"clips": [over_max]},
        {"clips": [VALID_CLIP]},
    ]
    index = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal index
        body = sequence[min(index, len(sequence) - 1)]
        index += 1
        return httpx.Response(200, json=envelope(body))

    client = client_with(handler)
    for _ in range(3):
        generate(client)

    summary = client.stats.summary()
    assert summary["calls"] == 3
    assert summary["firstAttemptOk"] == 2
    assert summary["repaired"] == 1
    assert summary["firstAttemptRate"] == pytest.approx(0.667, abs=0.01)
    assert summary["successRate"] == 1.0


@pytest.mark.unit
def test_an_empty_clip_list_is_a_valid_answer() -> None:
    """Most 120-second stretches contain nothing worth clipping, and the prompt
    says so. The client must not treat that as a failure."""
    client = client_with(scripted({"clips": []}))
    assert generate(client).clips == []
    assert client.stats.first_attempt_ok == 1


# ─────────────────────────────────────────────────────────────────────────────
# Failure modes that are not the model's fault
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_an_unreachable_server_says_how_to_fix_it() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(OllamaError, match="ollama serve") as caught:
        generate(client_with(handler))
    assert caught.value.retryable is True


@pytest.mark.unit
def test_a_missing_model_names_the_pull_command() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "model not found"})

    with pytest.raises(OllamaError, match="ollama pull") as caught:
        generate(client_with(handler))
    assert caught.value.retryable is False, "pulling will not happen on its own"


@pytest.mark.unit
def test_an_empty_response_is_reported_rather_than_parsed() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": "   "})

    with pytest.raises(OllamaError, match="empty"):
        generate(client_with(handler))


@pytest.mark.unit
def test_availability_can_be_checked_without_generating() -> None:
    """`doctor` and the worker's capability advertisement both need this."""
    assert client_with(lambda _r: httpx.Response(200, json={"models": []})).is_available()
    assert not client_with(lambda _r: httpx.Response(500)).is_available()
