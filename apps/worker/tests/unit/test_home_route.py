"""The home wifi route: a phone at home plays clips straight off this PC.

The cases follow the home-route blueprint's checklist (section 10) and then
ClipForge's own rules: approval rather than a confirmed email, only files under
the worker's workspace/clips, and the agent as the route's only owner.

Nothing here touches the network, lego, DNS, the firewall or Firebase: every
one of them sits behind a seam, and what is under test is the decisions.
"""

from __future__ import annotations

import hashlib
import io
import subprocess
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from clipforge.agent import Agent, home_route_wanted
from clipforge.config import Settings
from clipforge.home import certificate, media
from clipforge.home.auth import Caller, NotSignedIn, verify
from clipforge.home.clips import APPROVAL_TTL_SECONDS, WorkspaceMedia, worker_workspace
from clipforge.home.service import HomeRouteService, firewall_command
from clipforge_contracts import AgentReport, HomeRoute, HomeRouteState

pytestmark = pytest.mark.unit

pytest.importorskip("fastapi")
from clipforge.home.listener import SERVICE, build_app, origins_for  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

HOST = "clipforge-home.example.dev"
ADDRESS = "192.168.100.13"
ORIGIN = "https://demo-clipforge.web.app"
HOME_PHONE = ("192.168.100.20", 50000)


def settings_for(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "use_emulators": True,
        "firebase_project_id": "demo-clipforge",
        "home_route_hostname": HOST,
        "home_route_address": ADDRESS,
        "home_route_port": 8768,
        "home_route_dir": tmp_path / "home",
        "workspace_dir": tmp_path / "workspace",
        "DUCKDNS_TOKEN": "not-a-real-token",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(autouse=True)
def media_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    folder = tmp_path / "media-copies"
    monkeypatch.setattr(media, "CLIP_DIR", str(folder))
    yield folder
    media.clear()


def claims(uid: str = "alice", *, verified: bool = False) -> dict[str, Any]:
    return {"uid": uid, "email": f"{uid}@example.com", "email_verified": verified}


# ── Sign-in ──────────────────────────────────────────────────────────────────


def test_an_approved_account_needs_no_confirmed_email_here() -> None:
    caller = verify("Bearer t", lambda _t: claims(), require_verified_email=False)
    assert caller == Caller(uid="alice", email="alice@example.com")


def test_the_blueprint_default_still_demands_a_confirmed_email() -> None:
    with pytest.raises(NotSignedIn):
        verify("Bearer t", lambda _t: claims())


@pytest.mark.parametrize("header", [None, "", "Bearer ", "Basic abc"])
def test_no_token_means_no_entry(header: str | None) -> None:
    with pytest.raises(NotSignedIn):
        verify(header, lambda _t: claims(), require_verified_email=False)


def test_a_token_that_does_not_check_out_is_refused() -> None:
    def bad(_token: str) -> dict[str, Any]:
        raise ValueError("expired")

    with pytest.raises(NotSignedIn, match="could not be checked"):
        verify("Bearer t", bad, require_verified_email=False)


# ── Which file "clip X" means ────────────────────────────────────────────────


def a_clip(tmp_path: Path, name: str = "abc.mp4") -> Path:
    folder = tmp_path / "workspace" / "clips" / "job1"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"\x00" * 4096)
    return path


def clip_media(
    tmp_path: Path,
    paths: dict[str, str],
    *,
    approved: set[str] | None = None,
    asked: list[str] | None = None,
    clock: list[float] | None = None,
) -> WorkspaceMedia:
    allowed = {"alice"} if approved is None else approved
    ticks = clock if clock is not None else [0.0]

    def is_approved(uid: str) -> bool:
        if asked is not None:
            asked.append(uid)
        return uid in allowed

    return WorkspaceMedia(
        settings_for(tmp_path),
        approved=is_approved,
        local_path=lambda _kind, item: paths.get(item),
        clock=lambda: ticks[0],
    )


def test_a_clip_resolves_to_its_own_file(tmp_path: Path) -> None:
    path = a_clip(tmp_path)
    resolve = clip_media(tmp_path, {"c1": str(path)})

    found, content_type = resolve("clip", "c1", Caller("alice", None))

    assert Path(found) == path.resolve()
    assert content_type == "video/mp4"


