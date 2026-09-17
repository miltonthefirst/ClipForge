"""Environment diagnostics.

These checks exist because two of the highest-impact risks in ``docs/PLAN.md``
are environmental rather than logical:

* a CPU-only or mismatched CUDA install silently making transcription ~20x
  slower instead of failing, and
* CTranslate2 failing to resolve the cuBLAS / cuDNN 9 DLLs on Windows, which
  surfaces as an opaque load error deep inside Phase 4.

``tools/doctor.ps1`` invokes this module so both failures happen in Phase 0, on
a five-second synthetic clip, rather than twenty minutes into a real job.

Run directly for machine-readable output::

    uv run python -m clipforge.diagnostics --json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import re
import struct
import subprocess
import sys
import tempfile
import wave
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clipforge.media.toolchain import resolve_toolchain
from clipforge.models.cuda import (
    find_cuda_dll_directories,
    register_cuda_dll_directories,
)

# VRAM headroom reserved for the desktop and browser. Mirrors
# CLIPFORGE_VRAM_RESERVE_MB in .env.example and docs/PLAN.md §2.1.
DEFAULT_VRAM_RESERVE_MB = 700


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single diagnostic check."""

    name: str
    ok: bool
    detail: str
    data: dict[str, Any] | None = None


def probe_gpu(reserve_mb: int = DEFAULT_VRAM_RESERVE_MB) -> CheckResult:
    """Report GPU name, total/free VRAM and the resulting model budget."""
    try:
        import pynvml
    except ImportError:  # pragma: no cover - dependency is declared in core
        return CheckResult("gpu", False, "nvidia-ml-py is not installed")

    try:
        pynvml.nvmlInit()
    except Exception as exc:  # noqa: BLE001 - any NVML failure means "no usable GPU"
        return CheckResult("gpu", False, f"NVML init failed: {exc}")

    try:
        if pynvml.nvmlDeviceGetCount() == 0:
            return CheckResult("gpu", False, "no NVIDIA device found")

        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        raw_name = pynvml.nvmlDeviceGetName(handle)
        name = raw_name.decode() if isinstance(raw_name, bytes) else raw_name
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)

        total_mb = mem.total // (1024 * 1024)
        free_mb = mem.free // (1024 * 1024)
        budget_mb = max(0, total_mb - reserve_mb)

        return CheckResult(
            name="gpu",
            ok=True,
            detail=f"{name} · {total_mb} MiB total · {free_mb} MiB free · {budget_mb} MiB budget",
            data={
                "name": name,
                "total_mb": total_mb,
                "free_mb": free_mb,
                "reserve_mb": reserve_mb,
                "model_budget_mb": budget_mb,
            },
        )
    finally:
        # A failed shutdown is not actionable and must not mask the report.
        with contextlib.suppress(Exception):
            pynvml.nvmlShutdown()


def _write_spoken_tone_wav(path: Path, seconds: float = 5.0, rate: int = 16000) -> None:
    """Write a short 16 kHz mono WAV.

    Content is irrelevant: this check verifies that CUDA kernels load and run,
    not that the transcription is accurate. Accuracy is asserted against a real
    fixture in the Phase 4 test suite.
    """
    frames = bytearray()
    for i in range(int(rate * seconds)):
        t = i / rate
        envelope = 0.5 * (1.0 - math.cos(2.0 * math.pi * min(t, 1.0)))
        sample = int(12000 * envelope * math.sin(2.0 * math.pi * 220.0 * t))
        frames += struct.pack("<h", sample)

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(bytes(frames))


def smoke_transcribe(device: str = "cuda", compute_type: str = "int8_float16") -> CheckResult:
    """Run a real transcription through CTranslate2 on the GPU.

    Uses the ``tiny`` model deliberately: it downloads in seconds and exercises
    exactly the same CUDA / cuDNN load path as ``large-v3-turbo``, which is the
    thing that actually breaks on Windows.
    """
    # MUST happen before faster_whisper/ctranslate2 is imported, or the bundled
    # cuBLAS and cuDNN DLLs will not resolve. See clipforge.models.cuda.
    register_cuda_dll_directories()

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return CheckResult(
            "cuda_transcribe",
            False,
            "faster-whisper not installed — run: uv sync --extra gpu",
        )

    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / "smoke.wav"
        _write_spoken_tone_wav(audio)

        try:
            model = WhisperModel("tiny", device=device, compute_type=compute_type)
        except Exception as exc:  # noqa: BLE001 - surface the raw loader error verbatim
            return CheckResult(
                "cuda_transcribe",
                False,
                f"model load failed on device={device!r}: {exc}",
            )

        try:
            segments, info = model.transcribe(str(audio), beam_size=1)
            count = sum(1 for _ in segments)
        except Exception as exc:  # noqa: BLE001
            return CheckResult("cuda_transcribe", False, f"transcription failed: {exc}")
        finally:
            del model

    return CheckResult(
        name="cuda_transcribe",
        ok=True,
        detail=(
            f"CTranslate2 ran on device={device!r} compute_type={compute_type!r} "
            f"({count} segment(s), detected language {info.language!r})"
        ),
        data={"device": device, "compute_type": compute_type, "segments": count},
    )


