"""Runs the home wifi route for the agent: the certificate, the listener, and
what the app is told about both (docs/HOME-NETWORK-PLAN.md).

One background thread does everything that takes time. While the route is
wanted it gets the certificate if there is none, checks for a renewal once a
day (lego decides whether one is due), and keeps the listener running with the
current certificate, restarting it when that changes. A change of wish wakes it.

The agent owns the wish (``homeRouteDesired`` on its document, admins only) and
publishes :meth:`HomeRouteService.report` with its own report. A failure
anywhere here only means a phone at home uses the bucket copy, as it always has.

Modelled on Sarungano's app/home/service.py, the home-route blueprint's
reference implementation.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from clipforge_contracts import HomeRoute, HomeRouteState

from clipforge.config import Settings
from clipforge.home import certificate, media
from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = ["FIREWALL_RULE", "HomeRouteService", "firebase_verifier", "firewall_command"]

FIREWALL_RULE = "ClipForge home wifi"

# Once a day when it worked; sooner after a failure, so a DuckDNS outage costs
# an hour, not a day. Let's Encrypt allows only a few failed validations an
# hour, which is why a failure is not retried at once.
RENEW_EVERY_SECONDS = 24 * 60 * 60
RETRY_AFTER_SECONDS = 60 * 60
# How often the thread looks at the listener in between, to notice one that
# has died.
LOOK_EVERY_SECONDS = 60.0
# The firewall check starts PowerShell, about a second.
FIREWALL_CACHE_SECONDS = 300.0


def firewall_command(port: int) -> str:
    """The one-time admin command: only the home network (Private profile),
    only devices on it, and only this port."""
    return (
        f'New-NetFirewallRule -DisplayName "{FIREWALL_RULE}" -Direction Inbound -Protocol TCP '
        f"-LocalPort {port} -Profile Private -RemoteAddress LocalSubnet -Action Allow"
    )


def firebase_verifier(settings: Settings) -> Callable[[str], dict[str, Any]]:
    """`verify_token` for the listener: firebase-admin's default app, made on
    first use with the same credentials the worker's admin commands use
    (clipforge/store/users.py). Against the emulators, the Auth emulator."""
    lock = threading.Lock()

    def verify(token: str) -> dict[str, Any]:
        import firebase_admin
        from firebase_admin import auth, credentials

        with lock:
            if not firebase_admin._apps:
                options = {"projectId": settings.firebase_project_id}
                if settings.use_emulators:
                    os.environ.setdefault(
                        "FIREBASE_AUTH_EMULATOR_HOST", settings.auth_emulator_host
                    )
                    firebase_admin.initialize_app(options=options)
                elif settings.google_application_credentials:
                    firebase_admin.initialize_app(
                        credentials.Certificate(settings.google_application_credentials),
                        options=options,
                    )
                else:
                    firebase_admin.initialize_app(options=options)
        return dict(auth.verify_id_token(token))

    return verify


def _home_extra_installed() -> bool:
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        return False
    return True


class HomeRouteService:
    """The listener and its certificate, kept as the wish and the disk say."""

    def __init__(
        self,
        settings: Settings,
        *,
        station_id: str,
        version: str,
        media_link: Any,
        on_change: Callable[[], None] = lambda: None,
        verify_token: Callable[[str], dict[str, Any]] | None = None,
        renew: Callable[..., str] = certificate.run,
        listener: Any = None,
        firewall: Callable[[int], bool | None] | None = None,
        installed: Callable[[], bool] = _home_extra_installed,
        has_address: Callable[[str], bool] | None = None,
    ) -> None:
        # renew, listener, firewall, installed and has_address are test seams.
        self._settings = settings
        self._station_id = station_id
        self._version = version
        self._media_link = media_link
        self._on_change = on_change
        self._verify_token = verify_token or firebase_verifier(settings)
        self._renew = renew
        self._listener = listener
        self._firewall_check = firewall or _firewall_rule_exists
        self._installed = installed
        self._has_address = has_address or _has_address
        certificate.configure(str(settings.home_route_dir))

        self._wanted = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        self._next_renew = 0.0
        self._renew_thread: threading.Thread | None = None
        self._renew_error: str | None = None
        # What the running listener was started with, so a change restarts it.
        self._serving: tuple[Any, ...] | None = None
        self._listen_error: str | None = None
        self._unavailable: str | None = None
        self._firewall: tuple[float, bool | None] | None = None
        self._latest: HomeRoute | None = None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    @property
    def url(self) -> str:
        return f"https://{self._settings.home_route_hostname}:{self._settings.home_route_port}"

    def want(self, on: bool) -> None:
        """The agent's wish, from ``homeRouteDesired``."""
        with self._lock:
            changed = on != self._wanted
            self._wanted = on
        if changed:
            log.info("home.wish", on=on)
            self._wake.set()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="home-route", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=15)
        # A renewal in progress is abandoned, not waited for: lego is stopped,
        # and the next start tries again.
        certificate.cancel()
        renewal = self._renew_thread
        if renewal is not None and renewal.is_alive():
            renewal.join(timeout=5)
        self._stop_listener()

    def latest(self) -> HomeRoute:
        """The report as of the last pass, for the agent's tick: reading it
        costs nothing, where :meth:`report` reads the certificate from disk."""
        with self._lock:
            cached = self._latest
        return cached if cached is not None else self.report()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001 - logged; the thread must outlive it
                log.warning("home.step_failed", error=f"{type(exc).__name__}: {exc}")
            self._refresh()
            self._wake.wait(LOOK_EVERY_SECONDS)
            self._wake.clear()
        self._refresh()

    def _refresh(self) -> None:
        try:
            fresh = self.report()
        except Exception as exc:  # noqa: BLE001 - the last good report stands
            log.warning("home.report_failed", error=f"{type(exc).__name__}: {exc}")
            return
        with self._lock:
            changed = fresh != self._latest
            self._latest = fresh
        if changed:
            self._on_change()

    # ── One pass ─────────────────────────────────────────────────────────────

    def step(self) -> None:
        """Bring the listener in line with the wish and the certificate.

        Public so a test can drive it one pass at a time without the thread.
        """
        with self._lock:
            wanted = self._wanted
        if not wanted:
            self._unavailable = None
            self._stop_listener()
            return

        self._unavailable = self._setup_problem()
        if self._unavailable is not None:
            self._stop_listener()
            return

        # Served with the certificate there is; a renewal that brings a new
        # one wakes this thread, and the next pass restarts the listener.
        self._serve()
        busy = self._renew_thread is not None and self._renew_thread.is_alive()
        if not busy and time.monotonic() >= self._next_renew:
            settings = self._settings
            has_certificate = (
                certificate.info(settings.home_route_hostname, settings.home_route_staging)
                is not None
            )
            self._renew_thread = threading.Thread(
                target=self._renew_certificate,
                args=(has_certificate,),
                name="home-route-renew",
                daemon=True,
            )
            self._renew_thread.start()

    def _setup_problem(self) -> str | None:
        s = self._settings
        if not s.home_route_hostname or not s.home_route_address:
            return (
                "Set CLIPFORGE_HOME_ROUTE_HOSTNAME and CLIPFORGE_HOME_ROUTE_ADDRESS in .env, "
                "then restart the agent."
            )
        if not s.duckdns_token:
            return "Set DUCKDNS_TOKEN in .env, then restart the agent."
        if not self._installed():
            return (
                "The worker's `home` extra is not installed. Stop the worker, then run "
                "uv sync --extra gpu --extra media --extra speech --extra home in apps/worker."
            )
        if not self._has_address(s.home_route_address):
            return (
                f"This PC does not have the address {s.home_route_address} right now. "
                "Check the router's reservation, or that the Wi-Fi is connected."
            )
        return None

    def _renew_certificate(self, has_certificate: bool) -> None:
        s = self._settings
        try:
            result = self._renew(
                s.home_route_hostname,
                s.home_route_staging,
                s.duckdns_token,
                # lego's random pause before a routine renewal is what Let's
                # Encrypt asks for; a first certificate has nobody to wait for.
                wait_randomly=has_certificate,
            )
        except Exception as exc:  # noqa: BLE001 - CertificateError and whatever lego threw
            self._renew_error = str(exc)
            self._next_renew = time.monotonic() + RETRY_AFTER_SECONDS
            if not self._stop.is_set():
                log.warning("home.certificate_failed", error=str(exc))
        else:
            self._renew_error = None
            self._next_renew = time.monotonic() + RENEW_EVERY_SECONDS
            log.info("home.certificate", result=result)
        finally:
            self._wake.set()

    def _serve(self) -> None:
        """Keep the listener running with the certificate as it is on disk."""
        if self._stop.is_set():
            return
        s = self._settings
        info = certificate.info(s.home_route_hostname, s.home_route_staging)
        if info is None:
            self._stop_listener()
            return
        certfile, keyfile = certificate.paths(s.home_route_hostname, s.home_route_staging)
        wanted = (s.home_route_address, s.home_route_port, certfile, info["modified"])
        listener = self._get_listener()
        if self._serving == wanted and listener.running:
            return
        self._stop_listener()
        from clipforge.home.listener import build_app, origins_for

        app = build_app(
            station_id=lambda: self._station_id,
            version=self._version,
            allowed_origins=origins_for(s.firebase_project_id),
            base_url=lambda: self.url,
            media_link=self._media_link,
            verify_token=self._verify_token,
            # ClipForge's gate is approval (checked in clips.py), not a
            # confirmed email: an email-and-password account an admin
            # approved is as entitled to its clips as a Google one.
            require_verified_email=False,
        )
        # What a crash left behind goes before anything new is offered.
        media.clear()
        try:
            listener.start(app, s.home_route_address, s.home_route_port, certfile, keyfile)
        except RuntimeError as exc:
            if str(exc) != self._listen_error:
                log.warning("home.listen_failed", error=str(exc))
            self._listen_error = str(exc)
            return
        self._listen_error = None
        self._serving = wanted
        log.info("home.serving", url=self.url, staging=s.home_route_staging)

    def _get_listener(self) -> Any:
        if self._listener is None:
            from clipforge.home.listener import Listener

            self._listener = Listener()
        return self._listener

    def _stop_listener(self) -> None:
        was_serving = self._serving is not None
        if self._listener is not None:
            self._listener.stop()
        self._serving = None
        if was_serving:
            media.clear()  # its links point at it
            log.info("home.stopped")

    # ── What the app sees ────────────────────────────────────────────────────

    def report(self) -> HomeRoute:
        s = self._settings
        with self._lock:
            wanted = self._wanted
        serving = (
            self._serving is not None
            and self._listener is not None
            and bool(self._listener.running)
        )
        info = (
            certificate.info(s.home_route_hostname, s.home_route_staging)
            if s.home_route_hostname
            else None
        )
        renewing = self._renew_thread is not None and self._renew_thread.is_alive()

        if not wanted:
            state, detail = HomeRouteState.OFF, None
        elif self._unavailable is not None:
            state, detail = HomeRouteState.UNAVAILABLE, self._unavailable
        elif serving:
            state = HomeRouteState.SERVING
            detail = (
                "A staging certificate: phones will not trust it, so it is not offered to them."
                if s.home_route_staging
                else None
            )
        elif self._listen_error is not None:
            state, detail = HomeRouteState.FAILED, self._listen_error
        elif info is None and renewing:
            state, detail = HomeRouteState.CERTIFICATE, "Getting the certificate."
        elif info is None and self._renew_error is not None:
            state, detail = HomeRouteState.FAILED, self._renew_error
        else:
            state, detail = HomeRouteState.CERTIFICATE, "Starting."

        return HomeRoute(
            state=state,
            # The one field a phone acts on: only a listener that is up, with a
            # certificate a phone will trust.
            url=self.url if serving and not s.home_route_staging else None,
            detail=detail,
            hostname=s.home_route_hostname or None,
            address=s.home_route_address or None,
            port=s.home_route_port,
            staging=s.home_route_staging,
            token_set=bool(s.duckdns_token),
            certificate_expires_at=(
                datetime.fromisoformat(info["expiresAt"]) if info is not None else None
            ),
            firewall_rule=self._firewall_rule() if wanted else None,
            firewall_command=firewall_command(s.home_route_port),
        )

    def _firewall_rule(self) -> bool | None:
        cached = self._firewall
        if cached is not None and time.monotonic() - cached[0] < FIREWALL_CACHE_SECONDS:
            return cached[1]
        found = self._firewall_check(self._settings.home_route_port)
        self._firewall = (time.monotonic(), found)
        return found


def _has_address(address: str) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((address, 0))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _firewall_rule_exists(port: int) -> bool | None:
    """Whether an enabled inbound Allow rule by our name opens this port.
    Reading rules needs no admin rights; adding one does."""
    if sys.platform != "win32":
        return None
    script = (
        f"$r = Get-NetFirewallRule -DisplayName '{FIREWALL_RULE}' -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Enabled -eq 'True' -and $_.Direction -eq 'Inbound' "
        "-and $_.Action -eq 'Allow' }; "
        "@{ found = [bool]$r; ports = @($r | Get-NetFirewallPortFilter | "
        "ForEach-Object { [string]$_.LocalPort }) } | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(  # noqa: S603 - a fixed script; the only value in it is our own port
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        parsed = json.loads(completed.stdout or "{}") if completed.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    ports = [str(p) for p in (parsed.get("ports") or [])]
    return bool(parsed.get("found")) and (str(port) in ports or "Any" in ports)
