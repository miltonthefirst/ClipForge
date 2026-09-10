"""The authorise route: what it refuses, and what it refuses to store.

The route exists so nobody has to find a terminal every seven days. These tests
are less about that convenience than about the two properties it must not lose
on the way: a redirect that does not match the request stores nothing, and a
second press while a window is already open does not start a second dance.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from clipforge.config import Settings
from clipforge.publish import oauth as oauth_module
from clipforge.publish.channels import ChannelRoutes
from clipforge.publish.credentials import OAuthTokens, TokenStore

CLIENT_ID = "871720866960-test.apps.googleusercontent.com"


def _routes(tmp_path: Path) -> ChannelRoutes:
    settings = Settings(
        youtube_client_secrets=tmp_path / "youtube-client.json",
        youtube_token_store=tmp_path / "youtube-token.enc",
        publishing_enabled=True,
    )
    return ChannelRoutes(settings)


def _with_client(tmp_path: Path) -> ChannelRoutes:
    """A configured channel, written the way the app writes it."""
    routes = _routes(tmp_path)
    routes.set_client({"clientId": CLIENT_ID, "clientSecret": "a-secret"})
    return routes


def _settled(routes: ChannelRoutes, timeout_s: float = 5.0) -> dict:
    """Wait for the background attempt to finish, then report the status."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = routes.status({})
        if not status["authorising"]:
            return status
        time.sleep(0.02)
    raise AssertionError("the authorisation attempt never finished")


def test_authorising_without_a_client_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no OAuth client yet"):
        _routes(tmp_path).authorise({})


def test_an_existing_token_is_not_replaced_silently(tmp_path: Path, monkeypatch) -> None:
    routes = _with_client(tmp_path)
    TokenStore(tmp_path / "youtube-token.enc").save(OAuthTokens(refresh_token="already-here"))

    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)

    result = routes.authorise({})

    assert result["already"] is True
    assert opened == [], "a browser opened for an authorisation nobody asked to redo"


def test_a_mismatched_state_stores_nothing(tmp_path: Path, monkeypatch) -> None:
    """The whole reason for sending a state parameter.

    A redirect carrying someone else's state did not come from the request this
    worker made, and a token minted from it would be a token the operator never
    asked for.
    """
    routes = _with_client(tmp_path)
    monkeypatch.setattr("webbrowser.open", lambda _url: True)
    monkeypatch.setattr(
        oauth_module,
        "listen_for_redirect",
        lambda _port, timeout_s=0: oauth_module.AuthorizationResult(code="c", state="not-ours"),
    )

    def _explode(**_kwargs: object) -> OAuthTokens:
        raise AssertionError("the code was exchanged despite a mismatched state")

    monkeypatch.setattr(oauth_module, "exchange_code", _explode)

    assert routes.authorise({})["waiting"] is True
    status = _settled(routes)

    assert status["authError"] is not None
    assert not TokenStore(tmp_path / "youtube-token.enc").exists()
    assert status["connection"] == "NEEDS_AUTH"


def test_a_matching_redirect_stores_the_token(tmp_path: Path, monkeypatch) -> None:
    routes = _with_client(tmp_path)
    monkeypatch.setattr("webbrowser.open", lambda _url: True)

    sent: dict[str, str] = {}
    real_url = oauth_module.authorization_url

    def _record(**kwargs: str) -> tuple[str, str]:
        sent["state"] = kwargs["state"]
        return real_url(**kwargs)

    monkeypatch.setattr(oauth_module, "authorization_url", _record)
    monkeypatch.setattr(
        oauth_module,
        "listen_for_redirect",
        lambda _port, timeout_s=0: oauth_module.AuthorizationResult(code="c", state=sent["state"]),
    )
    monkeypatch.setattr(
        oauth_module,
        "exchange_code",
        lambda **_kwargs: OAuthTokens(refresh_token="fresh"),
    )

    routes.authorise({})
    status = _settled(routes)

    assert status["authError"] is None
    assert status["connection"] == "CONNECTED"
    assert TokenStore(tmp_path / "youtube-token.enc").load().refresh_token == "fresh"


def test_a_second_press_does_not_start_a_second_dance(tmp_path: Path, monkeypatch) -> None:
    """Two windows would strand the first state and hold the loopback port."""
    routes = _with_client(tmp_path)
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)

    release = __import__("threading").Event()
    monkeypatch.setattr(
        oauth_module,
        "listen_for_redirect",
        lambda _port, timeout_s=0: (
            release.wait(5),
            oauth_module.AuthorizationResult(code="c", state="not-ours"),
        )[1],
    )
    monkeypatch.setattr(oauth_module, "exchange_code", lambda **_kwargs: None)

    first = routes.authorise({})
    second = routes.authorise({})

    assert second["waiting"] is True
    assert second["url"] == first["url"], "a second press started a different dance"
    assert len(opened) == 1

    release.set()
    _settled(routes)
