"""The worker version is published in the heartbeat, so it must stay parseable."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType

import pytest
from clipforge import __version__
from clipforge.version import version_tuple


@pytest.mark.unit
def test_version_is_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


@pytest.mark.unit
def test_version_tuple_matches_string() -> None:
    assert ".".join(str(part) for part in version_tuple()) == __version__


# ── One version for the whole repository (tools/release.py) ──────────────────
#
# The phone shows the version stamped into the PWA from VERSION, the Worker
# page shows the one the agent and worker report from version.py. If they could
# drift, "which build is this?" would have two answers.

_REPO = Path(__file__).resolve().parents[4]


def _release_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release", _REPO / "tools" / "release.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_every_version_in_the_repository_agrees_with_version_file() -> None:
    version = (_REPO / "VERSION").read_text(encoding="utf-8").strip()
    assert __version__ == version
    pyproject = (_REPO / "apps/worker/pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE).group(1) == version  # type: ignore[union-attr]
    for path in (
        "apps/web/package.json",
        "apps/desktop/package.json",
        "apps/desktop/src-tauri/tauri.conf.json",
    ):
        assert json.loads((_REPO / path).read_text(encoding="utf-8"))["version"] == version, path


@pytest.mark.unit
@pytest.mark.parametrize(
    ("current", "major", "expected"),
    [
        ("0.0.4", False, "0.0.5"),
        ("0.0.9", False, "0.1.0"),
        ("0.9.9", False, "0.10.0"),
        ("0.4.2", True, "1.0.0"),
    ],
)
def test_the_release_rule(current: str, major: bool, expected: str) -> None:
    assert _release_module().next_version(current, major) == expected


@pytest.mark.unit
def test_a_release_rewrites_only_the_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = _release_module()
    for relative in ("VERSION", *release.VERSIONED_JSON, release.PYPROJECT, release.VERSION_PY):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((_REPO / relative).read_bytes())
    monkeypatch.setattr(release, "ROOT", str(tmp_path))
    monkeypatch.setattr(release, "_run", lambda *args, **kwargs: "")

    release.write_version("9.8.7")

    for relative in (*release.VERSIONED_JSON, release.PYPROJECT, release.VERSION_PY):
        before = (_REPO / relative).read_bytes()
        after = (tmp_path / relative).read_bytes()
        assert b"9.8.7" in after, relative
        # Same layout and line endings: only the number moved.
        assert len(after.splitlines()) == len(before.splitlines()), relative
        assert after.count(b"\r\n") == before.count(b"\r\n"), relative
    assert (tmp_path / "VERSION").read_text(encoding="utf-8") == "9.8.7\n"


@pytest.mark.unit
def test_a_release_skips_a_version_whose_tag_already_exists() -> None:
    """ClipForge's plan tagged v0.1.0 long before releases were versioned."""
    release = _release_module()
    assert release.free_version("0.0.9", {"v0.1.0"}) == "0.1.1"
    assert release.free_version("0.0.9", set()) == "0.1.0"
    assert release.free_version("0.1.0", {"v0.1.0", "v0.1.1"}) == "0.1.2"