def probe_ollama(host: str = "http://127.0.0.1:11434", required: str = "qwen3.5:4b") -> CheckResult:
    """Confirm the Ollama daemon is reachable and the analysis model is pulled."""
    try:
        import httpx

        response = httpx.get(f"{host}/api/tags", timeout=5.0)
        response.raise_for_status()
        models = [m["name"] for m in response.json().get("models", [])]
    except Exception as exc:  # noqa: BLE001 - daemon down, refused, malformed: all the same
        return CheckResult("ollama", False, f"unreachable at {host}: {exc}")

    if required not in models:
        return CheckResult(
            "ollama",
            False,
            f"model {required!r} not pulled — run: ollama pull {required}",
            {"available": models},
        )

    return CheckResult(
        "ollama",
        True,
        f"reachable · {required} present · {len(models)} model(s) total",
        {"available": models},
    )


def has_capability(listing: str, name: str) -> bool:
    """Test whether `name` appears as a capability NAME in an ffmpeg listing.

    ``ffmpeg -filters`` and ``-encoders`` both print ``<flags> <name> <description>``,
    so the name must be matched as its own column. A naive substring test is
    badly wrong here: ``"ass" in listing`` is satisfied by ``bass``, ``lowpass``,
    ``allpass`` and fifteen other entries, which would report libass as present
    on a build that lacks it entirely — and burned-in captions (Phase 6) would
    then fail far from this check.
    """
    return re.search(rf"^\s*\S+\s+{re.escape(name)}\s", listing, re.MULTILINE) is not None


