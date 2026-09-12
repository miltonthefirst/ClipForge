"""Worker-held credentials, the quota ledger, and the YouTube client.

Phase 8, exit criteria 3 and 4. No network: every HTTP interaction goes through
an `httpx.MockTransport`, which means these assert what the client *sends* as
well as how it handles what comes back. A test that only stubbed the return
value would not have caught `prompt=consent` being missing, and the symptom of
that is "publishing silently stops working a week later".
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from clipforge.publish.credentials import CredentialError, OAuthTokens, TokenStore, _xor
from clipforge.publish.oauth import SCOPES, authorization_url, exchange_code
from clipforge.publish.youtube import (
    DAILY_QUOTA_UNITS,
    UPLOAD_QUOTA_UNITS,
    QuotaExceededError,
    QuotaLedger,
    YouTubeClient,
    YouTubeError,
    load_client_secrets,
)

# Every test in this file is the unit tier. Without this marker CI's
# `pytest -m unit` silently deselects the whole file — the tests pass locally,
# run nowhere, and protect nothing.
pytestmark = pytest.mark.unit

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


# ── Storage ──────────────────────────────────────────────────────────────────


def test_tokens_round_trip_through_the_encrypted_file(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "token.enc")
    tokens = OAuthTokens(
        refresh_token="1//refresh-secret",
        access_token="ya29.access",
        expires_at=NOW + timedelta(hours=1),
        scopes=SCOPES,
        obtained_at=NOW,
    )
    store.save(tokens)

    loaded = store.load()
    assert loaded.refresh_token == tokens.refresh_token
    assert loaded.access_token == tokens.access_token
    assert loaded.scopes == SCOPES


def test_the_token_never_appears_in_plaintext_on_disk(tmp_path: Path) -> None:
    """Exit criterion 4, the local half.

    Encryption here protects against a token being read out of a backup or a
    synced folder — see the module docstring for what it deliberately does not
    protect against. This asserts the modest claim it actually makes.
    """
    path = tmp_path / "token.enc"
    TokenStore(path).save(OAuthTokens(refresh_token="1//a-very-distinctive-secret"))

    raw = path.read_bytes()
    assert b"a-very-distinctive-secret" not in raw
    assert b"refreshToken" not in raw


def test_a_file_that_will_not_decrypt_says_why_and_what_to_do(tmp_path: Path) -> None:
    path = tmp_path / "token.enc"
    path.write_bytes(b"this is not a ClipForge token file")

    with pytest.raises(CredentialError) as caught:
        TokenStore(path).load()
    assert "youtube-auth" in str(caught.value)


def test_a_missing_token_file_names_the_command_that_creates_one(tmp_path: Path) -> None:
    with pytest.raises(CredentialError) as caught:
        TokenStore(tmp_path / "absent.enc").load()
    assert "youtube-auth" in str(caught.value)


def test_the_keystream_is_an_involution_and_is_not_the_identity() -> None:
    payload = b"x" * 100
    key = b"k" * 32
    assert _xor(_xor(payload, key), key) == payload
    assert _xor(payload, key) != payload


def test_saving_replaces_atomically_and_leaves_no_partial_file(tmp_path: Path) -> None:
    """A crash mid-write must not leave a half-token that fails to decrypt on
    the next run, which would look like a revoked grant rather than a bad
    write."""
    store = TokenStore(tmp_path / "token.enc")
    store.save(OAuthTokens(refresh_token="first"))
    store.save(OAuthTokens(refresh_token="second"))

    assert store.load().refresh_token == "second"
    assert list(tmp_path.glob("*.partial")) == []


def test_redacted_never_carries_the_token_itself() -> None:
    """Logged on every save. A token in a log file is a token in a log file."""
    redacted = OAuthTokens(refresh_token="1//secret", access_token="ya29.secret").redacted()
    assert "1//secret" not in json.dumps(redacted)
    assert "ya29.secret" not in json.dumps(redacted)
    assert redacted["hasRefreshToken"] is True


def test_expiry_is_judged_from_the_stored_time() -> None:
    fresh = OAuthTokens(refresh_token="r", access_token="a", expires_at=NOW + timedelta(minutes=5))
    assert fresh.is_expired(NOW) is False
    assert fresh.is_expired(NOW + timedelta(minutes=10)) is True

    # No access token at all counts as expired: there is nothing to use.
    assert OAuthTokens(refresh_token="r").is_expired(NOW) is True


# ── Quota ────────────────────────────────────────────────────────────────────


def test_the_ledger_allows_six_uploads_and_then_stops() -> None:
    """10,000 units a day, 1,600 an upload. Six, with 400 to spare."""
    # `today()` rather than a pinned date: the ledger rolls itself over against
    # the wall clock, so a ledger dated in the past would reset itself on first
    # read and the test would assert nothing.
    ledger = QuotaLedger.today()
    assert ledger.uploads_remaining == DAILY_QUOTA_UNITS // UPLOAD_QUOTA_UNITS == 6

    for _ in range(6):
        assert ledger.can_afford(UPLOAD_QUOTA_UNITS)
        ledger.spend(UPLOAD_QUOTA_UNITS)

    assert ledger.can_afford(UPLOAD_QUOTA_UNITS) is False
    assert ledger.uploads_remaining == 0


def test_the_ledger_resets_when_the_day_changes() -> None:
    ledger = QuotaLedger(day="2026-05-31", used_units=DAILY_QUOTA_UNITS)
    ledger.roll_over(now=NOW)
    assert ledger.used_units == 0
    assert ledger.day == "2026-06-01"


# ── The client ───────────────────────────────────────────────────────────────


def _store_with(tmp_path: Path, tokens: OAuthTokens) -> TokenStore:
    store = TokenStore(tmp_path / "token.enc")
    store.save(tokens)
    return store


def _client(
    tmp_path: Path,
    handler: object,
    *,
    tokens: OAuthTokens | None = None,
    ledger: QuotaLedger | None = None,
) -> YouTubeClient:
    stored = tokens or OAuthTokens(
        refresh_token="1//r", access_token="ya29.a", expires_at=NOW + timedelta(days=3650)
    )
    return YouTubeClient(
        tokens=_store_with(tmp_path, stored),
        client_id="cid",
        client_secret="csecret",
        ledger=ledger,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


def test_a_valid_access_token_is_reused_without_a_network_call(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"should not have called {request.url}")

    assert _client(tmp_path, handler).access_token() == "ya29.a"


def test_an_expired_access_token_is_refreshed_and_written_back(tmp_path: Path) -> None:
    """Written back so a restart does not need the network before it can do
    anything, and so a long run does not refresh on every upload."""
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(urllib.parse.parse_qsl(request.content.decode())))
        return httpx.Response(200, json={"access_token": "ya29.fresh", "expires_in": 3600})

    store = _store_with(tmp_path, OAuthTokens(refresh_token="1//r"))
    client = YouTubeClient(
        tokens=store,
        client_id="cid",
        client_secret="csecret",
        transport=httpx.MockTransport(handler),
    )

    assert client.access_token(now=NOW) == "ya29.fresh"
    assert seen[0]["grant_type"] == "refresh_token"
    assert seen[0]["refresh_token"] == "1//r"
    assert store.load().access_token == "ya29.fresh"


def test_an_expired_refresh_token_names_the_seven_day_testing_limit(tmp_path: Path) -> None:
    """The documented risk, and the one an opaque 401 would hide."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    client = _client(tmp_path, handler, tokens=OAuthTokens(refresh_token="1//stale"))

    with pytest.raises(YouTubeError) as caught:
        client.access_token(now=NOW)
    assert caught.value.code == "REAUTH_REQUIRED"
    assert caught.value.retryable is False
    assert "7 days" in str(caught.value)
    assert "youtube-auth" in str(caught.value)


