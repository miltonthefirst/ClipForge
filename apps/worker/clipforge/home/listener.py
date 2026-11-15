"""The small HTTPS app a phone talks to over the home wifi.

Deliberately not the worker's control API (clipforge/localapi.py) or its file
server (clipforge/localserver.py): both are loopback-only, and the control API
must never be reachable from the network. This app has these routes and
nothing else:

- GET /hello, with no sign-in, answers which machine this is and its version.
  The phone calls it to find out whether it is home.
- POST /media-links (when ``media_link`` is given) turns "play clip X" into a
  link, for a signed-in caller the project lets in (the home-route
  blueprint, section 5b).
- GET /media/{token} plays what a link was made for (media.py): the link is
  the key, as a <video> element cannot sign in.
- POST /commands (when ``run_command`` is given) runs one command. ClipForge
  passes none: its phone creates jobs through Firestore.

It is served by its own uvicorn, on its own thread, bound to the home network
address only, and refuses anything not from a private address.

Drawn from the home-route blueprint (P:/Experiments/blueprints). Against its
1.0.0 reference: the origins and the confirmed-email rule are parameters, and
/media-links exists.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from . import auth, media


class DirectUnavailable(Exception):
    """The backend cannot run a command directly right now. The phone then
    uses Firebase."""


# Firestore caps a document at 1 MiB, so nothing a phone sends is bigger than
# that; this leaves room without inviting abuse.
MAX_BODY_BYTES = 2 * 1024 * 1024

SERVICE = "clipforge-home"  # the PWA's HomeRouteService checks the same string


class CommandBody(BaseModel):
    # Made on the phone, the same shape as a Firestore document id, and the
    # id of the Firestore command if the phone ends up sending it that way.
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    type: str = Field(min_length=1, max_length=80)
    payload: dict[str, Any] = Field(default_factory=dict)
    station: str | None = None


class MediaLinkBody(BaseModel):
    """What to play. `kind` is the project's vocabulary (ClipForge: "clip")."""

    kind: str = Field(pattern=r"^[a-z]{1,20}$")
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")


# Turns (kind, id, caller) into (path, content type). Raises PermissionError
# for a caller the project does not let in, LookupError for nothing to play.
MediaLink = Callable[[str, str, auth.Caller], tuple[str, str]]


def origins_for(project_id: str) -> tuple[str, ...]:
    """The PWA's own addresses on Firebase Hosting, and no other page."""
    return (f"https://{project_id}.web.app", f"https://{project_id}.firebaseapp.com")


def build_app(
    station_id: Callable[[], str],
    version: str,
    *,
    allowed_origins: Iterable[str],
    base_url: Callable[[], str] | None = None,
    media_link: MediaLink | None = None,
    run_command: Callable[[str, str, dict[str, Any], str], dict[str, Any]] | None = None,
    verify_token: Callable[[str], dict[str, Any]] | None = None,
    require_verified_email: bool = True,
    on_command: Callable[[], None] | None = None,
) -> FastAPI:
    """`media_link` and `run_command` switch their routes on; `base_url` is
    where links point (the listener's own https://name:port); `verify_token`
    is the test seam for auth.py."""
    if media_link is not None and base_url is None:
        raise ValueError("media_link needs base_url, to say where its links point.")
    app = FastAPI(title="Home wifi", docs_url=None, redoc_url=None, openapi_url=None)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(allowed_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
        # Older Chrome asks a public page's request to a private address
        # this extra question first (Private Network Access), and gives up
        # without the yes.
        allow_private_network=True,
        max_age=600,
    )

    # Added after CORS, so it wraps it: the address check covers the
    # preflight too.
    @app.middleware("http")
    async def home_only(request: Request, call_next: Any) -> Any:
        if not _private(request.client.host if request.client else None):
            return JSONResponse(
                status_code=403, content={"detail": "Only devices on the home network."}
            )
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
            return JSONResponse(status_code=413, content={"detail": "Too large."})
        return await call_next(request)

    async def signed_in(authorization: str | None) -> auth.Caller:
        # Who is asking comes first, before anything is read: an unsigned
        # request costs no more than its headers.
        try:
            return await run_in_threadpool(
                auth.verify,
                authorization,
                verify_token,
                require_verified_email=require_verified_email,
            )
        except auth.NotSignedIn as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    async def capped(request: Request) -> bytes:
        # Read with a cap, which a chunked body (no Content-Length for the
        # middleware to check) is held to as well.
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Too large.")
        return bytes(raw)

    @app.get("/hello")
    def hello() -> dict[str, str]:
        return {"service": SERVICE, "station": station_id(), "version": version}

    @app.get("/media/{token}")
    def play(token: str) -> FileResponse:
        found = media.find(token)
        if found is None:
            raise HTTPException(status_code=404, detail="That link has expired. Open it again.")
        # A file, so the phone can seek in it (range requests).
        return FileResponse(
            found.path,
            media_type=found.content_type,
            headers={"Cache-Control": "private, max-age=3600"},
        )

    if media_link is not None and base_url is not None:
        link_for, base = media_link, base_url

        @app.post("/media-links")
        async def media_links(
            request: Request, authorization: str | None = Header(default=None)
        ) -> dict[str, str]:
            caller = await signed_in(authorization)
            try:
                body = MediaLinkBody.model_validate_json(await capped(request))
            except ValidationError as exc:
                raise HTTPException(
                    status_code=422, detail="Not something this PC can play."
                ) from exc
            try:
                path, content_type = await run_in_threadpool(link_for, body.kind, body.id, caller)
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            # On a worker thread: copying a long clip takes a moment, and the
            # event loop must keep answering /hello meanwhile.
            url, expires = await run_in_threadpool(
                lambda: media.offer(base(), content_type, path=path)
            )
            return {"url": url, "expiresAt": expires}

    if run_command is not None:
        command = run_command

        @app.post("/commands")
        async def commands(
            request: Request, authorization: str | None = Header(default=None)
        ) -> dict[str, Any]:
            caller = await signed_in(authorization)
            try:
                body = CommandBody.model_validate_json(await capped(request))
            except ValidationError as exc:
                raise HTTPException(
                    status_code=422, detail="Not a command this workstation understands."
                ) from exc
            if body.station and body.station != station_id():
                raise HTTPException(
                    status_code=409, detail="That command is for another workstation."
                )
            try:
                outcome = await run_in_threadpool(
                    command, body.id, body.type, body.payload, caller.uid
                )
            except DirectUnavailable as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            if on_command is not None:
                on_command()
            return {"id": body.id, **outcome}

    return app


