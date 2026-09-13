"""Ollama client with JSON-schema-constrained output.

The schema is derived from the Pydantic model and handed to Ollama as its
`format`, so the *runtime* rejects malformed output rather than this code parsing
defensively around it. That moves the failure from "we got something we cannot
use" to "the model was not allowed to say that", which is a much better place for
it.

A residual failure rate survives anyway — constrained decoding guarantees the
shape, not the semantics, so a model can still emit `endSec` before `startSec` or
a hook it invented. Hence one bounded repair retry: the validation error is fed
back verbatim and the model is asked to fix it. The **pre-repair** rate is
recorded separately, because a rising need for repairs is the early warning that
a prompt or model change has degraded, and a metric that folded repairs into
successes would hide exactly that.

`keep_alive=0` is not an optimisation. Ollama holds a model resident by default,
and at 6 GB of VRAM a resident LLM makes the next transcription impossible
(docs/PLAN.md §2.1). The broker's lease is only meaningful if the model actually
leaves when the lease ends.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = ["OllamaClient", "OllamaError", "StructuredCallStats"]


class OllamaError(RuntimeError):
    """Ollama could not be reached, or would not produce usable output."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        self.retryable = retryable
        super().__init__(message)


@dataclass
class StructuredCallStats:
    """How well the model is behaving, across a run.

    Kept as a running tally rather than logged per call: one repair is noise, a
    repair rate of 30% is a signal worth acting on.
    """

    calls: int = 0
    first_attempt_ok: int = 0
    repaired: int = 0
    failed: int = 0
    durations_sec: list[float] = field(default_factory=list)

    @property
    def first_attempt_rate(self) -> float:
        return self.first_attempt_ok / self.calls if self.calls else 0.0

    @property
    def success_rate(self) -> float:
        return (self.first_attempt_ok + self.repaired) / self.calls if self.calls else 0.0

    def summary(self) -> dict[str, float | int]:
        return {
            "calls": self.calls,
            "firstAttemptOk": self.first_attempt_ok,
            "repaired": self.repaired,
            "failed": self.failed,
            "firstAttemptRate": round(self.first_attempt_rate, 3),
            "successRate": round(self.success_rate, 3),
            "meanSeconds": round(sum(self.durations_sec) / len(self.durations_sec), 2)
            if self.durations_sec
            else 0.0,
        }


