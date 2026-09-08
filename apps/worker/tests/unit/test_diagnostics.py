"""Unit coverage for the parts of diagnostics that need no hardware."""

from __future__ import annotations

import wave
from pathlib import Path

import pytest
from clipforge import diagnostics
from clipforge.diagnostics import (
    CheckResult,
    _write_spoken_tone_wav,
    has_capability,
    probe_ffmpeg,
    run_all,
)


@pytest.mark.unit
def test_smoke_wav_is_16khz_mono_pcm(tmp_path: Path) -> None:
    """The smoke fixture must match what faster-whisper expects: 16 kHz mono."""
    target = tmp_path / "smoke.wav"
    _write_spoken_tone_wav(target, seconds=1.0)

    with wave.open(str(target), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == 16000
        assert handle.getsampwidth() == 2
        assert handle.getnframes() == 16000


@pytest.mark.unit
def test_missing_ffmpeg_fails_without_raising() -> None:
    """A missing binary must be reported, never raised: doctor has to keep going."""
    result = probe_ffmpeg(binary="clipforge-definitely-not-a-real-binary")
    assert result.ok is False
    assert "not runnable" in result.detail


@pytest.mark.unit
def test_check_result_is_immutable() -> None:
    result = CheckResult(name="x", ok=True, detail="d")
    with pytest.raises(AttributeError):
        result.ok = False  # type: ignore[misc]


@pytest.mark.unit
def test_run_all_can_skip_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI has no GPU, so the report must be constructible without one.

    Every probe is stubbed: the `unit` tier must touch no network and no
    subprocess (docs/PLAN.md §6).
    """
    stub = CheckResult(name="stub", ok=True, detail="")
    for probe in ("probe_ffmpeg", "probe_ollama", "probe_gpu"):
        monkeypatch.setattr(diagnostics, probe, lambda *_, **__: stub)

    names = [check.name for check in run_all(include_gpu=False)]
    assert "cuda_transcribe" not in names
    assert len(names) == 3


# --- ffmpeg capability matching -------------------------------------------
#
# Real `ffmpeg -filters` output. Note that 18 lines contain "ass" as a
# substring while only one declares the libass filter, which is what makes a
# naive `in` test unsafe.
_FILTERS = """ TS allpass           A->A       Apply a two-pole all-pass filter.
 TS bandpass          A->A       Apply a two-pole Butterworth band-pass filter.
 TS bass              A->A       Boost or cut lower frequencies.
 TS highpass          A->A       Apply a high-pass filter with 3dB point frequency.
 TS lowpass           A->A       Apply a low-pass filter with 3dB point frequency.
 .. ass               V->V       Render ASS subtitles onto input video using the libass library.
 .. subtitles         V->V       Render text subtitles onto input video using the libass library.
 .. loudnorm          A->A       EBU R128 loudness normalization
 .. silencedetect     A->A       Detect silence.
"""

# The same listing with the libass filter removed - i.e. an essentials build.
_FILTERS_WITHOUT_LIBASS = "\n".join(
    line for line in _FILTERS.splitlines() if not line.startswith(" .. ass ")
)


@pytest.mark.unit
def test_capability_match_requires_the_name_column() -> None:
    assert has_capability(_FILTERS, "ass")
    assert has_capability(_FILTERS, "loudnorm")
    assert has_capability(_FILTERS, "silencedetect")


@pytest.mark.unit
def test_capability_match_is_not_fooled_by_substrings() -> None:
    """Regression: `"ass" in listing` matches bass/lowpass/allpass and lies."""
    assert "ass" in _FILTERS_WITHOUT_LIBASS  # the naive check would pass here
    assert not has_capability(_FILTERS_WITHOUT_LIBASS, "ass")  # the real one does not


@pytest.mark.unit
@pytest.mark.parametrize("absent", ["nvenc", "libx26", "silence", "loud", "subtitle"])
def test_partial_names_do_not_match(absent: str) -> None:
    assert not has_capability(_FILTERS, absent)
