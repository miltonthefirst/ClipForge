"""The worker version is published in the heartbeat, so it must stay parseable."""

from __future__ import annotations

import re

import pytest
from clipforge import __version__
from clipforge.version import version_tuple


@pytest.mark.unit
def test_version_is_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


@pytest.mark.unit
def test_version_tuple_matches_string() -> None:
    assert ".".join(str(part) for part in version_tuple()) == __version__
