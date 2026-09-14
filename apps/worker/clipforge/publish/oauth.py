"""The one-time OAuth authorisation flow, run from the terminal.

Google's installed-application flow: open a consent page in the browser, catch
the redirect on a loopback port, exchange the code for a refresh token. The
refresh token then lives on this machine and the worker uses it unattended.

**PKCE is used even though this client has a secret.** A "client secret" shipped
in a desktop application is not a secret in any meaningful sense — anyone with
the file has it. PKCE is what actually binds the authorisation code to the
process that requested it, so an attacker who intercepts the redirect cannot
exchange the code. Google's own guidance for installed apps says the same.

The loopback listener is bound to 127.0.0.1 and serves exactly one request. It is
not a web server and must not become one.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import threading
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from clipforge.publish.credentials import CredentialError, OAuthTokens
from clipforge.publish.youtube import ANALYTICS_SCOPE, TOKEN_URL, UPLOAD_SCOPE

__all__ = ["AuthorizationResult", "authorization_url", "exchange_code", "listen_for_redirect"]

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

# `youtube.readonly` is requested alongside upload so a retry can ask whether a
# video already exists. Without it, reconciling an interrupted upload would be
# impossible and the safe fallback would be to never retry at all.
#
# `yt-analytics.readonly` arrived with Phase 9 and is the reason an operator who
# authorised before it has to authorise again. It is requested here rather than
# incrementally, on its own consent screen, because two separate authorisations
# is two things to get right and the second one would only ever be prompted for
# by a failure days later, when the first metrics poll came back 403.
SCOPES = (
    UPLOAD_SCOPE,
    "https://www.googleapis.com/auth/youtube.readonly",
    ANALYTICS_SCOPE,
)

_DONE_PAGE = b"""<!doctype html><meta charset="utf-8">
<title>ClipForge</title>
<body style="font:16px system-ui;padding:3rem;max-width:34rem">
<h1>Authorised</h1>
<p>ClipForge can now upload to your channel. You can close this tab and return
to ClipForge.</p>
<p style="color:#666">The token is stored on this machine only.</p>
"""


@dataclass(frozen=True)
class AuthorizationResult:
    code: str
    state: str


def _pkce_pair() -> tuple[str, str]:
    """A verifier and its S256 challenge."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorization_url(
    *, client_id: str, redirect_uri: str, state: str, verifier: str | None = None
) -> tuple[str, str]:
    """Build the consent URL. Returns ``(url, code_verifier)``."""
    verifier = verifier or _pkce_pair()[0]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # Without both of these Google returns no refresh token on a repeat
        # authorisation — and the second time someone runs this is exactly when
        # they most need one.
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}", verifier


def _handler_for(inbox: dict[str, str]) -> type[http.server.BaseHTTPRequestHandler]:
    """A one-shot redirect handler that reports into ``inbox``.

    Built per call, closing over a plain dict rather than storing the result on
    the handler class. ``http.server`` instantiates the handler itself, so shared
    state is the only channel back — and shared state on the *class* would mean
    two authorisation flows on one machine could read each other's codes.
    """

    class RedirectHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = query.get("code", [""])[0]
            error = query.get("error", [""])[0]

            if error:
                inbox["error"] = error
                self.send_response(400)
                self.end_headers()
                self.wfile.write(f"Authorisation failed: {error}".encode())
                return
            if not code:
                self.send_response(404)
                self.end_headers()
                return

            inbox["code"] = code
            inbox["state"] = query.get("state", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_DONE_PAGE)

        def log_message(self, *args: object) -> None:
            """Silence the default stderr logging: the URL contains the code."""

    return RedirectHandler


def listen_for_redirect(port: int, *, timeout_s: float = 300.0) -> AuthorizationResult:
    """Serve exactly one request on loopback and return what it carried."""
    inbox: dict[str, str] = {}

    server = http.server.HTTPServer(("127.0.0.1", port), _handler_for(inbox))
    server.timeout = timeout_s
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout=timeout_s)
    server.server_close()

    if "error" in inbox:
        raise CredentialError(f"authorisation was refused: {inbox['error']}")
    if "code" not in inbox:
        raise CredentialError(
            f"no redirect arrived on port {port} within {int(timeout_s)}s. "
            "Check that the OAuth client lists "
            f"http://127.0.0.1:{port}/ as an authorised redirect URI."
        )
    return AuthorizationResult(code=inbox["code"], state=inbox.get("state", ""))


def exchange_code(
    *,
    code: str,
    verifier: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    now: datetime | None = None,
    transport: httpx.BaseTransport | None = None,
) -> OAuthTokens:
    """Trade the authorisation code for tokens."""
    now = now or datetime.now(UTC)
    with httpx.Client(timeout=30.0, transport=transport) as client:
        response = client.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )

    if response.status_code != 200:
        raise CredentialError(
            f"the token exchange failed: {response.status_code} {response.text[:300]}"
        )

    body = response.json()
    refresh = body.get("refresh_token")
    if not refresh:
        # Without a refresh token the worker can publish for one hour and then
        # stop, which would look like an intermittent fault rather than a setup
        # problem. Refuse now and say why.
        raise CredentialError(
            "Google returned no refresh token. Revoke ClipForge's access at "
            "https://myaccount.google.com/permissions and authorise again — a repeat "
            "authorisation only returns one when the previous grant has been removed."
        )

    return OAuthTokens(
        refresh_token=str(refresh),
        access_token=body.get("access_token"),
        expires_at=now + timedelta(seconds=int(body.get("expires_in", 3600)) - 60),
        scopes=tuple(str(body.get("scope", "")).split()) or SCOPES,
        obtained_at=now,
    )
