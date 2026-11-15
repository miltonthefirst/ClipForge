"""The certificate for the home name, from Let's Encrypt, through lego.

Browsers will not let the phone app (a secure page) call a plain http://
address, so the listener needs a real certificate for a real name. Each
renewal proves the name is ours with a code published in DNS. Squarespace
(where these projects' domains live) has no DNS API, so one CNAME there hands
that single check to DuckDNS, which lego updates with the token in .env
(the home-route blueprint, section 2; docs/HOME-NETWORK-PLAN.md).

lego is one Windows program. It is downloaded on first use, pinned to one
version and checked against that release's published SHA-256. It, its account
and the certificates all live under Settings.home_route_dir
(~/.clipforge/home), outside the repository: the keys there are private.

lego itself decides when a renewal is due (a third of the lifetime left,
or what Let's Encrypt suggests), so this module simply runs it once a day
and reads back whether anything changed.
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import threading
import zipfile
from collections.abc import Callable
from datetime import timezone
from typing import Any

LEGO_VERSION = "5.5.2"
LEGO_URL = f"https://github.com/go-acme/lego/releases/download/v{LEGO_VERSION}/lego_v{LEGO_VERSION}_windows_amd64.zip"
# From lego_5.5.2_checksums.txt on the same release.
LEGO_SHA256 = "fc544130a2716329cad73a47b674e3aeea019c4753c3963d259bd45d946bbd34"

# Set from Settings.home_route_dir by configure(); these are the defaults.
LEGO_DIR = os.path.expanduser(os.path.join("~", ".clipforge", "home", "lego"))
DATA_DIR = os.path.expanduser(os.path.join("~", ".clipforge", "home", "certificates"))


def configure(root: str) -> None:
    """Where lego and the certificates live (Settings.home_route_dir)."""
    global LEGO_DIR, DATA_DIR
    base = os.path.expanduser(str(root))
    LEGO_DIR = os.path.join(base, "lego")
    DATA_DIR = os.path.join(base, "certificates")


# Without these lego checks the code through the system's resolvers or,
# failing that, Cloudflare's over IPv6, which this home network does not
# have (seen 2026-11-01: "connect: A socket operation was attempted to an
# unreachable network").
RESOLVERS = "1.1.1.1:53,8.8.8.8:53"

# A run that has to wait for DNS can take a few minutes; lego's own random
# pause before an automatic renewal adds up to eight more.
RUN_TIMEOUT_SECONDS = 20 * 60

_lock = threading.Lock()
# The lego run in progress, if any, so cancel() can stop it.
_process: subprocess.Popen[str] | None = None


class CertificateError(RuntimeError):
    """Getting or renewing the certificate failed. The message is shown on
    the Server page, so it ends with what lego said."""


def lego_path() -> str:
    return os.path.join(LEGO_DIR, "lego.exe")


def ensure_lego(download: Callable[[str], bytes] | None = None) -> str:
    """lego.exe at the pinned version, downloaded and checked if it is not
    there yet. `download` (url -> bytes) is the test seam."""
    marker = os.path.join(LEGO_DIR, "VERSION")
    try:
        with open(marker, "r", encoding="utf-8") as handle:
            if handle.read().strip() == LEGO_VERSION and os.path.isfile(lego_path()):
                return lego_path()
    except OSError:
        pass

    archive = (download or _download)(LEGO_URL)
    digest = hashlib.sha256(archive).hexdigest()
    if digest != LEGO_SHA256:
        raise CertificateError(
            f"The lego download did not match its published checksum (got {digest}), so it was not used."
        )
    os.makedirs(LEGO_DIR, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        binary = bundle.read("lego.exe")
    temporary = lego_path() + ".tmp"
    with open(temporary, "wb") as handle:
        handle.write(binary)
    os.replace(temporary, lego_path())
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write(LEGO_VERSION)
    return lego_path()


def _download(url: str) -> bytes:
    import requests

    response = requests.get(url, timeout=120)
    response.raise_for_status()
    return response.content


def data_dir(staging: bool) -> str:
    """Staging and real certificates are kept apart, so trying staging
    never overwrites a working certificate."""
    return os.path.join(DATA_DIR, "staging" if staging else "production")


def paths(hostname: str, staging: bool) -> tuple[str, str]:
    """The certificate (with its chain) and its key, where lego writes them."""
    folder = os.path.join(data_dir(staging), "certificates")
    return os.path.join(folder, f"{hostname}.crt"), os.path.join(folder, f"{hostname}.key")


def run(
    hostname: str,
    staging: bool,
    token: str | None,
    *,
    wait_randomly: bool = True,
    runner: Callable[[list[str], dict[str, str]], subprocess.CompletedProcess[str]] | None = None,
) -> str:
    """Gets the certificate, or renews it if lego says it is due. Answers
    "renewed" when files changed and "current" when nothing was due.

    `wait_randomly` keeps lego's pause before an automatic renewal, which
    Let's Encrypt asks for so that renewals don't all land at once. A
    renewal someone pressed a button for skips it."""
    if not token:
        raise CertificateError(
            "DUCKDNS_TOKEN is not set in .env, so the certificate cannot be renewed."
        )
    with _lock:
        lego = ensure_lego()
        command = [
            lego,
            "run",
            "--accept-tos",
            "--server",
            "letsencrypt-staging" if staging else "letsencrypt",
            "--dns",
            "duckdns",
            "--dns.resolvers",
            RESOLVERS,
            "--domains",
            hostname,
            "--path",
            data_dir(staging),
        ]
        if not wait_randomly:
            command.append("--no-random-sleep")
        environment = {**os.environ, "DUCKDNS_TOKEN": token, "LEGO_LOG_FORMAT": "text"}
        try:
            completed = (runner or _run)(command, environment)
        except subprocess.TimeoutExpired as exc:
            raise CertificateError(
                f"lego took longer than {RUN_TIMEOUT_SECONDS // 60} minutes and was stopped."
            ) from exc
        output = (completed.stdout or "") + (completed.stderr or "")
        if completed.returncode != 0:
            raise CertificateError(f"lego could not get the certificate: {_last_error(output)}")
        return "current" if "Skip renewal" in output else "renewed"


def _run(command: list[str], environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    global _process
    process = subprocess.Popen(  # noqa: S603 - argv built here: the pinned lego and our settings
        command,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
    )
    _process = process
    try:
        output, _ = process.communicate(timeout=RUN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise
    finally:
        _process = None
    return subprocess.CompletedProcess(command, process.returncode, output, "")


def cancel() -> None:
    """Stops a lego run in progress, for a server that is shutting down:
    left alone it would outlive the process. The run then fails, and the
    next start tries again."""
    process = _process
    if process is not None and process.poll() is None:
        process.kill()


def _last_error(output: str) -> str:
    """lego's own words: the ERROR line if there is one, else its last line."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        if "level=ERROR" in line:
            return line.split("msg=", 1)[-1][:600]
    return lines[-1][:600] if lines else "no output"


def info(hostname: str, staging: bool) -> dict[str, Any] | None:
    """What the Server page shows about the certificate, or None when
    there isn't one yet."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID

    certificate_path, key_path = paths(hostname, staging)
    if not (os.path.isfile(certificate_path) and os.path.isfile(key_path)):
        return None
    try:
        with open(certificate_path, "rb") as handle:
            # The file is the certificate followed by its chain; the first is ours.
            certificate = x509.load_pem_x509_certificate(handle.read())
    except (OSError, ValueError):
        return None
    issuer = certificate.issuer.get_attributes_for_oid(
        NameOID.ORGANIZATION_NAME
    ) or certificate.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
    return {
        "expiresAt": certificate.not_valid_after_utc.astimezone(timezone.utc).isoformat(),
        "issuedAt": certificate.not_valid_before_utc.astimezone(timezone.utc).isoformat(),
        "issuer": issuer[0].value if issuer else None,
        "staging": staging,
        "modified": os.path.getmtime(certificate_path),
    }
