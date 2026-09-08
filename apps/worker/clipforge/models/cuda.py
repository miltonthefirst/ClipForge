"""CUDA shared-library discovery for Windows.

CTranslate2 (which powers faster-whisper) loads cuBLAS and cuDNN dynamically by
bare filename — ``cublas64_12.dll``, ``cudnn64_9.dll`` and friends. The pip
packages ``nvidia-cublas-cu12`` and ``nvidia-cudnn-cu12`` do ship those DLLs,
but they land in ``site-packages/nvidia/<component>/bin``, which is on no search
path Windows consults. The result is a load failure at first inference:

    Library cublas64_12.dll is not found or cannot be loaded

This module registers those directories with the loader so the DLLs resolve.
It must run **before** ``ctranslate2`` or ``faster_whisper`` is imported.

The usual advice is "add them to your PATH by hand", which is exactly the kind of
undocumented machine-specific setup step that makes a project unreproducible.
Doing it here means a clean ``uv sync --extra gpu`` is genuinely sufficient.

On non-Windows platforms this is a no-op: the manylinux wheels declare their
dependencies through RPATH and resolve without help.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# Directories already handed to the loader, so repeated calls stay cheap and do
# not grow PATH without bound.
_registered: set[Path] = set()


def _nvidia_package_roots() -> list[Path]:
    """Locate the ``nvidia`` namespace package inside the active environment."""
    try:
        spec = importlib.util.find_spec("nvidia")
    except (ImportError, ValueError):
        # A partially-installed or shadowed `nvidia` package must degrade to
        # "no CUDA libraries found", never take down the diagnostics run.
        return []
    if spec is None or not spec.submodule_search_locations:
        return []
    return [Path(location) for location in spec.submodule_search_locations]


def find_cuda_dll_directories() -> list[Path]:
    """Return every ``nvidia/*/bin`` directory that actually contains a DLL."""
    directories: list[Path] = []
    for root in _nvidia_package_roots():
        if not root.is_dir():
            continue
        for component in sorted(root.iterdir()):
            candidate = component / "bin"
            if candidate.is_dir() and any(candidate.glob("*.dll")):
                directories.append(candidate)
    return directories


def register_cuda_dll_directories() -> list[Path]:
    """Make the bundled CUDA DLLs loadable. Returns the directories registered.

    Idempotent, and safe to call when the GPU extra is not installed — it simply
    finds nothing and returns an empty list.
    """
    if sys.platform != "win32":
        return []

    newly_registered: list[Path] = []
    for directory in find_cuda_dll_directories():
        if directory in _registered:
            continue

        # The authoritative mechanism for the CPython DLL loader.
        os.add_dll_directory(str(directory))

        # Belt and braces: CTranslate2 resolves some libraries through the
        # process PATH rather than the added-directory list.
        path = os.environ.get("PATH", "")
        if str(directory) not in path:
            os.environ["PATH"] = f"{directory}{os.pathsep}{path}"

        _registered.add(directory)
        newly_registered.append(directory)

    return newly_registered
