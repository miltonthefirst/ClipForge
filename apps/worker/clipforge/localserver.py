"""A read-only HTTP server over the workspace, bound to loopback.

This is playback branch 2 (docs/adr/0009-spark-tier-local-artefacts.md). With no
Cloud Storage there is no URL a phone can play — but the PWA opened **on this
machine** can play a clip if something serves it, and browsers exempt `localhost`
from mixed-content blocking, so it works even with the PWA served over HTTPS.

It is a convenience, not an authenticated surface, and the code is written to
keep it that way:

**Loopback only.** Binding to `0.0.0.0` would expose the workspace to the whole
network with no authentication at all. The bind address is configurable because
the port might clash, not because serving publicly is a supported mode — and
doing so logs a warning that says exactly that.

**Read-only, GET and HEAD.** Range requests are supported because `<video>`
seeking depends on them; nothing else is.

**Every path is confined to the workspace root.** Resolved and checked, not
merely prefix-matched — `..` traversal and symlinks both get caught by comparing
the *resolved* path.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import threading
from pathlib import Path
from urllib.parse import unquote, urlparse

from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = ["LocalFileServer"]

# Only what the review UI actually needs. An allowlist rather than a denylist:
# the workspace also holds transcripts and source video, and serving those by
# accident is exactly the kind of thing an allowlist prevents by construction.
SERVABLE_SUFFIXES = frozenset({".mp4", ".m4a", ".jpg", ".jpeg", ".png", ".webm"})

CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".m4a": "audio/mp4",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


# What the PWA asks for to confirm this is ClipForge and not whatever else
# happens to hold the port. See `_Handler._identify`.
IDENTITY_PATH = "/__clipforge"
IDENTITY_SERVICE = "clipforge-local-file-server"


class _Handler(http.server.BaseHTTPRequestHandler):
    root: Path

    # Quieter than the default, which writes an Apache-style line to stderr for
    # every range request a video player makes — and a player makes many.
    def log_message(self, fmt: str, *args: object) -> None:
        log.debug("localserver.request", request=fmt % args)

    def do_HEAD(self) -> None:
        if self._identify(body=False):
            return
        self._serve(body=False)

    def do_GET(self) -> None:
        if self._identify(body=True):
            return
        self._serve(body=True)

    def _identify(self, *, body: bool) -> bool:
        """Answer "are you ClipForge's file server?" — and nothing else.

        This exists because the PWA's probe could not previously tell this
        server from any other. It asked whether *something* was listening on the
        port, and something usually is: an unrelated app on 8765 answered, the
        app concluded clips were playable here, and every clip then pointed at a
        stranger's 404. The video element showed a poster, no error, and no way
        to guess why.

        A path rather than a header, so the answer survives the `no-cors` fetch
        the probe used to make and the ordinary one it makes now. Deliberately
        carries no information beyond identity and the workspace it serves.
        """
        if urlparse(self.path).path != IDENTITY_PATH:
            return False

        payload = json.dumps({"service": IDENTITY_SERVICE, "root": str(self.root)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(payload)
        return True

    def _resolve(self) -> Path | None:
        """Map a request path to a file inside the workspace, or refuse."""
        requested = unquote(urlparse(self.path).path).lstrip("/")
        if not requested:
            return None

        candidate = (self.root / requested).resolve()
        # Compare resolved paths: this catches `..` traversal and symlinks that
        # point outside, which a prefix check on the raw string would not.
        if self.root != candidate and self.root not in candidate.parents:
            log.warning("localserver.path_escape", requested=requested)
            return None
        if candidate.suffix.lower() not in SERVABLE_SUFFIXES:
            return None
        if not candidate.is_file():
            return None
        return candidate

    def _serve(self, *, body: bool) -> None:
        path = self._resolve()
        if path is None:
            self.send_error(404, "Not found")
            return

        size = path.stat().st_size
        content_type = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        start, end = self._range(size)

        with path.open("rb") as handle:
            if start is not None and end is not None:
                handle.seek(start)
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                length = size
                self.send_response(200)

            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            # Seeking in a <video> element does not work without this.
            self.send_header("Accept-Ranges", "bytes")
            # The PWA is served from a different origin (a dev server, or
            # Hosting), so it needs permission to fetch this at all.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            if not body:
                return
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    # A player seeking away mid-transfer. Entirely normal.
                    return
                remaining -= len(chunk)

    def _range(self, size: int) -> tuple[int | None, int | None]:
        """Parse a single `Range: bytes=` header. Open-ended ranges included."""
        header = self.headers.get("Range")
        if not header or not header.startswith("bytes="):
            return None, None
        spec = header.removeprefix("bytes=").split(",")[0].strip()
        first, _, last = spec.partition("-")
        try:
            start = int(first) if first else 0
            end = int(last) if last else size - 1
        except ValueError:
            return None, None
        if start >= size:
            return None, None
        return start, min(end, size - 1)


class _Server(socketserver.ThreadingTCPServer):
    # A video player opens several connections at once, and a single-threaded
    # server would make seeking feel broken.
    daemon_threads = True
    allow_reuse_address = True


class LocalFileServer:
    """Serves the workspace read-only, for the PWA running on this machine."""

    def __init__(self, root: Path, *, host: str = "127.0.0.1", port: int = 8765) -> None:
        self._root = root.expanduser().resolve()
        self._host = host
        self._port = port
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def origin(self) -> str:
        return f"http://{self._host}:{self.port}"

    @property
    def port(self) -> int:
        """The port actually bound, which differs from the requested one when 0
        was asked for — as tests do, to avoid clashing with a real worker."""
        if self._server is not None:
            return int(self._server.server_address[1])
        return self._port

    def start(self) -> None:
        if self._server is not None:
            return

        if self._host not in {"127.0.0.1", "localhost", "::1"}:
            log.warning(
                "localserver.not_loopback",
                host=self._host,
                hint=(
                    "this server has no authentication; binding it off loopback "
                    "exposes the workspace to the whole network"
                ),
            )

        handler = type("BoundHandler", (_Handler,), {"root": self._root})
        self._server = _Server((self._host, self._port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="localserver", daemon=True
        )
        self._thread.start()
        log.info("localserver.started", origin=self.origin, root=str(self._root))

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
        log.info("localserver.stopped")

    def __enter__(self) -> LocalFileServer:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()
