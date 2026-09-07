"""Real-hardware smoke tests. Opt-in: ``uv run pytest -m gpu``.

These are the canonical implementation of the Phase 0 exit criteria that CI
cannot check, because GitHub runners have no NVIDIA GPU.
"""

from __future__ import annotations

import pytest
from clipforge.diagnostics import probe_ffmpeg, probe_gpu, probe_ollama, smoke_transcribe


@pytest.mark.gpu
def test_gpu_is_visible_and_has_budget() -> None:
    result = probe_gpu()
    assert result.ok, result.detail
    assert result.data is not None
    # docs/PLAN.md §2.1 sizes every model against this budget.
    assert result.data["model_budget_mb"] >= 4096, "insufficient VRAM budget for the 4B analyst"


@pytest.mark.gpu
def test_ctranslate2_runs_on_cuda() -> None:
    """The Phase 4 sharp edge: cuBLAS/cuDNN DLL resolution on Windows."""
    result = smoke_transcribe(device="cuda")
    assert result.ok, result.detail


@pytest.mark.gpu
def test_ffmpeg_has_required_capabilities() -> None:
    result = probe_ffmpeg()
    assert result.ok, result.detail


@pytest.mark.gpu
def test_ollama_has_the_analysis_model() -> None:
    result = probe_ollama()
    assert result.ok, result.detail
