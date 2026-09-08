"""The ModelBroker: exclusive ownership of GPU residency.

The binding constraint on this whole architecture is 6144 MiB of VRAM, of which
roughly 5.4 GB is usable. Whisper (~1.6 GB) and a 4B LLM (~3.4 GB) cannot be
co-resident with any headroom worth having, so **at most one model class is
resident at a time** and every GPU stage must acquire this broker's lease first.

That is what makes the scheduler's GPU lane depth 1 and its CPU lane depth N:
a download or a render proceeds happily while another job transcribes.
See docs/PLAN.md §2.1.

Two things here are less obvious than they look.

**Release is verified, not assumed.** There is no PyTorch allocator to ask
whether memory actually came back — CTranslate2 frees on refcount drop, and
Ollama frees on its own schedule. So the broker re-probes after unloading and
reports the shortfall if the memory did not return.

**A foreign process can hold the memory we need.** Observed in Phase 0: an
unrelated interactive Ollama session held 4.8 GB, leaving 1.3 GB free. The broker
therefore measures *actual free* VRAM rather than tracking its own allocations,
and on a shortfall names the process responsible instead of letting the stage die
with an opaque CUDA OOM.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import structlog

from clipforge.models.vram import GpuProcess, VramProbe, VramSnapshot, probe_vram

log = structlog.get_logger(__name__)

__all__ = ["InsufficientVramError", "ModelBroker", "ModelLease"]


class InsufficientVramError(RuntimeError):
    """Not enough free VRAM to load a model, with an actionable explanation.

    The message is the point. "CUDA out of memory" tells an operator nothing
    they can act on; "needs 3400 MiB, 1300 MiB free, ollama.exe (pid 8412) is
    holding 4800 MiB" tells them exactly what to close.
    """

    def __init__(
        self,
        *,
        model: str,
        required_mb: int,
        available_mb: int,
        holder: GpuProcess | None,
        snapshot: VramSnapshot | None,
    ) -> None:
        self.model = model
        self.required_mb = required_mb
        self.available_mb = available_mb
        self.holder = holder
        self.snapshot = snapshot

        message = (
            f"cannot load {model}: needs {required_mb} MiB, "
            f"{available_mb} MiB available within budget"
        )
        if holder is not None:
            message += f". Largest other consumer: {holder.describe()}"
        elif snapshot is not None:
            message += (
                f". Device {snapshot.device_name}: "
                f"{snapshot.free_mb} MiB free of {snapshot.total_mb} MiB"
            )
        super().__init__(message)


@dataclass
class ModelLease:
    """An exclusive hold on GPU residency for one model."""

    model: str
    required_mb: int
    acquired_at: float
    peak_vram_mb: int = 0
    _broker: ModelBroker | None = field(default=None, repr=False)

    def observe(self) -> int:
        """Sample current usage, tracking the peak for the job document.

        Per-stage peak VRAM is written back to the job so the model ladder in
        docs/PLAN.md §2.1 can be revised against measurements rather than
        estimates.
        """
        if self._broker is None:
            return self.peak_vram_mb
        snapshot = self._broker.snapshot()
        if snapshot is not None:
            self.peak_vram_mb = max(self.peak_vram_mb, snapshot.used_mb)
        return self.peak_vram_mb


class ModelBroker:
    """Serialises GPU residency across the worker.

    Thread-safe and re-entrant-hostile on purpose: a stage that tries to acquire
    the broker while already holding it has a design error, and blocking forever
    would hide it. The lock is not reentrant, so that deadlocks immediately and
    visibly in tests rather than intermittently in production.
    """

    def __init__(
        self,
        *,
        reserve_mb: int = 700,
        probe: VramProbe = probe_vram,
        wait_timeout_s: float = 0.0,
        poll_interval_s: float = 2.0,
    ) -> None:
        self._reserve_mb = reserve_mb
        self._probe = probe
        self._wait_timeout_s = wait_timeout_s
        self._poll_interval_s = poll_interval_s
        self._lock = threading.Lock()
        self._current: ModelLease | None = None

    # ── Introspection ────────────────────────────────────────────────────────

    def snapshot(self) -> VramSnapshot | None:
        return self._probe()

    @property
    def resident(self) -> str | None:
        """Which model currently holds the GPU, if any."""
        lease = self._current
        return lease.model if lease else None

    def available_mb(self) -> int | None:
        """Budget after the desktop reserve. ``None`` when there is no GPU."""
        snapshot = self._probe()
        if snapshot is None:
            return None
        return snapshot.budget_mb(self._reserve_mb)

    def fits(self, required_mb: int) -> bool:
        available = self.available_mb()
        if available is None:
            # No GPU to measure. Refusing here would make every CPU-only machine
            # unable to run, and the stage itself will fail clearly if it truly
            # needed a device.
            return True
        return required_mb <= available

    # ── Leasing ──────────────────────────────────────────────────────────────

    @contextmanager
    def acquire(self, model: str, required_mb: int) -> Iterator[ModelLease]:
        """Hold the GPU for one model, for the duration of the block.

        Blocks until no other model is resident, then checks the budget. If the
        budget check fails the lock is released before raising, so a foreign
        process holding memory cannot wedge the whole worker.
        """
        self._lock.acquire()
        try:
            self._ensure_capacity(model, required_mb)
            lease = ModelLease(
                model=model,
                required_mb=required_mb,
                acquired_at=time.monotonic(),
                _broker=self,
            )
            self._current = lease
            log.info(
                "model.acquired",
                model=model,
                required_mb=required_mb,
                available_mb=self.available_mb(),
            )
            try:
                yield lease
            finally:
                self._current = None
                self._verify_release(lease)
        finally:
            self._lock.release()

    def _ensure_capacity(self, model: str, required_mb: int) -> None:
        """Wait for capacity if configured to, then refuse if it never arrives."""
        deadline = time.monotonic() + self._wait_timeout_s

        while True:
            snapshot = self._probe()
            if snapshot is None:
                return  # No GPU to reason about; let the stage speak for itself.

            available = snapshot.budget_mb(self._reserve_mb)
            if required_mb <= available:
                return

            if time.monotonic() >= deadline:
                holder = snapshot.largest_foreign_consumer()
                log.warning(
                    "model.insufficient_vram",
                    model=model,
                    required_mb=required_mb,
                    available_mb=available,
                    free_mb=snapshot.free_mb,
                    total_mb=snapshot.total_mb,
                    holder=holder.describe() if holder else None,
                )
                raise InsufficientVramError(
                    model=model,
                    required_mb=required_mb,
                    available_mb=available,
                    holder=holder,
                    snapshot=snapshot,
                )

            log.info(
                "model.waiting_for_vram",
                model=model,
                required_mb=required_mb,
                available_mb=available,
            )
            time.sleep(self._poll_interval_s)

    def _verify_release(self, lease: ModelLease) -> None:
        """Check the memory actually came back.

        There is no allocator to interrogate, so this is the only way to notice a
        leak. It logs rather than raises: the stage has already finished, and
        failing a completed job because the *next* one might not fit would be
        the wrong trade. The next acquire's budget check is what enforces it.
        """
        snapshot = self._probe()
        if snapshot is None:
            return

        held = snapshot.budget_mb(self._reserve_mb)
        if held < lease.required_mb:
            log.warning(
                "model.release_unverified",
                model=lease.model,
                expected_free_mb=lease.required_mb,
                available_mb=held,
                hint="model may not have released VRAM; the next acquire will refuse",
            )
        else:
            log.info(
                "model.released",
                model=lease.model,
                held_for_s=round(time.monotonic() - lease.acquired_at, 2),
                peak_vram_mb=lease.peak_vram_mb or None,
            )