def test_starting_an_upload_sends_the_metadata_and_defaults_to_unlisted(
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, headers={"Location": "https://upload.example/session-1"})

    url = _client(tmp_path, handler).start_resumable_upload(
        title="A hook", description="why", tags=["clip"]
    )

    assert url == "https://upload.example/session-1"
    assert captured["params"] == {"uploadType": "resumable", "part": "snippet,status"}
    status = captured["status"]
    assert isinstance(status, dict)
    assert status["privacyStatus"] == "unlisted"


def test_an_over_long_title_is_truncated_rather_than_rejected(tmp_path: Path) -> None:
    """YouTube refuses titles over 100 characters outright, and a hook is not
    written to a length limit. Losing the tail beats losing the upload."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, headers={"Location": "https://upload.example/s"})

    _client(tmp_path, handler).start_resumable_upload(title="x" * 250, description="d", tags=[])
    snippet = captured["snippet"]
    assert isinstance(snippet, dict)
    assert len(snippet["title"]) == 100


def test_a_full_quota_refuses_before_reserving_a_session(tmp_path: Path) -> None:
    """Checked before the request, not after. Discovering the limit on the
    seventh upload means discovering it after the encode and the review."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the quota check should have refused before any request")

    spent = QuotaLedger.today()
    spent.spend(DAILY_QUOTA_UNITS - 1)
    client = _client(tmp_path, handler, ledger=spent)

    with pytest.raises(QuotaExceededError) as caught:
        client.start_resumable_upload(title="t", description="d", tags=[])
    assert caught.value.retryable is True  # it resets at midnight Pacific


def test_a_successful_upload_returns_the_video_and_charges_the_quota(
    tmp_path: Path,
) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 2048)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        return httpx.Response(200, json={"id": "vid-123"})

    ledger = QuotaLedger.today()
    client = _client(tmp_path, handler, ledger=ledger)
    result = client.upload_bytes("https://upload.example/session-1", clip)

    assert result.video_id == "vid-123"
    assert result.url == "https://www.youtube.com/watch?v=vid-123"
    assert ledger.used_units == UPLOAD_QUOTA_UNITS