def test_a_path_recorded_with_backslashes_still_resolves(tmp_path: Path) -> None:
    path = a_clip(tmp_path)
    resolve = clip_media(tmp_path, {"c1": str(path).replace("/", "\\")})

    found, _ = resolve("clip", "c1", Caller("alice", None))

    assert Path(found) == path.resolve()


def test_only_clips_and_tracks_are_offered(tmp_path: Path) -> None:
    resolve = clip_media(tmp_path, {})
    with pytest.raises(LookupError):
        resolve("source", "s1", Caller("alice", None))


def a_track(tmp_path: Path, name: str = "trk_abc.wav") -> Path:
    folder = tmp_path / "workspace" / "music" / "generated"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"RIFF" + b"\x00" * 2048)
    return path


def test_a_track_resolves_to_its_audio_under_music(tmp_path: Path) -> None:
    path = a_track(tmp_path)
    resolve = clip_media(tmp_path, {"t1": str(path)})

    found, content_type = resolve("track", "t1", Caller("alice", None))

    assert Path(found) == path.resolve()
    assert content_type == "audio/wav"


def test_each_kind_is_served_only_from_its_own_folder(tmp_path: Path) -> None:
    """A clip's record cannot point at music, nor a track's at the clips."""
    clip = a_clip(tmp_path)
    track = a_track(tmp_path)
    resolve = clip_media(tmp_path, {"clip-as-track": str(clip), "track-as-clip": str(track)})

    with pytest.raises(LookupError):
        resolve("track", "clip-as-track", Caller("alice", None))
    with pytest.raises(LookupError):
        resolve("clip", "track-as-clip", Caller("alice", None))


def test_an_account_that_is_not_approved_is_refused_before_any_lookup(tmp_path: Path) -> None:
    path = a_clip(tmp_path)
    resolve = clip_media(tmp_path, {"c1": str(path)}, approved=set())
    with pytest.raises(PermissionError):
        resolve("clip", "c1", Caller("mallory", None))


def test_approval_is_read_once_a_minute_per_account(tmp_path: Path) -> None:
    path = a_clip(tmp_path)
    asked: list[str] = []
    clock = [0.0]
    resolve = clip_media(tmp_path, {"c1": str(path)}, asked=asked, clock=clock)

    for _ in range(20):
        resolve("clip", "c1", Caller("alice", None))
    clock[0] = APPROVAL_TTL_SECONDS + 1
    resolve("clip", "c1", Caller("alice", None))

    assert asked == ["alice", "alice"]


def test_a_file_outside_the_clips_folder_is_never_served(tmp_path: Path) -> None:
    source = tmp_path / "workspace" / "sources" / "raw.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"\x00")
    sneaky = tmp_path / "workspace" / "clips" / ".." / "sources" / "raw.mp4"
    resolve = clip_media(tmp_path, {"src": str(source), "dots": str(sneaky)})

    for clip_id in ("src", "dots"):
        with pytest.raises(LookupError):
            resolve("clip", clip_id, Caller("alice", None))


def test_only_video_files_are_served(tmp_path: Path) -> None:
    transcript = a_clip(tmp_path, "abc.json")
    resolve = clip_media(tmp_path, {"c1": str(transcript)})
    with pytest.raises(LookupError):
        resolve("clip", "c1", Caller("alice", None))


def test_a_clip_whose_file_has_gone_says_so(tmp_path: Path) -> None:
    resolve = clip_media(tmp_path, {"c1": str(tmp_path / "workspace" / "clips" / "gone.mp4")})
    with pytest.raises(LookupError, match="not on this PC"):
        resolve("clip", "c1", Caller("alice", None))


def test_a_relative_workspace_is_the_workers_not_the_agents() -> None:
    """The agent runs from the repository root, where ./workspace is another
    folder; the worker runs from apps/worker."""
    resolved = worker_workspace(Settings(workspace_dir=Path("./workspace")))
    assert resolved.parts[-3:] == ("apps", "worker", "workspace")


# ── The listener ─────────────────────────────────────────────────────────────


def client_for(
    tmp_path: Path,
    paths: dict[str, str],
    *,
    caller: tuple[str, int] = HOME_PHONE,
    approved: set[str] | None = None,
) -> TestClient:
    app = build_app(
        lambda: "tower",
        "0.0.3",
        allowed_origins=origins_for("demo-clipforge"),
        base_url=lambda: f"https://{HOST}:8768",
        media_link=clip_media(tmp_path, paths, approved=approved),
        verify_token=lambda token: claims(token),
        require_verified_email=False,
    )
    return TestClient(app, client=caller)


