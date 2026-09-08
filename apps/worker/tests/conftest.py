"""Shared pytest configuration.

Test tiers are declared as markers in ``pyproject.toml``. CI runs everything
except ``gpu``; see docs/PLAN.md §6.
"""

from __future__ import annotations
