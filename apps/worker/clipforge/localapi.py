"""A small authenticated control API on loopback, for the desktop app.

## Why this exists

The YouTube client secret and refresh token stay on the worker and never reach
Firestore (D7, docs/adr/0010-worker-held-publishing-credentials.md). Taken
literally that would mean typing credentials into a terminal, which is a poor
experience for something you do once per channel.

The desktop shell resolves it: Tauri runs on the *same machine* as the worker, so
the app can hand the secret straight to the worker over loopback. It is entered
in a form, and it still never leaves the machine.

## Why it is not the file server

`localserver.py` is deliberately read-only and says so: "a convenience, not an
authenticated surface". Accepting credentials on that port would quietly make
that sentence false. A separate server keeps each one's properties obvious —
that one serves clips to anything that asks, this one takes writes and demands a
token.

## The threat model, stated plainly

Loopback is not a security boundary. Anything running as this user can reach
127.0.0.1, and so, in two specific ways, can a web page:

**Any local process.** Answered by a bearer token, generated on first start and
written owner-only next to the other credentials. A process that can read that
file could read the token store beside it anyway, so this adds no new trust.

**A malicious web page.** A browser can issue cross-origin requests to
127.0.0.1. Answered by requiring the token — which a page cannot read — *and* by
refusing any `Origin` that is not the desktop shell. Both, because either alone
has a bad day: CORS does not stop a non-browser client, and a token alone does
not stop a page that has somehow obtained one.

**DNS rebinding**, where a hostile name resolves to 127.0.0.1 so the page's own
origin appears local. Answered by requiring the `Host` header to be a loopback
literal; a rebound request carries the attacker's hostname.

What none of this defends against is malware already running as this user. That
is out of reach here, as it is for the token file itself, and pretending
otherwise would be worse than saying so.
"""

from __future__ import annotations

import json
import secrets
import socketserver
import stat
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = ["LocalControlApi", "read_or_create_token"]

# The desktop shell's origin, measured rather than assumed — see
# docs/adr/0011-local-control-api.md. A browser tab cannot forge this.
ALLOWED_ORIGINS = frozenset({"http://tauri.localhost", "https://tauri.localhost"})

# Host headers that can only mean "this machine". A DNS-rebinding request
# carries the attacker's hostname instead, which is the point of checking.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})

MAX_BODY_BYTES = 64 * 1024

# How much of an unread body is read and thrown away so that the reply reaches
# the caller rather than racing a connection reset. See `_drain_request`.
DRAIN_LIMIT_BYTES = 1024 * 1024
DRAIN_CHUNK_BYTES = 32 * 1024


def read_or_create_token(path: Path) -> str:
    """The shared secret between the worker and the desktop shell.

    Generated once and reused, so restarting the worker does not silently break
    an app that is already holding the old one.
    """
    path = path.expanduser()
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing

    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # Windows, where the meaningful control is ACLs rather than mode bits.
        # Noted rather than silently skipped: a reader should not assume this
        # file is protected there.
        log.debug("localapi.chmod_unsupported", path=str(path))
    return token