def signed(uid: str = "alice") -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


def test_hello_says_which_machine_this_is(tmp_path: Path) -> None:
    answer = client_for(tmp_path, {}).get("/hello").json()
    assert answer == {"service": SERVICE, "station": "tower", "version": "0.0.3"}


def test_nothing_answers_a_public_address(tmp_path: Path) -> None:
    outside = client_for(tmp_path, {}, caller=("8.8.8.8", 5000))
    assert outside.get("/hello").status_code == 403


def test_a_clip_link_plays_from_the_listener_and_seeks(tmp_path: Path) -> None:
    path = a_clip(tmp_path)
    client = client_for(tmp_path, {"c1": str(path)})

    answer = client.post("/media-links", json={"kind": "clip", "id": "c1"}, headers=signed())
    assert answer.status_code == 200
    url = answer.json()["url"]
    assert url.startswith(f"https://{HOST}:8768/media/")

    played = client.get(url.split(":8768")[1], headers={"Range": "bytes=0-99"})
    assert played.status_code == 206
    assert len(played.content) == 100


def test_the_same_clip_asked_for_again_is_the_same_link(tmp_path: Path) -> None:
    path = a_clip(tmp_path)
    client = client_for(tmp_path, {"c1": str(path)})
    body = {"kind": "clip", "id": "c1"}

    first = client.post("/media-links", json=body, headers=signed()).json()["url"]
    second = client.post("/media-links", json=body, headers=signed()).json()["url"]

    assert first == second


@pytest.mark.parametrize(
    ("headers", "body", "status"),
    [
        ({}, {"kind": "clip", "id": "c1"}, 401),
        (signed("mallory"), {"kind": "clip", "id": "c1"}, 403),
        (signed(), {"kind": "clip", "id": "missing"}, 404),
        (signed(), {"kind": "clip", "id": "../../etc"}, 422),
        (signed(), {"kind": "CLIP!", "id": "c1"}, 422),
    ],
)
def test_every_refusal_says_why(
    tmp_path: Path, headers: dict[str, str], body: dict[str, str], status: int
) -> None:
    path = a_clip(tmp_path)
    client = client_for(tmp_path, {"c1": str(path)})
    assert client.post("/media-links", json=body, headers=headers).status_code == status


def test_an_unsigned_request_is_turned_away_before_its_body_is_read(tmp_path: Path) -> None:
    client = client_for(tmp_path, {})
    answer = client.post("/media-links", content=b"not json at all")
    assert answer.status_code == 401


def test_an_oversized_request_is_refused_unread(tmp_path: Path) -> None:
    client = client_for(tmp_path, {})
    answer = client.post("/media-links", content=b"x" * (3 * 1024 * 1024), headers=signed())
    assert answer.status_code == 413


def test_there_is_no_command_route(tmp_path: Path) -> None:
    client = client_for(tmp_path, {})
    assert client.post("/commands", json={}, headers=signed()).status_code in (404, 405)


def test_an_expired_link_plays_nothing(tmp_path: Path) -> None:
    path = a_clip(tmp_path)
    url, _ = media.offer(
        f"https://{HOST}:8768",
        "video/mp4",
        path=str(path),
        now=datetime.now(UTC) - timedelta(hours=media.LINK_HOURS + 1),
    )
    client = client_for(tmp_path, {})
    assert client.get(url.split(":8768")[1]).status_code == 404


def test_the_apps_preflight_gets_the_private_network_yes(tmp_path: Path) -> None:
    answer = client_for(tmp_path, {}).options(
        "/media-links",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
            "Access-Control-Request-Private-Network": "true",
        },
    )
    assert answer.status_code == 200
    assert answer.headers.get("access-control-allow-origin") == ORIGIN
    assert answer.headers.get("access-control-allow-private-network") == "true"