def probe_ffmpeg(binary: str = "ffmpeg") -> CheckResult:
    """Confirm ffmpeg exists and exposes the encoders and filters we depend on."""
    try:
        encoders = subprocess.run(  # noqa: S603
            [binary, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
        filters = subprocess.run(  # noqa: S603
            [binary, "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult("ffmpeg", False, f"{binary} not runnable: {exc}")

    required = {
        "libx264": encoders,
        "ass": filters,
        "loudnorm": filters,
        "silencedetect": filters,
    }
    missing = [name for name, listing in required.items() if not has_capability(listing, name)]

    if missing:
        return CheckResult("ffmpeg", False, f"missing capabilities: {', '.join(missing)}")
    return CheckResult("ffmpeg", True, "libx264, ass, loudnorm, silencedetect present")


def probe_ffprobe(ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> CheckResult:
    """Confirm ffprobe exists too, and that it is a sibling of ffmpeg.

    A separate check from `probe_ffmpeg` because this file used to have only the
    one, and the gap was load-bearing: every MUSIC job failed with *"ffprobe and
    ffmpeg not found"* on a machine whose doctor reported ffmpeg present, which
    made the message read like nonsense. Two programs are two questions.

    The sibling half matters for a different reason. Nothing that shells out
    cares where these live — `subprocess` resolves both against PATH
    independently. yt-dlp does care, because it is told *one* location and finds
    the other beside it, so a split install is a real configuration worth naming
    before it produces a confusing failure rather than after.
    """
    try:
        version = subprocess.run(  # noqa: S603
            [ffprobe, "-hide_banner", "-version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult(
            "ffprobe",
            False,
            f"{ffprobe} not runnable: {exc}. Durations, dimensions and the beat grid all "
            "read through it, so ingest and music both stop without it.",
        )

    tools = resolve_toolchain(ffmpeg, ffprobe)
    if tools.ffmpeg is not None and tools.ffprobe is not None:
        if tools.ffmpeg.parent != tools.ffprobe.parent:
            return CheckResult(
                "ffprobe",
                True,
                f"present, but installed apart from ffmpeg ({tools.ffprobe.parent}). Everything "
                "still works; yt-dlp is simply left to find both on PATH.",
                {"ffmpeg": str(tools.ffmpeg), "ffprobe": str(tools.ffprobe)},
            )
        return CheckResult(
            "ffprobe",
            True,
            f"{version.splitlines()[0][:60]}, beside ffmpeg",
            {"directory": str(tools.ffprobe.parent)},
        )

    return CheckResult("ffprobe", True, version.splitlines()[0][:60])


def probe_nvenc(binary: str = "ffmpeg") -> CheckResult:
    """Actually encode a frame with NVENC, rather than trusting the listing.

    `ffmpeg -encoders` lists `h264_nvenc` on any build compiled with it, whether
    or not the installed driver can run it. On this project's own reference
    machine that gap is real: the encoder is listed, and every attempt to use it
    fails with "Driver does not support the required nvenc API version".

    This is the same class of problem as the cuBLAS DLL in Phase 0 — "present"
    and "usable" are different questions — so it gets the same treatment: run the
    real thing for a fraction of a second and see.

    Not fatal. The render stage falls back to libx264 automatically, which is
    slower but correct, so this reports a warning-shaped pass rather than
    blocking the whole environment.
    """
    try:
        completed = subprocess.run(  # noqa: S603
            [
                binary,
                "-hide_banner",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=256x256:rate=10:duration=0.2",
                "-c:v",
                "h264_nvenc",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult("nvenc", True, f"could not be tested ({exc}); renders will use libx264")

    if completed.returncode == 0:
        return CheckResult("nvenc", True, "h264_nvenc encodes successfully")

    reason = next(
        (
            line.strip()
            for line in completed.stderr.splitlines()
            if "driver" in line.lower() or "nvenc" in line.lower()
        ),
        completed.stderr.strip().splitlines()[0] if completed.stderr.strip() else "unknown",
    )
    return CheckResult(
        "nvenc",
        True,
        f"UNUSABLE, renders fall back to libx264 — {reason[:120]}",
        data={
            "usable": False,
            "fix": "update the NVIDIA driver, or set CLIPFORGE_VIDEO_ENCODER=libx264",
        },
    )


def probe_cuda_libraries() -> CheckResult:
    """Confirm the bundled cuBLAS / cuDNN DLLs were found and registered."""
    directories = find_cuda_dll_directories()
    if not directories:
        return CheckResult(
            "cuda_libs",
            False,
            "no nvidia/*/bin directories found — run: uv sync --extra gpu",
        )

    register_cuda_dll_directories()
    names = ", ".join(d.parent.name for d in directories)
    return CheckResult(
        "cuda_libs",
        True,
        f"registered {len(directories)} DLL director(ies): {names}",
        {"directories": [str(d) for d in directories]},
    )


def probe_publishing(settings: Any | None = None) -> CheckResult:
    """Report whether publishing is configured, without touching the network.

    Publishing being *off* is a pass, not a failure. Off is the default and the
    intended state for most machines; reporting it as a fault would train the
    reader to ignore a red line, which is the one thing a diagnostic must never
    do. What this catches is the genuinely broken middle: publishing switched on
    with no client secrets or no authorisation, which would otherwise surface as
    a failed job hours after someone approved a clip and expected it to go out.
    """
    from clipforge.config import get_settings
    from clipforge.publish.credentials import TokenStore

    # Injectable so a test can describe a configuration rather than arrange one
    # in the environment. `doctor` passes nothing and gets the real settings.
    settings = settings or get_settings()
    if not settings.publishing_enabled:
        return CheckResult(
            "publishing",
            True,
            "disabled (CLIPFORGE_PUBLISHING_ENABLED=false) — nothing will be uploaded",
            {"enabled": False},
        )

    secrets = Path(settings.youtube_client_secrets)
    if not secrets.is_file():
        return CheckResult(
            "publishing",
            False,
            f"enabled, but no OAuth client at {secrets}. Create one in the Google Cloud "
            "console and point CLIPFORGE_YOUTUBE_CLIENT_SECRETS at it.",
            {"enabled": True, "clientSecrets": False},
        )

    store = TokenStore(settings.youtube_token_store)
    if not store.exists():
        return CheckResult(
            "publishing",
            False,
            "enabled, but not authorised. Run: clipforge-worker youtube-auth",
            {"enabled": True, "clientSecrets": True, "authorised": False},
        )

    try:
        tokens = store.load()
    except Exception as exc:  # noqa: BLE001 - any failure here means "re-authorise"
        return CheckResult(
            "publishing",
            False,
            f"enabled, but the stored token will not open: {exc}",
            {"enabled": True, "authorised": False},
        )

    # Deliberately reports the *age* rather than the token. Refresh tokens expire
    # after 7 days while the consent screen is in Testing mode, so age is the
    # number that predicts the next failure.
    age = ""
    if tokens.obtained_at is not None:
        days = (datetime.now(UTC) - tokens.obtained_at).days
        age = f", authorised {days}d ago"
        if days >= 7:
            return CheckResult(
                "publishing",
                False,
                f"enabled, but the token is {days} days old. While the OAuth consent screen "
                "is in Testing mode Google expires refresh tokens after 7 days — "
                "re-run: clipforge-worker youtube-auth",
                {"enabled": True, "authorised": True, "ageDays": days},
            )

    return CheckResult(
        "publishing",
        True,
        f"enabled, authorised, default privacy {settings.youtube_default_privacy}{age}",
        {"enabled": True, "authorised": True, **tokens.redacted()},
    )


def run_all(*, include_gpu: bool = True) -> list[CheckResult]:
    """Run every diagnostic and return the results in report order."""
    results = [
        probe_ffmpeg(),
        probe_ffprobe(),
        probe_nvenc(),
        probe_ollama(),
        probe_gpu(),
        probe_publishing(),
    ]
    if include_gpu:
        results.append(probe_cuda_libraries())
        results.append(smoke_transcribe())
    return results


def main(argv: list[str] | None = None) -> int:
    """Entrypoint. Returns a process exit code: 0 if every check passed."""
    parser = argparse.ArgumentParser(description="ClipForge worker diagnostics")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--skip-gpu", action="store_true", help="skip the CUDA transcription check")
    args = parser.parse_args(argv)

    results = run_all(include_gpu=not args.skip_gpu)

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        for result in results:
            print(f"[{'PASS' if result.ok else 'FAIL'}] {result.name}: {result.detail}")

    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
