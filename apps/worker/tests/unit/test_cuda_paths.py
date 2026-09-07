"""Coverage for CUDA DLL discovery.

The bug this guards against cost a debugging session in Phase 0: CTranslate2
loads cuBLAS/cuDNN by bare filename, and the pip-installed DLLs sit somewhere
Windows never looks. See clipforge/models/cuda.py.
"""

from __future__ import annotations

import sys

import pytest
from clipforge.models import cuda


@pytest.mark.unit
def test_discovery_returns_only_real_directories_holding_dlls() -> None:
    for directory in cuda.find_cuda_dll_directories():
        assert directory.is_dir()
        assert directory.name == "bin"
        assert any(directory.glob("*.dll"))


@pytest.mark.unit
def test_registration_is_idempotent() -> None:
    """Repeat calls must not re-register, or PATH grows without bound."""
    cuda.register_cuda_dll_directories()
    assert cuda.register_cuda_dll_directories() == []


@pytest.mark.unit
def test_registration_never_raises_without_the_gpu_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CI installs no nvidia packages; discovery must degrade to a no-op."""
    monkeypatch.setattr(cuda, "_nvidia_package_roots", list)
    monkeypatch.setattr(cuda, "_registered", set())
    assert cuda.register_cuda_dll_directories() == []


@pytest.mark.unit
@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows no-op path")
def test_noop_off_windows() -> None:
    assert cuda.register_cuda_dll_directories() == []