class _Handler(BaseHTTPRequestHandler):
    token: str
    routes: dict[tuple[str, str], Callable[[dict[str, Any]], dict[str, Any]]]

    server_version = "ClipForgeControl/1"

    # Whether this request's body has been read. A class-level default rather
    # than an __init__: BaseHTTPRequestHandler does the whole request inside its
    # constructor, so an override would have to set this before calling super()
    # and would read as though the ordering were incidental. One handler is
    # constructed per request, so the default is per request too.
    _drained = False

    def log_message(self, fmt: str, *args: object) -> None:
        log.debug("localapi.request", request=fmt % args)

    # ── Guards ───────────────────────────────────────────────────────────────

    def _host_is_loopback(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in LOOPBACK_HOSTS

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        # No Origin at all means a non-browser client — curl, the CLI, a test.
        # Those are gated by the token, which is the control that matters.
        return origin is None or origin in ALLOWED_ORIGINS

    def _authorised(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        # Constant-time: a token is a secret, and comparing it with == leaks its
        # prefix to anything that can measure.
        return secrets.compare_digest(header[7:], type(self).token)

    # ── Plumbing ─────────────────────────────────────────────────────────────

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        # Read whatever is still in flight before answering. Every refusal here
        # replies *before* touching the body — a bad token and a foreign origin
        # are decided from headers alone — so replying and closing leaves the
        # client's remaining bytes unread, and the OS answers the rest of that
        # write with a TCP reset. The caller then sees a connection error
        # instead of the reason it was refused, which is the one thing this API
        # exists to be able to say.
        self._drain_request()
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        if not self._host_is_loopback() or not self._origin_allowed():
            self._reply(403, {"error": "refused"})
            return
        self._reply(204, {})

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        if not self._host_is_loopback():
            self._reply(403, {"error": "this API answers on loopback only"})
            return
        if not self._origin_allowed():
            self._reply(403, {"error": "origin not allowed"})
            return
        if not self._authorised():
            self._reply(401, {"error": "missing or invalid bearer token"})
            return

        handler = type(self).routes.get((method, self.path.split("?", 1)[0]))
        if handler is None:
            self._reply(404, {"error": f"no route for {method} {self.path}"})
            return

        try:
            payload = self._body()
        except ValueError as exc:
            self._reply(400, {"error": str(exc)})
            return

        try:
            self._reply(200, handler(payload))
        except Exception as exc:  # noqa: BLE001 - a handler may raise anything
            log.warning("localapi.handler_failed", route=self.path, error=str(exc))
            self._reply(500, {"error": str(exc)})

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError(f"request body too large: {length} bytes, limit {MAX_BODY_BYTES}")
        raw = self.rfile.read(length)
        # Consumed exactly what was announced, so the reply has nothing to drain.
        self._drained = True
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"body is not JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("body must be a JSON object")
        return parsed

    def _drain_request(self) -> None:
        """Read and discard an unread request body, so the reply can be read.

        Bounded, because draining is a courtesy and an unbounded courtesy is a
        lever: a client that announces a gigabyte does not get a gigabyte read
        on its behalf. Past the limit the reset is the correct answer, and
        nothing that deliberately sent that is owed an explanation. Read in
        fixed chunks rather than in one call — the whole point of the size cap
        is to not allocate what a stranger announced.
        """
        if self._drained:
            return
        self._drained = True
        announced = int(self.headers.get("Content-Length") or 0)
        remaining = min(announced, DRAIN_LIMIT_BYTES)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, DRAIN_CHUNK_BYTES))
            if not chunk:
                return
            remaining -= len(chunk)


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class LocalControlApi:
    """The worker's loopback control surface.

    Routes are supplied by the caller rather than defined here, so this module
    stays about *transport and trust* and knows nothing about YouTube.
    """

    def __init__(
        self,
        routes: dict[tuple[str, str], Callable[[dict[str, Any]], dict[str, Any]]],
        *,
        token: str,
        port: int = 8766,
    ) -> None:
        self._routes = routes
        self._token = token
        # Not configurable, unlike the file server's. That one binds elsewhere
        # because a port might clash; this one accepts credentials, and there is
        # no version of exposing it that is a supported mode.
        self._host = "127.0.0.1"
        self._port = port
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def origin(self) -> str:
        return f"http://{self._host}:{self._port}"

    def start(self) -> None:
        handler = type("_BoundHandler", (_Handler,), {"token": self._token, "routes": self._routes})
        self._server = _Server((self._host, self._port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="clipforge-localapi", daemon=True
        )
        self._thread.start()
        log.info("localapi.started", origin=self.origin, routes=len(self._routes))

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        log.info("localapi.stopped")

    def __enter__(self) -> LocalControlApi:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()