def _private(host: str | None) -> bool:
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_private


class Listener:
    """One uvicorn, on its own thread, with TLS. start() binds the socket
    itself first, so a wrong address or a port in use comes back as an
    error to show rather than uvicorn quietly exiting its thread."""

    def __init__(self) -> None:
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._error: str | None = None

    @property
    def running(self) -> bool:
        return bool(
            self._thread and self._thread.is_alive() and self._server and self._server.started
        )

    @property
    def error(self) -> str | None:
        return self._error

    def start(
        self, app: FastAPI, host: str, port: int, certfile: str, keyfile: str, wait: float = 10.0
    ) -> None:
        import uvicorn

        self.stop()
        self._error = None
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, port))
            sock.listen(64)
        except OSError as exc:
            sock.close()
            self._error = _bind_error(host, port, exc)
            raise RuntimeError(self._error) from exc

        config = uvicorn.Config(
            app,
            ssl_certfile=certfile,
            ssl_keyfile=keyfile,
            # None: uvicorn must not reconfigure logging, which would throw
            # away the handler that feeds the Logs page (app/logs/capture.py).
            log_config=None,
            access_log=False,
            lifespan="off",
            timeout_graceful_shutdown=2,
        )
        server = uvicorn.Server(config)
        self._server = server

        def serve() -> None:
            try:
                server.run(sockets=[sock])
            except BaseException as exc:  # noqa: BLE001 - SystemExit included: uvicorn exits on a bad start
                self._error = f"The listener stopped: {type(exc).__name__}: {exc}"
            finally:
                sock.close()

        self._thread = threading.Thread(target=serve, name="home-wifi-listener", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and self._thread.is_alive() and not server.started:
            time.sleep(0.05)
        if not server.started:
            self.stop()
            self._error = self._error or "The listener did not start in time."
            raise RuntimeError(self._error)

    def stop(self, timeout: float = 10.0) -> None:
        server, thread = self._server, self._thread
        self._server, self._thread = None, None
        if server is not None:
            server.should_exit = True
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)


def _bind_error(host: str, port: int, exc: OSError) -> str:
    # WSAEADDRNOTAVAIL (10049): the address is not this PC's right now.
    if getattr(exc, "winerror", None) == 10049 or exc.errno in (99, 10049):
        return (
            f"This PC does not have the address {host} right now, so the listener cannot use it. "
            "Check the router's reservation (docs/HOME-NETWORK-PLAN.md section 3) or the address saved here."
        )
    # WSAEADDRINUSE (10048)
    if getattr(exc, "winerror", None) == 10048 or exc.errno in (98, 10048):
        return f"Port {port} is already in use on this PC."
    return f"Could not listen on {host}:{port}: {exc}"