def test_another_website_gets_no_cors_yes(tmp_path: Path) -> None:
    answer = client_for(tmp_path, {}).get("/hello", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in answer.headers


def test_a_clip_being_played_can_still_be_replaced(tmp_path: Path) -> None:
    """On Windows a file being played is held open: the link serves a copy, so
    a remake replacing the clip does not fail."""
    path = a_clip(tmp_path)
    url, _ = media.offer(f"https://{HOST}:8768", "video/mp4", path=str(path))
    found = media.find(url.rsplit("/", 1)[1])
    assert found is not None
    assert Path(found.path) != path

    with Path(found.path).open("rb"):
        replacement = path.with_suffix(".new")
        replacement.write_bytes(b"\x01" * 10)
        replacement.replace(path)

    assert path.read_bytes() == b"\x01" * 10


# ── The certificate ──────────────────────────────────────────────────────────


@pytest.fixture
def lego_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    certificate.configure(str(tmp_path / "home"))
    lego = Path(certificate.LEGO_DIR)
    lego.mkdir(parents=True)
    (lego / "lego.exe").write_bytes(b"MZ")
    (lego / "VERSION").write_text(certificate.LEGO_VERSION, encoding="utf-8")
    return lego


def test_lego_is_downloaded_once_and_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    certificate.configure(str(tmp_path / "home"))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("lego.exe", b"MZ-lego")
    data = archive.getvalue()
    monkeypatch.setattr(certificate, "LEGO_SHA256", hashlib.sha256(data).hexdigest())
    downloads: list[str] = []

    def download(url: str) -> bytes:
        downloads.append(url)
        return data

    first = certificate.ensure_lego(download)
    second = certificate.ensure_lego(download)

    assert first == second
    assert Path(first).read_bytes() == b"MZ-lego"
    assert len(downloads) == 1


def test_a_download_that_does_not_match_its_checksum_is_not_used(tmp_path: Path) -> None:
    certificate.configure(str(tmp_path / "home"))
    with pytest.raises(certificate.CertificateError, match="checksum"):
        certificate.ensure_lego(lambda _url: b"tampered")
    assert not Path(certificate.lego_path()).exists()


def test_lego_runs_with_the_token_and_our_resolvers(lego_installed: Path) -> None:
    seen: dict[str, Any] = {}

    def runner(command: list[str], environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
        seen["command"], seen["environment"] = command, environment
        return subprocess.CompletedProcess(command, 0, "obtained", "")

    assert certificate.run(HOST, False, "tok", runner=runner) == "renewed"
    command = seen["command"]
    assert command[command.index("--dns") + 1] == "duckdns"
    assert command[command.index("--dns.resolvers") + 1] == certificate.RESOLVERS
    assert command[command.index("--server") + 1] == "letsencrypt"
    assert seen["environment"]["DUCKDNS_TOKEN"] == "tok"
    # Never on the command line, where any process can read it.
    assert "tok" not in command


def test_staging_goes_to_its_own_server_and_folder(lego_installed: Path) -> None:
    seen: list[list[str]] = []

    def runner(command: list[str], _env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    certificate.run(HOST, True, "tok", runner=runner)
    command = seen[0]
    assert command[command.index("--server") + 1] == "letsencrypt-staging"
    assert command[command.index("--path") + 1].endswith("staging")


def test_nothing_due_is_reported_as_current(lego_installed: Path) -> None:
    def runner(command: list[str], _env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "Skip renewal: not due", "")

    assert certificate.run(HOST, False, "tok", runner=runner) == "current"


def test_a_failure_says_what_lego_said(lego_installed: Path) -> None:
    def runner(command: list[str], _env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 1, 'level=ERROR msg="duckdns: TXT update failed"', ""
        )

    with pytest.raises(certificate.CertificateError, match="TXT update failed"):
        certificate.run(HOST, False, "tok", runner=runner)


def test_without_a_token_lego_is_not_even_started(lego_installed: Path) -> None:
    def runner(command: list[str], _env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        raise AssertionError("lego was started")

    with pytest.raises(certificate.CertificateError, match="DUCKDNS_TOKEN"):
        certificate.run(HOST, False, "", runner=runner)


def write_certificate(tmp_path: Path, *, staging: bool = False, days: int = 90) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    certificate.configure(str(tmp_path / "home"))
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)])
    issuer = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Let's Encrypt")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=days))
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = certificate.paths(HOST, staging)
    Path(cert_path).parent.mkdir(parents=True, exist_ok=True)
    Path(cert_path).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    Path(key_path).write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


def test_the_certificate_is_read_back_for_the_app(tmp_path: Path) -> None:
    write_certificate(tmp_path, days=60)
    info = certificate.info(HOST, False)
    assert info is not None
    assert info["issuer"] == "Let's Encrypt"
    expires = datetime.fromisoformat(info["expiresAt"])
    assert timedelta(days=59) < expires - datetime.now(UTC) < timedelta(days=61)


# ── The service ──────────────────────────────────────────────────────────────


class FakeListener:
    def __init__(self, *, fail: str | None = None) -> None:
        self.running = False
        self.started: list[tuple[Any, ...]] = []
        self.stops = 0
        self._fail = fail

    def start(self, app: Any, host: str, port: int, certfile: str, keyfile: str) -> None:
        if self._fail:
            raise RuntimeError(self._fail)
        self.started.append((host, port, certfile, keyfile))
        self.running = True

    def stop(self) -> None:
        self.stops += 1
        self.running = False


class Renewals:
    def __init__(self, tmp_path: Path, *, fail: str | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._tmp = tmp_path
        self._fail = fail

    def __call__(self, hostname: str, staging: bool, token: str, *, wait_randomly: bool) -> str:
        self.calls.append(
            {"hostname": hostname, "staging": staging, "token": token, "wait": wait_randomly}
        )
        if self._fail:
            raise certificate.CertificateError(self._fail)
        write_certificate(self._tmp, staging=staging)
        return "renewed"


def service_for(
    tmp_path: Path,
    *,
    listener: FakeListener | None = None,
    renew: Renewals | None = None,
    installed: bool = True,
    has_address: bool = True,
    **overrides: Any,
) -> HomeRouteService:
    return HomeRouteService(
        settings_for(tmp_path, **overrides),
        station_id="tower",
        version="0.0.3",
        media_link=clip_media(tmp_path, {}),
        verify_token=lambda token: claims(token),
        renew=renew or Renewals(tmp_path),
        listener=listener or FakeListener(),
        firewall=lambda _port: True,
        installed=lambda: installed,
        has_address=lambda _address: has_address,
    )


def settle(service: HomeRouteService) -> None:
    """One pass, then wait for a renewal it started, then one more pass."""
    service.step()
    thread = service._renew_thread
    if thread is not None:
        thread.join(timeout=10)
    service.step()


def test_switched_off_it_does_nothing(tmp_path: Path) -> None:
    listener, renew = FakeListener(), Renewals(tmp_path)
    service = service_for(tmp_path, listener=listener, renew=renew)

    settle(service)

    assert service.report().state == HomeRouteState.OFF
    assert renew.calls == []
    assert listener.started == []


def test_switching_on_gets_the_certificate_at_once_then_listens(tmp_path: Path) -> None:
    listener, renew = FakeListener(), Renewals(tmp_path)
    service = service_for(tmp_path, listener=listener, renew=renew)
    service.want(True)

    settle(service)

    assert renew.calls[0]["wait"] is False  # a first certificate waits for nobody
    assert listener.started[0][:2] == (ADDRESS, 8768)
    report = service.report()
    assert report.state == HomeRouteState.SERVING
    assert report.url == f"https://{HOST}:8768"
    assert report.certificate_expires_at is not None


def test_a_staging_certificate_is_never_offered_to_phones(tmp_path: Path) -> None:
    service = service_for(tmp_path, home_route_staging=True)
    service.want(True)

    settle(service)

    report = service.report()
    assert report.state == HomeRouteState.SERVING
    assert report.url is None


@pytest.mark.parametrize(
    ("overrides", "installed", "has_address", "says"),
    [
        ({"home_route_hostname": ""}, True, True, "CLIPFORGE_HOME_ROUTE_HOSTNAME"),
        ({"DUCKDNS_TOKEN": ""}, True, True, "DUCKDNS_TOKEN"),
        ({}, False, True, "--extra home"),
        ({}, True, False, "does not have the address"),
    ],
)
def test_a_machine_not_set_up_for_it_says_what_is_missing(
    tmp_path: Path, overrides: dict[str, Any], installed: bool, has_address: bool, says: str
) -> None:
    renew = Renewals(tmp_path)
    service = service_for(
        tmp_path, renew=renew, installed=installed, has_address=has_address, **overrides
    )
    service.want(True)

    settle(service)

    report = service.report()
    assert report.state == HomeRouteState.UNAVAILABLE
    assert says in (report.detail or "")
    assert report.url is None
    assert renew.calls == []


def test_a_listener_that_cannot_bind_says_why_and_is_not_offered(tmp_path: Path) -> None:
    service = service_for(tmp_path, listener=FakeListener(fail="Port 8768 is already in use."))
    service.want(True)

    settle(service)

    report = service.report()
    assert report.state == HomeRouteState.FAILED
    assert report.detail == "Port 8768 is already in use."
    assert report.url is None


def test_a_failed_first_certificate_says_what_lego_said(tmp_path: Path) -> None:
    service = service_for(tmp_path, renew=Renewals(tmp_path, fail="duckdns: unauthorised"))
    service.want(True)

    settle(service)

    report = service.report()
    assert report.state == HomeRouteState.FAILED
    assert "duckdns: unauthorised" in (report.detail or "")


def test_a_failed_renewal_is_not_retried_within_the_hour(tmp_path: Path) -> None:
    renew = Renewals(tmp_path, fail="rate limited")
    service = service_for(tmp_path, renew=renew)
    service.want(True)

    for _ in range(5):
        settle(service)

    assert len(renew.calls) == 1


def test_switching_off_stops_the_listener_and_withdraws_the_url(tmp_path: Path) -> None:
    listener = FakeListener()
    service = service_for(tmp_path, listener=listener)
    service.want(True)
    settle(service)

    service.want(False)
    service.step()

    assert listener.running is False
    report = service.report()
    assert report.state == HomeRouteState.OFF
    assert report.url is None


def test_the_token_is_reported_only_as_whether_it_is_set(tmp_path: Path) -> None:
    service = service_for(tmp_path)
    service.want(True)
    settle(service)

    report = service.report()
    dumped = report.model_dump_json()
    assert report.token_set is True
    assert "not-a-real-token" not in dumped


def test_the_report_carries_the_firewall_command_for_this_port(tmp_path: Path) -> None:
    report = service_for(tmp_path).report()
    assert report.firewall_command == firewall_command(8768)
    assert "-LocalPort 8768" in (report.firewall_command or "")
    assert "-Profile Private" in (report.firewall_command or "")


# ── The agent owns the wish and publishes the report ─────────────────────────


def test_only_on_switches_it_on() -> None:
    assert home_route_wanted({"homeRouteDesired": "ON"}) is True
    for document in (None, {}, {"homeRouteDesired": "OFF"}, {"homeRouteDesired": "on"}):
        assert home_route_wanted(document) is False


class FakeHome:
    def __init__(self) -> None:
        self.wanted: list[bool] = []
        self.current = HomeRoute(state=HomeRouteState.OFF)
        self.started = self.stopped = False

    def want(self, on: bool) -> None:
        self.wanted.append(on)

    def latest(self) -> HomeRoute:
        return self.current

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def test_the_agent_passes_the_wish_on_and_publishes_what_changes() -> None:
    from tests.unit.test_agent import FakeAgentStore, FakeProcess, supervisor

    store = FakeAgentStore({"desired": "STOPPED"})
    home = FakeHome()
    agent = Agent(
        settings=Settings(use_emulators=True),
        store=store,  # type: ignore[arg-type]
        supervisor=supervisor(FakeProcess()),
        listener_check_seconds=3600.0,
        relay=lambda _token: True,
        home=home,  # type: ignore[arg-type]
    )
    agent._claim()
    agent._subscribe()
    start = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
    agent.step(start)
    assert store.deliver is not None
    store.deliver({"desired": "STOPPED", "homeRouteDesired": "ON"})
    assert home.wanted[-1] is True

    agent.step(start + timedelta(seconds=2))
    written = len(store.reports)
    home.current = HomeRoute(state=HomeRouteState.SERVING, url=f"https://{HOST}:8768")
    agent.step(start + timedelta(seconds=4))

    assert len(store.reports) == written + 1
    latest: AgentReport = store.reports[-1]
    assert latest.home is not None
    assert latest.home.url == f"https://{HOST}:8768"


def test_the_store_never_writes_the_admins_wish() -> None:
    from clipforge.store.firestore import AgentStore

    written: list[dict[str, Any]] = []

    class Ref:
        def set(self, document: dict[str, Any], merge: bool = False) -> None:
            written.append(document)

    class Collection:
        def document(self, _id: str) -> Ref:
            return Ref()

    class Client:
        def collection(self, _name: str) -> Collection:
            return Collection()

    store = AgentStore(Client(), Settings(use_emulators=True))  # type: ignore[arg-type]
    report = AgentReport(
        agent_id="tower",
        version="0.0.3",
        desired="STOPPED",
        state="STOPPED",
        last_seen_at=datetime.now(UTC),
        home=HomeRoute(state=HomeRouteState.OFF),
    )
    store.publish(report, claim=True)

    assert "home" in written[0]
    for field in ("homeRouteDesired", "homeRouteRequestedBy", "homeRouteRequestedAt"):
        assert field not in written[0]
