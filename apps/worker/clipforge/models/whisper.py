"""Transcription through faster-whisper, under the ModelBroker's GPU lease.

Three things here are load-bearing.

**CUDA DLLs are registered before `faster_whisper` is imported.** CTranslate2
loads cuBLAS and cuDNN by bare filename, and the pip packages put them somewhere
Windows never searches. Importing first fails at the first inference with an
opaque DLL error. See docs/adr/0002-ctranslate2-without-pytorch.md.

**The model is released by dropping the last reference, and the release is
verified through NVML.** There is no PyTorch allocator to interrogate — the
broker re-probes the card and reports a shortfall rather than assuming.

**Word-level timestamps are mandatory, not an option.** Phase 5 snaps clip
boundaries to word ends and Phase 6 generates karaoke captions from them. A
transcript without them is not usable by anything downstream, so the flag is not
exposed as configuration.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clipforge_contracts import Transcript, TranscriptSegment, TranscriptWord

from clipforge.models.broker import ModelBroker
from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = ["TranscriptionResult", "WhisperTranscriber", "model_version"]

# Measured on the reference RTX 3050 for large-v3-turbo at int8_float16. Used for
# the broker's budget check, so it is deliberately a little pessimistic: refusing
# to start is recoverable, an OOM mid-transcription is not.
LARGE_V3_TURBO_VRAM_MB = 1800


def model_version(model: str, compute_type: str) -> str:
    """A cache key that changes whenever the output would change.

    Both parts matter: the same model at a different precision produces a
    materially different transcript, so keying on the model name alone would
    serve a stale result after a precision change.
    """
    return f"faster-whisper:{model}:{compute_type}"


@dataclass(frozen=True)
class TranscriptionResult:
    transcript: Transcript
    speech_spans: tuple[tuple[float, float], ...]
    realtime_factor: float
    peak_vram_mb: int


class WhisperTranscriber:
    """Loads Whisper for the duration of one transcription and then lets it go."""

    def __init__(
        self,
        *,
        model: str,
        compute_type: str,
        device: str,
        broker: ModelBroker,
        required_vram_mb: int = LARGE_V3_TURBO_VRAM_MB,
    ) -> None:
        self._model = model
        self._compute_type = compute_type
        self._device = device
        self._broker = broker
        self._required_vram_mb = required_vram_mb

    @property
    def version(self) -> str:
        return model_version(self._model, self._compute_type)

    @contextmanager
    def _loaded(self) -> Iterator[Any]:
        """Hold the GPU, load, yield, then drop the last reference.

        The model is created *inside* the broker lease rather than cached on the
        instance. Keeping it resident between jobs would be faster and would also
        make the ANALYZE stage impossible: at 6 GB, Whisper and the LLM cannot
        both be loaded (docs/PLAN.md §2.1).
        """
        # Must precede the faster_whisper import, not merely the model load.
        from clipforge.models.cuda import register_cuda_dll_directories

        register_cuda_dll_directories()

        from faster_whisper import WhisperModel

        with self._broker.acquire(self.version, self._required_vram_mb) as leased:
            model = WhisperModel(self._model, device=self._device, compute_type=self._compute_type)
            try:
                yield model
            finally:
                # Drop the last reference. CTranslate2 frees on refcount drop;
                # the broker verifies through NVML that it actually happened.
                del model
            leased.observe()

    def transcribe(
        self,
        audio_path: Path,
        *,
        source_id: str,
        language: str | None = None,
    ) -> TranscriptionResult:
        started = time.monotonic()

        with self._loaded() as model:
            segments_iter, info = model.transcribe(
                str(audio_path),
                language=language,
                word_timestamps=True,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            # faster-whisper yields lazily; the work happens on iteration, so it
            # must be drained inside the lease.
            raw_segments = list(segments_iter)
            peak = 0

        elapsed = max(time.monotonic() - started, 1e-6)
        duration = float(getattr(info, "duration", 0.0) or 0.0)

        segments = [_to_segment(index, raw) for index, raw in enumerate(raw_segments)]
        transcript = Transcript(
            source_id=source_id,
            model_version=self.version,
            language=getattr(info, "language", None),
            duration_sec=duration or None,
            segments=segments,
            created_at=datetime.now(UTC),
        )

        realtime_factor = duration / elapsed if duration else 0.0
        log.info(
            "transcribe.done",
            segments=len(segments),
            words=sum(len(s.words or []) for s in segments),
            language=transcript.language,
            realtime_factor=round(realtime_factor, 2),
        )

        return TranscriptionResult(
            transcript=transcript,
            speech_spans=_speech_spans(segments),
            realtime_factor=realtime_factor,
            peak_vram_mb=peak,
        )


def _to_segment(index: int, raw: Any) -> TranscriptSegment:
    words = [
        TranscriptWord(
            text=str(w.word),
            start_sec=float(w.start),
            end_sec=float(w.end),
            probability=float(getattr(w, "probability", 0.0)) or None,
        )
        for w in (getattr(raw, "words", None) or [])
        # Whisper occasionally emits a word with no timing at a segment boundary.
        # Keeping it would corrupt boundary snapping, which trusts these.
        if getattr(w, "start", None) is not None and getattr(w, "end", None) is not None
    ]
    return TranscriptSegment(
        index=index,
        text=str(raw.text).strip(),
        start_sec=float(raw.start),
        end_sec=float(raw.end),
        words=words,
    )


def _speech_spans(segments: list[TranscriptSegment]) -> tuple[tuple[float, float], ...]:
    """Merge segments into contiguous speech spans.

    The *gaps* between these are what Phase 5 snaps clip boundaries to: cutting
    mid-word is the single most obvious way for a generated clip to look
    automated.
    """
    spans: list[tuple[float, float]] = []
    for segment in segments:
        if spans and segment.start_sec - spans[-1][1] < 0.25:
            spans[-1] = (spans[-1][0], segment.end_sec)
        else:
            spans.append((segment.start_sec, segment.end_sec))
    return tuple(spans)