class OllamaClient:
    """Structured generation against a local Ollama server."""

    def __init__(
        self,
        *,
        host: str,
        model: str,
        num_ctx: int = 16384,
        timeout_s: float = 180.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._host = host.rstrip("/")
        self._model = model
        self._num_ctx = num_ctx
        self._timeout_s = timeout_s
        self._transport = transport
        self.stats = StructuredCallStats()

    @property
    def model(self) -> str:
        return self._model

    def _client(self, timeout_s: float | None = None) -> httpx.Client:
        return httpx.Client(
            base_url=self._host,
            timeout=timeout_s if timeout_s is not None else self._timeout_s,
            transport=self._transport,
        )

    def is_available(self) -> bool:
        try:
            with self._client() as client:
                return client.get("/api/tags").status_code == 200
        except httpx.HTTPError:
            return False

    def generate_structured[T: BaseModel](
        self,
        *,
        schema_model: type[T],
        system: str,
        prompt: str,
        temperature: float = 0.0,
        seed: int = 0,
        images: Sequence[str] | None = None,
        model: str | None = None,
        timeout_s: float | None = None,
    ) -> T:
        """Generate a response guaranteed to parse as ``schema_model``.

        `images` are base64-encoded stills, for a multimodal model. `model`
        overrides the configured one, because looking at a picture and reading
        a sentence are not the same job and are not the same weights — the
        vision model is several times the size and is used for one call per
        clip, so it is named per call rather than made the client's identity.
        `timeout_s` exists for the same reason: a 15 GB model that does not fit
        in 6 GB of VRAM answers in a minute, not in seconds.

        Temperature 0 and a fixed seed by default, because Phase 5's golden test
        requires a fixed transcript to produce a stable candidate set. Selection
        is a judgement, not a creative act — there is nothing to be gained from
        sampling.
        """
        self.stats.calls += 1
        started = time.monotonic()
        schema = schema_model.model_json_schema()

        try:
            raw = self._call(
                system,
                prompt,
                schema,
                temperature,
                seed,
                images=images,
                model=model,
                timeout_s=timeout_s,
            )
            parsed = schema_model.model_validate_json(raw)
        except ValidationError as first_error:
            parsed = self._repair(
                schema_model,
                system,
                prompt,
                schema,
                temperature,
                seed,
                raw,
                first_error,
                images=images,
                model=model,
                timeout_s=timeout_s,
            )
        else:
            self.stats.first_attempt_ok += 1
            self.stats.durations_sec.append(time.monotonic() - started)
            return parsed

        self.stats.durations_sec.append(time.monotonic() - started)
        return parsed

    def _repair[T: BaseModel](
        self,
        schema_model: type[T],
        system: str,
        prompt: str,
        schema: dict[str, Any],
        temperature: float,
        seed: int,
        raw: str,
        error: ValidationError,
        images: Sequence[str] | None = None,
        model: str | None = None,
        timeout_s: float | None = None,
    ) -> T:
        """One retry, with the validation error fed back verbatim.

        Exactly one. A model that cannot satisfy its own schema twice will not
        satisfy it on the third attempt either, and each try costs seconds of GPU
        time that the rest of the pipeline is waiting on.
        """
        log.warning("ollama.repair_attempt", errors=error.error_count())
        repair_prompt = (
            f"{prompt}\n\n"
            f"Your previous response was rejected:\n{raw[:1500]}\n\n"
            f"It failed validation with:\n{error}\n\n"
            "Return corrected JSON that satisfies the schema. Nothing else."
        )
        try:
            repaired_raw = self._call(
                system,
                repair_prompt,
                schema,
                temperature,
                seed + 1,
                images=images,
                model=model,
                timeout_s=timeout_s,
            )
            parsed = schema_model.model_validate_json(repaired_raw)
        except ValidationError as second_error:
            self.stats.failed += 1
            raise OllamaError(
                f"{self._model} produced output that failed validation twice: {second_error}",
                # Deterministic settings mean a third attempt produces the same
                # thing. This is a prompt or model problem, not a transient one.
                retryable=False,
            ) from second_error

        self.stats.repaired += 1
        return parsed

    def _call(
        self,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        temperature: float,
        seed: int,
        *,
        images: Sequence[str] | None = None,
        model: str | None = None,
        timeout_s: float | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model or self._model,
            "system": system,
            "prompt": prompt,
            "stream": False,
            "format": schema,
            "options": {
                "temperature": temperature,
                "seed": seed,
                "num_ctx": self._num_ctx,
            },
            # Release the model as soon as the call returns. At 6 GB a resident
            # LLM makes the next transcription impossible.
            "keep_alive": 0,
            # Reasoning models put their chain of thought in a separate
            # `thinking` field and — when constrained by `format` — return an
            # EMPTY `response`. qwen3.5:4b, the configured default, does exactly
            # this: the call succeeds, the model reasons at length, and nothing
            # usable comes back. Disabling thinking is what makes structured
            # output work at all on such a model.
            #
            # It is also the right trade here. Selection is a bounded judgement
            # against an explicit rubric, not a problem that rewards extended
            # reasoning, and thinking tokens cost GPU seconds per window across
            # dozens of windows. Models without a thinking mode ignore this.
            "think": False,
        }
        if images:
            # Base64 stills, as Ollama's /api/generate takes them. Absent for
            # every text call, because a `images: []` key changes how some
            # models route the request — they are not the same code path.
            payload["images"] = list(images)

        try:
            with self._client(timeout_s) as client:
                response = client.post("/api/generate", json=payload)
        except httpx.HTTPError as exc:
            raise OllamaError(
                f"could not reach Ollama at {self._host}: {exc}. Is `ollama serve` running?"
            ) from exc

        if response.status_code == 404:
            raise OllamaError(
                f"model {self._model!r} is not pulled. Run: ollama pull {self._model}",
                retryable=False,
            )
        if response.status_code >= 400:
            raise OllamaError(f"Ollama returned {response.status_code}: {response.text[:300]}")

        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise OllamaError("Ollama returned a non-JSON envelope") from exc

        content = body.get("response")
        if not isinstance(content, str) or not content.strip():
            raise OllamaError("Ollama returned an empty response")
        return content
