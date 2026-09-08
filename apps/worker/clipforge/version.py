"""Single source of truth for the worker version.

The worker publishes this in its heartbeat document so the PWA can show which
build produced a given job, and so a stale worker is identifiable in the field.
"""

from __future__ import annotations

from typing import Final

__version__: Final[str] = "0.0.1"


def version_tuple() -> tuple[int, int, int]:
    """Return ``__version__`` as a ``(major, minor, patch)`` tuple."""
    major, minor, patch = (int(part) for part in __version__.split("."))
    return major, minor, patch
