"""VRAM accounting through NVML.

There is no PyTorch allocator to interrogate — faster-whisper runs on CTranslate2
and the LLM runs out-of-process in Ollama — so VRAM is measured directly from the
driver. See docs/adr/0002-ctranslate2-without-pytorch.md.

Measuring *actual free* memory rather than tracking our own allocations is not a
refinement, it is the requirement. Phase 0 found an unrelated interactive Ollama
session holding 4.8 GB of the 6 GB card, leaving 1.3 GB free. A broker that
tracked only its own loads would have believed the card was empty and handed the
stage an opaque CUDA OOM. This module therefore also reports *who* is holding the
memory, so the failure message can name the culprit.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from typing import Protocol

BYTES_PER_MB = 1024 * 1024


@dataclass(frozen=True)
class GpuProcess:
    """A process holding VRAM on the device."""

    pid: int
    name: str
    used_mb: int

    def describe(self) -> str:
        return f"{self.name} (pid {self.pid}, {self.used_mb} MiB)"


@dataclass(frozen=True)
class VramSnapshot:
    """A point-in-time reading of the device."""

    device_name: str
    total_mb: int
    free_mb: int
    used_mb: int
    processes: tuple[GpuProcess, ...] = ()

    def budget_mb(self, reserve_mb: int) -> int:
        """Free memory minus the headroom reserved for the desktop and browser.

        Clamped at zero: a negative budget is not a meaningful number, and
        letting it go negative makes every downstream comparison read backwards.
        """
        return max(0, self.free_mb - reserve_mb)

    def foreign_consumers(self, own_pid: int | None = None) -> tuple[GpuProcess, ...]:
        """Processes holding VRAM that are not this worker."""
        pid = own_pid if own_pid is not None else os.getpid()
        return tuple(p for p in self.processes if p.pid != pid)

    def largest_foreign_consumer(self, own_pid: int | None = None) -> GpuProcess | None:
        consumers = self.foreign_consumers(own_pid)
        if not consumers:
            return None
        return max(consumers, key=lambda p: p.used_mb)


class VramProbe(Protocol):
    """A source of VRAM readings.

    The broker takes one of these rather than calling NVML directly, so its
    behaviour under a constrained card — the case that actually matters and the
    one that is impossible to arrange on demand — is an ordinary unit test.
    """

    def __call__(self) -> VramSnapshot | None: ...


def probe_vram() -> VramSnapshot | None:
    """Read the first NVIDIA device, or ``None`` if there is nothing to read.

    Returns ``None`` rather than raising on every failure path — no driver, no
    device, NVML not installed. A machine without a GPU is a supported
    configuration (CI runs on one), not an error.
    """
    try:
        import pynvml
    except ImportError:
        return None

    try:
        pynvml.nvmlInit()
    except Exception:  # noqa: BLE001 - any NVML failure means "no usable GPU"
        return None

    try:
        if pynvml.nvmlDeviceGetCount() == 0:
            return None

        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        raw_name = pynvml.nvmlDeviceGetName(handle)
        device_name = raw_name.decode() if isinstance(raw_name, bytes) else str(raw_name)
        memory = pynvml.nvmlDeviceGetMemoryInfo(handle)

        return VramSnapshot(
            device_name=device_name,
            total_mb=memory.total // BYTES_PER_MB,
            free_mb=memory.free // BYTES_PER_MB,
            used_mb=memory.used // BYTES_PER_MB,
            processes=_running_processes(pynvml, handle),
        )
    except Exception:  # noqa: BLE001 - degrade to "unknown", never take down a job
        return None
    finally:
        # Nothing useful to do if shutdown fails, and raising here would mask
        # whatever the caller was actually asking about.
        with contextlib.suppress(Exception):
            pynvml.nvmlShutdown()


def _running_processes(pynvml: object, handle: object) -> tuple[GpuProcess, ...]:
    """Who is holding memory on the device.

    Best-effort by design. Process enumeration is the first thing to fail on a
    laptop GPU under WDDM, and losing the *names* must not cost us the *numbers* —
    the totals are what the budget check depends on.
    """
    processes: list[GpuProcess] = []
    try:
        infos = pynvml.nvmlDeviceGetComputeRunningProcesses(handle)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return ()

    for info in infos:
        used = getattr(info, "usedGpuMemory", None)
        try:
            raw = pynvml.nvmlSystemGetProcessName(info.pid)  # type: ignore[attr-defined]
            name = raw.decode() if isinstance(raw, bytes) else str(raw)
        except Exception:  # noqa: BLE001
            name = "unknown"
        processes.append(
            GpuProcess(
                pid=int(info.pid),
                name=name,
                # usedGpuMemory is None when the driver will not report per-process
                # usage, which is common under WDDM on Windows.
                used_mb=int(used) // BYTES_PER_MB if used else 0,
            )
        )

    return tuple(processes)
