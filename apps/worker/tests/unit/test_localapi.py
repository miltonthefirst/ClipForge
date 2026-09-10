"""The loopback control API, and the three ways in it refuses.

This surface accepts a Google OAuth client secret. Loopback is not a security
boundary — anything running as this user can reach 127.0.0.1, and a web page can
reach it too — so the guards are the feature, and these tests are the reason to
believe them.

Real HTTP against a real socket rather than calling the handler directly: what
is under test is header handling, and a test that constructed the headers itself
would be testing its own assumptions.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from clipforge.localapi import LocalControlApi, read_or_create_token

pytestmark = pytest.mark.unit


@pytest.fixture
def api(tmp_path: Path) -> Iterator[tuple[str, str, list[dict[str, Any]]]]:
    """A running API on an ephemeral port, plus the calls it received."""
    seen: list[dict[str, Any]] = []

    def echo(payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(payload)
        return {"ok": True, "got": payload}

    token = read_or_create_token(tmp_path / "token")
    # Port 0 lets the OS pick a free one; hardcoding a port makes a test suite
    # that fails when something else on the machine happens to want it.
    server = LocalControlApi(
        {("GET", "/status"): echo, ("POST", "/set"): echo}, token=token, port=0
    )
    server.start()
    assert server._server is not None
    port = server._server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}", token, seen
    finally:
        server.stop()


def test_a_valid_request_reaches_its_route(api: tuple[str, str, list[dict[str, Any]]]) -> None:
    base, token, seen = api
    response = httpx.post(
        f"{base}/set", headers={"Authorization": f"Bearer {token}"}, json={"clientId": "abc"}
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert seen == [{"clientId": "abc"}]


def test_no_token_is_refused(api: tuple[str, str, list[dict[str, Any]]]) -> None:
    """The guard against any other process on this machine."""
    base, _token, seen = api
    response = httpx.post(f"{base}/set", json={"clientId": "abc"})
    assert response.status_code == 401
    assert seen == []


def test_a_wrong_token_is_refused(api: tuple[str, str, list[dict[str, Any]]]) -> None:
    base, _token, seen = api
    response = httpx.post(
        f"{base}/set", headers={"Authorization": "Bearer not-the-token"}, json={"x": 1}
    )
    assert response.status_code == 401
    assert seen == []


def test_a_foreign_origin_is_refused_even_with_the_token(
    api: tuple[str, str, list[dict[str, Any]]],
) -> None:
    """A page the user happens to be visiting must not reach this.

    Belt and braces with the token: CORS alone would not stop a non-browser
    client, and the token alone would not stop a page that had somehow obtained
    one. The route must not run.
    """
    base, token, seen = api
    response = httpx.post(
        f"{base}/set",
        headers={"Authorization": f"Bearer {token}", "Origin": "https://evil.example"},
        json={"clientId": "abc"},
    )
    assert response.status_code == 403
    assert seen == []


def test_the_desktop_shell_origin_is_allowed(api: tuple[str, str, list[dict[str, Any]]]) -> None:
    base, token, seen = api
    response = httpx.post(
        f"{base}/set",
        headers={"Authorization": f"Bearer {token}", "Origin": "http://tauri.localhost"},
        json={"clientId": "abc"},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://tauri.localhost"
    assert len(seen) == 1


def test_a_rebound_host_header_is_refused(api: tuple[str, str, list[dict[str, Any]]]) -> None:
    """DNS rebinding: a hostile name resolved to 127.0.0.1.

    The connection genuinely is local, so the address proves nothing. What gives
    it away is the Host header still carrying the attacker's name.
    """
    base, token, seen = api
    port = base.rsplit(":", 1)[1]
    response = httpx.post(
        f"{base}/set",
        headers={
            "Authorization": f"Bearer {token}",
            "Host": f"attacker.example:{port}",
        },
        json={"clientId": "abc"},
    )
    assert response.status_code == 403
    assert seen == []


def test_a_preflight_from_the_shell_is_answered(api: tuple[str, str, list[dict[str, Any]]]) -> None:
    base, _token, _seen = api
    response = httpx.request("OPTIONS", f"{base}/set", headers={"Origin": "http://tauri.localhost"})
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "http://tauri.localhost"


def test_a_preflight_from_elsewhere_is_not(api: tuple[str, str, list[dict[str, Any]]]) -> None:
    base, _token, _seen = api
    response = httpx.request("OPTIONS", f"{base}/set", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers


def test_an_unknown_route_is_a_404_not_a_500(api: tuple[str, str, list[dict[str, Any]]]) -> None:
    base, token, _seen = api
    response = httpx.get(f"{base}/nope", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 404


def test_a_body_that_is_not_json_is_rejected(api: tuple[str, str, list[dict[str, Any]]]) -> None:
    base, token, seen = api
    response = httpx.post(
        f"{base}/set",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        content=b"{not json",
    )
    assert response.status_code == 400
    assert seen == []


def test_an_oversized_body_is_rejected_rather_than_read(
    api: tuple[str, str, list[dict[str, Any]]],
) -> None:
    """A credential is small. Anything large is either a mistake or an attempt
    to make the worker allocate."""
    base, token, seen = api
    response = httpx.post(
        f"{base}/set",
        headers={"Authorization": f"Bearer {token}"},
        content=json.dumps({"x": "y" * 200_000}).encode(),
    )
    assert response.status_code == 400
    assert seen == []


def test_a_handler_that_raises_becomes_a_500_with_its_reason(tmp_path: Path) -> None:
    def explode(_payload: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("that is not a client id")

    token = read_or_create_token(tmp_path / "token")
    server = LocalControlApi({("POST", "/boom"): explode}, token=token, port=0)
    server.start()
    assert server._server is not None
    port = server._server.server_address[1]
    try:
        response = httpx.post(
            f"http://127.0.0.1:{port}/boom",
            headers={"Authorization": f"Bearer {token}"},
            json={},
        )
        assert response.status_code == 500
        # The reason travels: this API's only client is a settings form, and
        # "500" alone would leave the user with nothing to act on.
        assert "not a client id" in response.json()["error"]
    finally:
        server.stop()


# ── The token file ───────────────────────────────────────────────────────────


def test_the_token_is_generated_once_and_reused(tmp_path: Path) -> None:
    """Regenerating on restart would silently break an app already holding it."""
    path = tmp_path / "token"
    first = read_or_create_token(path)
    assert read_or_create_token(path) == first
    assert len(first) > 30


def test_an_empty_token_file_is_replaced(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text("", encoding="utf-8")
    assert read_or_create_token(path).strip() != ""
