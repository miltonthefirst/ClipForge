"""Unit coverage for the parts of diagnostics that need no hardware."""

from __future__ import annotations

import json
import shutil
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from clipforge import diagnostics
from clipforge.config import Settings
from clipforge.diagnostics import (
    CheckResult,
    _write_spoken_tone_wav,
    has_capability,
    probe_ffmpeg,
    probe_ffprobe,
    run_all,
)
from clipforge.media.toolchain import Toolchain, resolve_toolchain


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
def test_missing_ffprobe_fails_without_raising() -> None:
    """A separate check from ffmpeg, because they are separate programs.

    The doctor reported ffmpeg present on a machine where every MUSIC job then
    died with "ffprobe and ffmpeg not found". One check for two programs is how
    a diagnostic ends up disagreeing with reality.
    """
    result = probe_ffprobe(ffprobe="clipforge-definitely-not-a-real-binary")

    assert result.ok is False
    assert "not runnable" in result.detail
    # Says what stops working, so the reader does not have to know.
    assert "beat grid" in result.detail


@pytest.mark.unit
def test_a_real_ffprobe_is_reported_with_where_it_lives() -> None:
    if shutil.which("ffprobe") is None:
        pytest.skip("needs ffprobe on PATH")

    result = probe_ffprobe()

    assert result.ok is True
    assert "ffprobe version" in result.detail


@pytest.mark.unit
def test_a_split_install_passes_but_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """Everything still works; it is a configuration worth naming, not a failure."""
    if shutil.which("ffprobe") is None:
        pytest.skip("needs ffprobe on PATH")

    real = resolve_toolchain("ffmpeg", "ffprobe")
    if real.ffprobe is None:
        pytest.skip("needs ffprobe on PATH")

    monkeypatch.setattr(
        diagnostics,
        "resolve_toolchain",
        lambda *_: Toolchain(
            ffmpeg=Path("/opt/ff/ffmpeg"),
            ffprobe=Path("/usr/bin/ffprobe"),
            requested_ffmpeg="ffmpeg",
            requested_ffprobe="ffprobe",
        ),
    )

    result = probe_ffprobe()

    assert result.ok is True
    assert "installed apart from ffmpeg" in result.detail


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
    probes = (
        "probe_ffmpeg",
        "probe_ffprobe",
        "probe_nvenc",
        "probe_ollama",
        "probe_gpu",
        "probe_publishing",
    )
    for probe in probes:
        monkeypatch.setattr(diagnostics, probe, lambda *_, **__: stub)

    names = [check.name for check in run_all(include_gpu=False)]
    assert "cuda_transcribe" not in names
    assert len(names) == len(probes)


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


# ─────────────────────────────────────────────────────────────────────────────
# Phase 8: the publishing readiness check.
# ─────────────────────────────────────────────────────────────────────────────


def publishing_settings(**overrides: object) -> Settings:
    return Settings(use_emulators=True, **overrides)  # type: ignore[arg-type]


@pytest.mark.unit
def test_publishing_disabled_is_a_pass_not_a_failure() -> None:
    """Off is the default and the intended state for most machines.

    Reporting it red would train the reader to ignore a red line, which is the
    one thing a diagnostic must never do.
    """
    result = diagnostics.probe_publishing(publishing_settings(publishing_enabled=False))
    assert result.ok is True
    assert "disabled" in result.detail


@pytest.mark.unit
def test_publishing_enabled_without_client_secrets_fails_loudly(tmp_path: Path) -> None:
    """The broken middle: switched on, and it cannot possibly work. Left
    undetected this surfaces as a failed job hours after someone approved a
    clip and expected it to go out."""
    result = diagnostics.probe_publishing(
        publishing_settings(
            publishing_enabled=True,
            youtube_client_secrets=tmp_path / "absent.json",
            youtube_token_store=tmp_path / "token.enc",
        )
    )
    assert result.ok is False
    assert "CLIPFORGE_YOUTUBE_CLIENT_SECRETS" in result.detail


@pytest.mark.unit
def test_publishing_enabled_but_unauthorised_names_the_command(tmp_path: Path) -> None:
    secrets = tmp_path / "client.json"
    secrets.write_text('{"installed": {"client_id": "c", "client_secret": "s"}}')

    result = diagnostics.probe_publishing(
        publishing_settings(
            publishing_enabled=True,
            youtube_client_secrets=secrets,
            youtube_token_store=tmp_path / "token.enc",
        )
    )
    assert result.ok is False
    assert "youtube-auth" in result.detail


@pytest.mark.unit
def test_a_token_older_than_seven_days_is_reported_before_it_fails(tmp_path: Path) -> None:
    """The documented risk, surfaced by `doctor` rather than by a failed upload.

    Refresh tokens expire after 7 days while the consent screen is in Testing
    mode, so the token's age is the number that predicts the next failure.
    """
    from clipforge.publish.credentials import OAuthTokens, TokenStore

    secrets = tmp_path / "client.json"
    secrets.write_text('{"installed": {"client_id": "c", "client_secret": "s"}}')
    store = TokenStore(tmp_path / "token.enc")
    store.save(
        OAuthTokens(
            refresh_token="1//old",
            obtained_at=datetime.now(UTC) - timedelta(days=9),
        )
    )

    result = diagnostics.probe_publishing(
        publishing_settings(
            publishing_enabled=True,
            youtube_client_secrets=secrets,
            youtube_token_store=store.path,
        )
    )
    assert result.ok is False
    assert "7 days" in result.detail
    assert "youtube-auth" in result.detail


@pytest.mark.unit
def test_a_fresh_token_passes_and_never_reports_the_token(tmp_path: Path) -> None:
    from clipforge.publish.credentials import OAuthTokens, TokenStore

    secrets = tmp_path / "client.json"
    secrets.write_text('{"installed": {"client_id": "c", "client_secret": "s"}}')
    store = TokenStore(tmp_path / "token.enc")
    store.save(OAuthTokens(refresh_token="1//a-secret-value", obtained_at=datetime.now(UTC)))

    result = diagnostics.probe_publishing(
        publishing_settings(
            publishing_enabled=True,
            youtube_client_secrets=secrets,
            youtube_token_store=store.path,
        )
    )
    assert result.ok is True
    assert "unlisted" in result.detail
    # `doctor` output gets pasted into issues. It must never carry the token.
    assert "a-secret-value" not in json.dumps({"detail": result.detail, "data": result.data})
