"""The tier mechanism has to hold itself up.

These carry an explicit ``@pytest.mark.unit`` rather than relying on the
derivation in ``tests/conftest.py``. A test of a mechanism should not be
selected *by* that mechanism: if the derivation broke, tests depending on it to
be collected are the worst possible witnesses.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from tests.conftest import TIERS, tier_from_path

WORKER_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.unit
@pytest.mark.parametrize("tier", TIERS)
def test_a_directory_under_tests_names_its_tier(tier: str) -> None:
    assert tier_from_path(Path("apps/worker/tests") / tier / "test_x.py") == tier


@pytest.mark.unit
def test_a_directory_that_names_no_tier_yields_none() -> None:
    assert tier_from_path(Path("apps/worker/tests/helpers/test_x.py")) is None


@pytest.mark.unit
def test_a_path_with_no_tests_directory_yields_none() -> None:
    assert tier_from_path(Path("apps/worker/clipforge/stages/render.py")) is None


@pytest.mark.unit
def test_the_last_tests_directory_is_the_one_that_counts() -> None:
    """A checkout living under a path containing ``tests`` must not confuse it.

    The first ``tests`` here is somebody's folder name and the segment below it
    is ``worker``, which is not a tier. Reading the first match would return
    None and strand every test in the repository.
    """
    where = Path("/home/tests/worker/apps/worker/tests/unit/test_x.py")
    assert tier_from_path(where) == "unit"


@pytest.mark.unit
def test_every_tier_is_a_marker_pytest_knows_about() -> None:
    """``--strict-markers`` turns a tier missing from pyproject into an error.

    Deriving a marker that was never registered would fail the whole run rather
    than the one test, so the two lists are pinned together here instead.
    """
    pyproject = tomllib.loads((WORKER_ROOT / "pyproject.toml").read_text("utf-8"))
    declared = {
        marker.split(":", 1)[0].strip()
        for marker in pyproject["tool"]["pytest"]["ini_options"]["markers"]
    }
    assert set(TIERS) == declared


@pytest.mark.unit
def test_every_tier_has_a_directory_of_its_own() -> None:
    """The derivation reads directories, so a tier without one can never apply."""
    for tier in TIERS:
        assert (WORKER_ROOT / "tests" / tier).is_dir(), f"tests/{tier}/ is missing"
