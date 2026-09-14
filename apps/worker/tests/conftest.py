"""Shared pytest configuration.

Test tiers are declared as markers in ``pyproject.toml``. CI runs everything
except ``gpu``; see docs/PLAN.md §6.

**A tier is derived, not remembered.** Every test lives in a directory that
names its tier, so the tier is applied at collection time from the path, and an
explicit marker only ever overrides it — ``tests/integration/test_render.py``
marks two of its own ``gpu``, and that still works.

This exists because the decorator was previously the only thing that put a test
in a tier, and twelve of them never got one: the regressions for the broker's
nested-lease deadlock, for the attempt-budget reclaim, and for hiding a
broadcaster's mark on the RENDER path. All twelve passed. None of them ran in
CI, because ``-m unit`` deselected them, and a test that is deselected is
indistinguishable from a test that does not exist. The count in the CI log went
on looking healthy the whole time.

Deriving the tier closes the hole for tests already written. The
``pytest.UsageError`` below closes it for tests written next: a test that lands
somewhere no tier can be read from fails the run rather than disappearing from
it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

#: The test tiers, which are also the directory names directly under ``tests/``.
#: Kept in step with the ``markers`` list in ``pyproject.toml`` by
#: ``test_tiers.py``, so adding one there without adding it here is caught.
TIERS: tuple[str, ...] = ("unit", "integration", "gpu")


def tier_from_path(path: Path) -> str | None:
    """Return the tier the directory layout implies for ``path``, if any.

    The segment immediately below the *last* ``tests`` directory names the tier.
    Taking the last one matters: this repository lives under a path that may
    itself contain a ``tests`` component, and an earlier match would read a tier
    out of somebody's directory name.
    """
    parts = path.parts
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == "tests" and index + 1 < len(parts):
            candidate = parts[index + 1]
            return candidate if candidate in TIERS else None
    return None


def _untiered(items: Iterable[pytest.Item]) -> list[pytest.Item]:
    """Apply the derived tier where a test has none, and report what is left."""
    stranded: list[pytest.Item] = []
    for item in items:
        if any(item.get_closest_marker(tier) for tier in TIERS):
            continue
        tier = tier_from_path(Path(str(item.path)))
        if tier is None:
            stranded.append(item)
        else:
            item.add_marker(getattr(pytest.mark, tier))
    return stranded


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: Sequence[pytest.Item],
) -> None:
    """Give every collected test a tier, or refuse to run at all.

    Failing collection is deliberate and is the whole point. A test with no tier
    is not a test that runs slightly less often; it is one that CI never selects,
    and the previous cost of that was two regression suites for two real bugs
    sitting dormant while the suite reported itself green.
    """
    stranded = _untiered(items)
    if not stranded:
        return

    where = "\n".join(f"  {item.nodeid}" for item in stranded)
    raise pytest.UsageError(
        f"{len(stranded)} test(s) carry no tier marker and sit in no tier "
        f"directory, so no CI job would select them:\n{where}\n"
        f"Move each one under tests/{{{','.join(TIERS)}}}/, or mark it "
        f"explicitly with one of those markers."
    )
