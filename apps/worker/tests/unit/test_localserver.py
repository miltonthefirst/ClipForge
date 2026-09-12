"""The local file server: what it serves, and — mostly — what it refuses.

This is the one component that exposes the filesystem over a socket, so the tests
that matter here are the negative ones. It has no authentication by design, which
is defensible only because it is confined to loopback, to a directory, and to a
handful of extensions — and each of those is asserted rather than assumed.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from clipforge.localserver import IDENTITY_PATH, IDENTITY_SERVICE, LocalFileServer


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "clips" / "user-1").mkdir(parents=True)
    (root / "clips" / "user-1" / "clip.mp4").write_bytes(b"video-bytes-" * 100)
    (root / "clips" / "user-1" / "poster.jpg").write_bytes(b"jpeg")
    # Deliberately present: the workspace also holds transcripts and source
    # media, and the allowlist must keep them unreachable.
    (root / "transcripts").mkdir()
    (root / "transcripts" / "secret.json").write_text("private", encoding="utf-8")
    (tmp_path / "outside.mp4").write_bytes(b"not yours")
    return root


@pytest.fixture
def server(workspace: Path) -> Iterator[LocalFileServer]:
    # Port 0: the OS picks a free one, so the suite never clashes with a real
    # worker running on this machine.
    with LocalFileServer(workspace, port=0) as running:
        yield running


def fetch(
    server: LocalFileServer, path: str, headers: dict[str, str] | None = None
) -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(f"{server.origin}/{path}", headers=headers or {})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, b"", dict(exc.headers or {})


@pytest.mark.unit
def test_a_clip_is_served(server: LocalFileServer) -> None:
    status, body, headers = fetch(server, "clips/user-1/clip.mp4")
    assert status == 200
    assert body.startswith(b"video-bytes-")
    assert headers["Content-Type"] == "video/mp4"


@pytest.mark.unit
def test_range_requests_are_supported_so_video_seeking_works(
    server: LocalFileServer,
) -> None:
    """A <video> element cannot seek without these, which makes a clip feel
    broken even though it plays."""
    status, body, headers = fetch(server, "clips/user-1/clip.mp4", {"Range": "bytes=6-10"})
    assert status == 206
    assert body == b"bytes"
    assert headers["Content-Range"].startswith("bytes 6-10/")


@pytest.mark.unit
def test_the_accept_ranges_header_advertises_seeking(server: LocalFileServer) -> None:
    _, _, headers = fetch(server, "clips/user-1/clip.mp4")
    assert headers["Accept-Ranges"] == "bytes"


@pytest.mark.unit
def test_cors_is_open_so_the_pwa_can_fetch_from_another_origin(
    server: LocalFileServer,
) -> None:
    """The PWA is served from a dev server or from Hosting — a different origin
    in both cases."""
    _, _, headers = fetch(server, "clips/user-1/poster.jpg")
    assert headers["Access-Control-Allow-Origin"] == "*"


# ─────────────────────────────────────────────────────────────────────────────
# What it refuses. The important half.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    [
        "../outside.mp4",
        "clips/../../outside.mp4",
        "clips/user-1/../../../outside.mp4",
        "%2e%2e/outside.mp4",
    ],
)
def test_traversal_outside_the_workspace_is_refused(server: LocalFileServer, path: str) -> None:
    """Compared on RESOLVED paths, so URL-encoded traversal and symlinks are
    caught too — a prefix check on the raw string would not catch either."""
    status, _, _ = fetch(server, path)
    assert status == 404


@pytest.mark.unit
def test_files_outside_the_allowlist_are_refused(server: LocalFileServer) -> None:
    """The workspace also holds transcripts and source media. An allowlist keeps
    those unreachable by construction rather than by remembering to exclude
    them."""
    status, _, _ = fetch(server, "transcripts/secret.json")
    assert status == 404


@pytest.mark.unit
def test_a_missing_file_is_a_plain_404(server: LocalFileServer) -> None:
    assert fetch(server, "clips/user-1/nope.mp4")[0] == 404


@pytest.mark.unit
def test_a_directory_is_not_listed(server: LocalFileServer) -> None:
    """Directory listing would enumerate every clip on the machine."""
    assert fetch(server, "clips/user-1/")[0] == 404
    assert fetch(server, "")[0] == 404


@pytest.mark.unit
def test_writes_are_not_supported(server: LocalFileServer) -> None:
    """Read-only means read-only: no PUT, POST or DELETE handler exists."""
    request = urllib.request.Request(  # noqa: S310
        f"{server.origin}/clips/user-1/clip.mp4", method="DELETE"
    )
    try:
        with urllib.request.urlopen(request, timeout=10):  # noqa: S310
            pytest.fail("DELETE was accepted")
    except urllib.error.HTTPError as exc:
        assert exc.code in {400, 405, 501}


@pytest.mark.unit
def test_the_default_bind_is_loopback() -> None:
    """There is no authentication. Loopback is what makes that defensible."""
    assert LocalFileServer(Path.cwd()).origin.startswith("http://127.0.0.1:")


@pytest.mark.unit
def test_stopping_is_idempotent(workspace: Path) -> None:
    server = LocalFileServer(workspace, port=0)
    server.start()
    server.stop()
    server.stop()


# ─────────────────────────────────────────────────────────────────────────────
# Identity
#
# The PWA has to be able to tell this server from whatever else holds the port.
# It could not, once: an unrelated app owned 8765, ClipForge's server failed to
# bind, and every clip pointed at that app's 404 behind an untouched poster.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_the_server_identifies_itself(server: LocalFileServer, workspace: Path) -> None:
    status, body, _ = fetch(server, "__clipforge")

    assert status == 200
    payload = json.loads(body)
    assert payload["service"] == IDENTITY_SERVICE
    assert payload["root"] == str(workspace.resolve())


@pytest.mark.unit
def test_identity_is_not_confused_with_a_file(server: LocalFileServer) -> None:
    """It is answered before the path is resolved, so no clip can shadow it."""
    status, _, headers = fetch(server, "__clipforge")

    assert status == 200
    assert headers.get("Content-Type") == "application/json"


@pytest.mark.unit
def test_the_identity_string_matches_the_one_the_pwa_looks_for() -> None:
    """Both halves of a handshake, and nothing but this test relating them.

    `playback.ts` compares against a literal. If either side is renamed alone,
    every clip silently falls back to the cloud copy or the poster — working,
    slower, and for no visible reason anyone could find.
    """
    playback = (
        Path(__file__).resolve().parents[3] / "web" / "src" / "app" / "core" / "playback.ts"
    ).read_text(encoding="utf-8")

    assert f"const IDENTITY_PATH = '{IDENTITY_PATH}'" in playback
    assert f"const IDENTITY_SERVICE = '{IDENTITY_SERVICE}'" in playback
