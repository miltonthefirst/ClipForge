"""Turning text into a voice, behind a port.

A port rather than a function because the choice of synthesiser is not settled
and should not have to be. Kokoro is the first adapter — 82M parameters,
Apache 2.0, 54 voices, faster than real time on CPU (docs/PLAN.md §M6) — and the
seam exists so that a better one arrives as a class rather than as a rewrite of
everything that calls it.

## Why the ONNX build and not the obvious one

The `kokoro` package on PyPI depends on PyTorch. This project does not have
PyTorch and does not want it: transcription runs on CTranslate2 precisely to
avoid ~2.5 GB of wheels and a class of CUDA version-matching failures
([ADR-0002](../../../docs/adr/0002-ctranslate2-without-pytorch.md)). Adding torch
for an 82M-parameter model that runs on a CPU would undo that decision for the
smallest model in the system. `kokoro-onnx` is the same weights on onnxruntime,
which is a 15 MB dependency and no CUDA coupling at all.

## Why it never touches the broker

Kokoro runs on the CPU lane and holds no VRAM, so it does not queue behind
Whisper or the analysis model (decision D11). A remake that only changes the
voice therefore costs nothing on the GPU and can run while a CLIP job is
transcribing. Rebuilding captions afterwards *does* need Whisper and therefore
does take a broker lease — that is a separate step, and it is why REBUILD is
slower than KEEP by more than the arithmetic suggests.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = [
    "DEFAULT_VOICES",
    "KokoroSynth",
    "SpeechError",
    "SpeechSynth",
    "Utterance",
    "kokoro_language",
    "write_wav",
]


class SpeechError(RuntimeError):
    """Speech could not be synthesised.

    Never retryable in practice: every cause is a missing dependency, a missing
    model file, an unknown voice or text the synthesiser cannot pronounce, and
    all four reproduce exactly on a second attempt.
    """


@dataclass(frozen=True)
class Utterance:
    """One synthesised passage, on disk."""

    path: Path
    duration_sec: float
    sample_rate: int
    text: str
    voice: str
    language: str
    engine: str


class SpeechSynth(Protocol):
    """What the REMAKE stage needs from a synthesiser, and nothing more."""

    @property
    def engine(self) -> str:
        """Name and version, recorded on the clip. Two voices of the same name
        from different engines do not sound alike, so the engine is part of the
        provenance rather than an implementation detail."""
        ...

    def voices(self) -> tuple[str, ...]: ...

    def speak(
        self,
        text: str,
        *,
        voice: str,
        language: str,
        speed: float,
        destination: Path,
    ) -> Utterance: ...


# BCP-47 in the contract, because that is what a client should speak. Kokoro's
# own codes are an implementation detail of one adapter and are mapped here.
_KOKORO_LANGUAGES: dict[str, str] = {
    "en": "en-us",
    "en-us": "en-us",
    "en-gb": "en-gb",
    "es": "es",
    "es-es": "es",
    "es-419": "es",
    "fr": "fr-fr",
    "fr-fr": "fr-fr",
    "hi": "hi",
    "it": "it",
    "pt": "pt-br",
    "pt-br": "pt-br",
    "ja": "ja",
    "ko": "ko",
    "zh": "cmn",
    "zh-cn": "cmn",
    "cmn": "cmn",
}

# One sensible voice per language, so a client that asks for a language without
# naming a voice gets speech rather than an error. Kokoro's naming is
# `<language><gender>_<name>`; these are its documented defaults.
DEFAULT_VOICES: dict[str, str] = {
    "en-us": "af_heart",
    "en-gb": "bf_emma",
    "es": "ef_dora",
    "fr-fr": "ff_siwis",
    "hi": "hf_alpha",
    "it": "if_sara",
    "pt-br": "pf_dora",
    "ja": "jf_alpha",
    "ko": "kf_yuna",
    "cmn": "zf_xiaobei",
}


def kokoro_language(language: str) -> str:
    """Map a BCP-47 tag onto the code Kokoro understands.

    Falls back to the primary subtag before giving up, so `en-AU` reaches
    `en-us` rather than failing — an approximate accent is a far better outcome
    than no narration, and the clip records which voice actually spoke.
    """
    wanted = language.strip().lower()
    if wanted in _KOKORO_LANGUAGES:
        return _KOKORO_LANGUAGES[wanted]
    primary = wanted.split("-")[0]
    if primary in _KOKORO_LANGUAGES:
        return _KOKORO_LANGUAGES[primary]
    raise SpeechError(
        f"no voice available for language {language!r}; "
        f"supported: {', '.join(sorted(set(_KOKORO_LANGUAGES)))}"
    )


def write_wav(samples: np.ndarray, *, sample_rate: int, destination: Path) -> float:
    """Write float samples as 16-bit PCM, and return the duration.

    The stdlib `wave` module rather than soundfile: this is the only place the
    project writes audio itself, and a dependency for one function that the
    standard library already provides is a dependency to explain forever.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    flattened = np.asarray(samples, dtype=np.float32).reshape(-1)
    if flattened.size == 0:
        raise SpeechError("the synthesiser returned no audio")

    # Clip before scaling. Float TTS output occasionally exceeds unity on
    # sibilants, and letting int16 wrap turns that into a loud click.
    clipped = np.clip(flattened, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2")

    with wave.open(str(destination), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())
    return len(pcm) / float(sample_rate)


class KokoroSynth:
    """Kokoro-82M on onnxruntime.

    The model is loaded on first use and then kept: it is ~310 MB of weights on
    the CPU, loading costs a couple of seconds, and unlike a GPU model there is
    nothing it is competing with by staying resident.
    """

    def __init__(self, *, model_path: Path, voices_path: Path) -> None:
        self._model_path = model_path
        self._voices_path = voices_path
        self._kokoro: object | None = None
        self._version = "kokoro-v1.0"

    @property
    def engine(self) -> str:
        return f"{self._version}-onnx"

    def _loaded(self) -> object:
        if self._kokoro is not None:
            return self._kokoro

        try:
            from kokoro_onnx import Kokoro
        except ImportError as exc:
            raise SpeechError(
                "the speech extra is not installed. "
                "Run: uv sync --project apps/worker --extra speech"
            ) from exc

        for path, what in ((self._model_path, "model"), (self._voices_path, "voice pack")):
            if not path.is_file():
                raise SpeechError(
                    f"the Kokoro {what} is not at {path}. Run: clipforge-worker fetch-voices"
                )

        log.info("speech.loading", model=str(self._model_path))
        self._kokoro = Kokoro(str(self._model_path), str(self._voices_path))
        return self._kokoro

    def voices(self) -> tuple[str, ...]:
        kokoro = self._loaded()
        getter = getattr(kokoro, "get_voices", None)
        if getter is None:
            return tuple(sorted(set(DEFAULT_VOICES.values())))
        return tuple(sorted(getter()))

    def speak(
        self,
        text: str,
        *,
        voice: str,
        language: str,
        speed: float,
        destination: Path,
    ) -> Utterance:
        cleaned = " ".join(text.split())
        if not cleaned:
            raise SpeechError("there is nothing to say: the script is empty")

        lang = kokoro_language(language)
        chosen = voice or DEFAULT_VOICES.get(lang, "af_heart")
        kokoro = self._loaded()

        try:
            samples, sample_rate = kokoro.create(  # type: ignore[attr-defined]
                cleaned, voice=chosen, speed=max(0.5, min(2.0, speed)), lang=lang
            )
        except Exception as exc:  # the library raises bare exceptions
            raise SpeechError(f"{chosen} could not speak this text: {exc}") from exc

        duration = write_wav(samples, sample_rate=int(sample_rate), destination=destination)
        log.info(
            "speech.spoken",
            voice=chosen,
            language=lang,
            seconds=round(duration, 2),
            characters=len(cleaned),
        )
        return Utterance(
            path=destination,
            duration_sec=duration,
            sample_rate=int(sample_rate),
            text=cleaned,
            voice=chosen,
            language=lang,
            engine=self.engine,
        )
