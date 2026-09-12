"""The ModelBroker and VRAM accounting.

Phase 2, exit criteria 4 and 5. Both are about refusing to start rather than
failing halfway, and both are testable here precisely because the broker takes an
injected probe: "a card with 1.3 GB free because something else is holding 4.8"
is the case that actually matters and the one that cannot be arranged on demand.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest
from clipforge.models.broker import InsufficientVramError, ModelBroker
from clipforge.models.vram import GpuProcess, VramSnapshot

RTX_3050_TOTAL = 6144
RESERVE = 700


def snapshot(free_mb: int, processes: tuple[GpuProcess, ...] = ()) -> VramSnapshot:
    return VramSnapshot(
        device_name="NVIDIA GeForce RTX 3050",
        total_mb=RTX_3050_TOTAL,
        free_mb=free_mb,
        used_mb=RTX_3050_TOTAL - free_mb,
        processes=processes,
    )


def probe_returning(value: VramSnapshot | None) -> Callable[[], VramSnapshot | None]:
    return lambda: value


# ─────────────────────────────────────────────────────────────────────────────
# The budget arithmetic
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_budget_subtracts_the_desktop_reserve() -> None:
    assert snapshot(free_mb=5000).budget_mb(RESERVE) == 4300


@pytest.mark.unit
def test_budget_never_goes_negative() -> None:
    """A negative budget makes every downstream comparison read backwards."""
    assert snapshot(free_mb=100).budget_mb(RESERVE) == 0


@pytest.mark.unit
def test_foreign_consumers_exclude_this_process() -> None:
    ours = GpuProcess(pid=1234, name="python.exe", used_mb=1600)
    theirs = GpuProcess(pid=8412, name="ollama.exe", used_mb=4800)

    snap = snapshot(free_mb=1300, processes=(ours, theirs))

    assert snap.foreign_consumers(own_pid=1234) == (theirs,)
    assert snap.largest_foreign_consumer(own_pid=1234) == theirs


@pytest.mark.unit
def test_largest_foreign_consumer_is_none_when_we_are_alone() -> None:
    ours = GpuProcess(pid=1234, name="python.exe", used_mb=1600)
    assert snapshot(free_mb=4000, processes=(ours,)).largest_foreign_consumer(own_pid=1234) is None


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 4 — refuse what does not fit
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_the_broker_refuses_a_model_larger_than_the_card() -> None:
    """Phase 2, exit criterion 4: a synthetic 8 GB request on a 6 GB card."""
    broker = ModelBroker(reserve_mb=RESERVE, probe=probe_returning(snapshot(free_mb=5800)))

    with pytest.raises(InsufficientVramError) as caught, broker.acquire("synthetic-8gb", 8192):
        pytest.fail("should not have acquired")

    assert caught.value.required_mb == 8192
    assert caught.value.available_mb == 5100
    assert "8192 MiB" in str(caught.value)


@pytest.mark.unit
def test_a_model_that_fits_exactly_is_allowed() -> None:
    """Boundary: `<=`, not `<`. Refusing an exact fit would waste the last MiB
    of a card whose whole design constraint is that it has too few."""
    broker = ModelBroker(reserve_mb=RESERVE, probe=probe_returning(snapshot(free_mb=5000)))
    with broker.acquire("exact", required_mb=4300) as leased:
        assert leased.model == "exact"


@pytest.mark.unit
def test_the_refusal_releases_the_lock() -> None:
    """A foreign process holding memory must not wedge the whole worker: the
    next stage to ask has to get a clean refusal, not block forever."""
    broker = ModelBroker(reserve_mb=RESERVE, probe=probe_returning(snapshot(free_mb=1000)))

    for _ in range(3):
        with pytest.raises(InsufficientVramError), broker.acquire("big", required_mb=4000):
            pass

    assert broker.resident is None


# ─────────────────────────────────────────────────────────────────────────────
# Exit criterion 5 — name the process holding the memory
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_a_shortfall_names_the_process_holding_the_memory() -> None:
    """Phase 2, exit criterion 5, and the exact situation Phase 0 hit: an
    unrelated Ollama session holding 4.8 GB of the 6 GB card."""
    ollama = GpuProcess(pid=8412, name="ollama.exe", used_mb=4800)
    broker = ModelBroker(
        reserve_mb=RESERVE,
        probe=probe_returning(snapshot(free_mb=1300, processes=(ollama,))),
    )

    with pytest.raises(InsufficientVramError) as caught, broker.acquire("whisper-turbo", 1600):
        pytest.fail("should not have acquired")

    message = str(caught.value)
    assert "ollama.exe" in message
    assert "8412" in message
    assert "4800 MiB" in message
    assert caught.value.holder is not None
    assert caught.value.holder.name == "ollama.exe"


@pytest.mark.unit
def test_a_shortfall_with_no_named_process_still_reports_the_numbers() -> None:
    """Per-process reporting is the first thing to fail under WDDM on Windows.
    Losing the names must not cost us a usable message."""
    broker = ModelBroker(reserve_mb=RESERVE, probe=probe_returning(snapshot(free_mb=900)))

    with pytest.raises(InsufficientVramError) as caught, broker.acquire("whisper", 1600):
        pytest.fail("should not have acquired")

    message = str(caught.value)
    assert caught.value.holder is None
    assert "RTX 3050" in message
    assert "900 MiB free" in message


# ─────────────────────────────────────────────────────────────────────────────
# Exclusivity — the depth-1 GPU lane
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_only_one_model_may_be_resident_at_a_time() -> None:
    """This lock IS the GPU lane. If two stages could hold it, the 6 GB budget
    in docs/PLAN.md §2.1 would be meaningless."""
    broker = ModelBroker(reserve_mb=RESERVE, probe=probe_returning(snapshot(free_mb=5800)))
    order: list[str] = []
    first_acquired = threading.Event()
    release_first = threading.Event()

    def hold_first() -> None:
        with broker.acquire("whisper", required_mb=1600):
            order.append("first-in")
            first_acquired.set()
            release_first.wait(timeout=5)
            order.append("first-out")

    def take_second() -> None:
        first_acquired.wait(timeout=5)
        with broker.acquire("llm", required_mb=3400):
            order.append("second-in")

    threads = [threading.Thread(target=hold_first), threading.Thread(target=take_second)]
    for thread in threads:
        thread.start()

    first_acquired.wait(timeout=5)
    time.sleep(0.05)  # give the second thread a chance to wrongly proceed
    assert order == ["first-in"], "the second model got in while the first was resident"

    release_first.set()
    for thread in threads:
        thread.join(timeout=5)

    assert order == ["first-in", "first-out", "second-in"]


@pytest.mark.unit
def test_the_lease_is_cleared_after_the_block() -> None:
    broker = ModelBroker(reserve_mb=RESERVE, probe=probe_returning(snapshot(free_mb=5800)))
    with broker.acquire("whisper", required_mb=1600):
        assert broker.resident == "whisper"
    assert broker.resident is None


# ─────────────────────────────────────────────────────────────────────────────
# No GPU at all — the CI configuration
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_a_machine_with_no_gpu_is_not_refused() -> None:
    """CI runs on one. Refusing here would make the whole harness unrunnable, and
    a stage that genuinely needed a device will fail clearly on its own."""
    broker = ModelBroker(reserve_mb=RESERVE, probe=probe_returning(None))

    with broker.acquire("whisper", required_mb=99999) as leased:
        assert leased.model == "whisper"

    assert broker.available_mb() is None
    assert broker.fits(99999) is True


# ─────────────────────────────────────────────────────────────────────────────
# Waiting, and release verification
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_the_broker_waits_for_capacity_when_configured_to() -> None:
    """A transient consumer should be waited out rather than failing the job."""
    readings = iter([snapshot(free_mb=1000), snapshot(free_mb=1000), snapshot(free_mb=5800)])
    last = snapshot(free_mb=5800)

    def probe() -> VramSnapshot:
        nonlocal last
        last = next(readings, last)
        return last

    broker = ModelBroker(reserve_mb=RESERVE, probe=probe, wait_timeout_s=5.0, poll_interval_s=0.01)

    with broker.acquire("whisper", required_mb=1600) as leased:
        assert leased.model == "whisper"


@pytest.mark.unit
def test_waiting_still_gives_up_eventually() -> None:
    broker = ModelBroker(
        reserve_mb=RESERVE,
        probe=probe_returning(snapshot(free_mb=800)),
        wait_timeout_s=0.05,
        poll_interval_s=0.01,
    )

    with pytest.raises(InsufficientVramError), broker.acquire("whisper", required_mb=1600):
        pytest.fail("should not have acquired")


@pytest.mark.unit
def test_peak_vram_is_observed_through_the_lease() -> None:
    """Per-stage peak VRAM is written back to the job document so the model
    ladder can be revised against measurements rather than estimates."""
    broker = ModelBroker(reserve_mb=RESERVE, probe=probe_returning(snapshot(free_mb=4000)))

    with broker.acquire("whisper", required_mb=1600) as leased:
        assert leased.observe() == RTX_3050_TOTAL - 4000