def test_a_failed_upload_does_not_charge_the_quota(tmp_path: Path) -> None:
    """Over-counting would refuse uploads that the allowance could actually
    cover, which is a worse failure than the one it guards against."""
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 16)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="backend error")

    ledger = QuotaLedger.today()
    with pytest.raises(YouTubeError):
        _client(tmp_path, handler, ledger=ledger).upload_bytes("https://upload.example/s", clip)

    assert ledger.used_units == 0


def test_an_interrupted_upload_is_reported_as_resumable(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 16)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(308, headers={"Range": "bytes=0-7"})

    with pytest.raises(YouTubeError) as caught:
        _client(tmp_path, handler).upload_bytes("https://upload.example/s", clip)

    assert caught.value.code == "UPLOAD_INCOMPLETE"
    assert caught.value.retryable is True


def test_a_missing_clip_file_is_not_retryable(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not reach the network")

    with pytest.raises(YouTubeError) as caught:
        _client(tmp_path, handler).upload_bytes("https://upload.example/s", tmp_path / "gone.mp4")

    assert caught.value.retryable is False
    assert caught.value.code == "CLIP_MISSING"


def test_find_video_reports_whether_the_platform_already_has_it(tmp_path: Path) -> None:
    """The reconciliation lookup. This is what tells "upload succeeded but the
    acknowledgement was lost" apart from "upload never happened"."""

    def found(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"id": "vid-123"}]})

    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    assert _client(tmp_path, found).find_video("vid-123") == {"id": "vid-123"}
    assert _client(tmp_path, missing).find_video("vid-123") is None


# ── OAuth flow ───────────────────────────────────────────────────────────────


def test_the_consent_url_asks_for_offline_access_and_forces_a_prompt() -> None:
    """Without both, Google returns no refresh token on a repeat authorisation —
    and the second time someone runs this is when they most need one."""
    url, verifier = authorization_url(
        client_id="cid", redirect_uri="http://127.0.0.1:8766/", state="st"
    )
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))

    assert params["access_type"] == "offline"
    assert params["prompt"] == "consent"
    assert params["code_challenge_method"] == "S256"
    assert params["state"] == "st"
    assert set(params["scope"].split()) == set(SCOPES)
    # The verifier is returned, never sent.
    assert verifier not in url


def test_the_code_challenge_is_the_s256_of_the_verifier() -> None:
    """PKCE is what actually binds the code to this process; a client secret
    shipped in a desktop app is not a secret."""
    import base64
    import hashlib

    url, verifier = authorization_url(
        client_id="cid", redirect_uri="http://127.0.0.1:8766/", state="st"
    )
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    assert params["code_challenge"] == expected


def test_the_exchange_sends_the_verifier_and_returns_a_refresh_token() -> None:
    sent: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(dict(urllib.parse.parse_qsl(request.content.decode())))
        return httpx.Response(
            200,
            json={
                "refresh_token": "1//new",
                "access_token": "ya29.new",
                "expires_in": 3600,
                "scope": " ".join(SCOPES),
            },
        )

    tokens = exchange_code(
        code="auth-code",
        verifier="ver",
        client_id="cid",
        client_secret="csecret",
        redirect_uri="http://127.0.0.1:8766/",
        now=NOW,
        transport=httpx.MockTransport(handler),
    )

    assert sent["code_verifier"] == "ver"
    assert sent["grant_type"] == "authorization_code"
    assert tokens.refresh_token == "1//new"
    assert tokens.expires_at is not None


def test_an_exchange_with_no_refresh_token_refuses_and_says_how_to_fix_it() -> None:
    """Without one the worker publishes for an hour and then stops, which reads
    as an intermittent fault rather than a setup problem."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "ya29.only", "expires_in": 3600})

    with pytest.raises(CredentialError) as caught:
        exchange_code(
            code="c",
            verifier="v",
            client_id="cid",
            client_secret="cs",
            redirect_uri="http://127.0.0.1:8766/",
            transport=httpx.MockTransport(handler),
        )
    assert "myaccount.google.com/permissions" in str(caught.value)


# ── Client secrets ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("block", ["installed", "web"])
def test_client_secrets_are_read_from_either_google_layout(tmp_path: Path, block: str) -> None:
    path = tmp_path / "client.json"
    path.write_text(json.dumps({block: {"client_id": "cid", "client_secret": "cs"}}))
    assert load_client_secrets(path) == ("cid", "cs")


def test_a_missing_client_secrets_file_explains_where_to_get_one(tmp_path: Path) -> None:
    with pytest.raises(CredentialError) as caught:
        load_client_secrets(tmp_path / "nope.json")
    assert "CLIPFORGE_YOUTUBE_CLIENT_SECRETS" in str(caught.value)


def test_a_json_file_that_is_not_client_secrets_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "client.json"
    path.write_text(json.dumps({"something": "else"}))
    with pytest.raises(CredentialError) as caught:
        load_client_secrets(path)
    assert "not a Google OAuth client secrets file" in str(caught.value)
