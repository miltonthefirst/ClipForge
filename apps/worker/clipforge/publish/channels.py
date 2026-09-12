"""The routes the desktop app calls to configure a publishing channel.

Split from :mod:`clipforge.localapi`, which owns transport and trust and knows
nothing about YouTube. This knows about YouTube and nothing about HTTP, so each
can be read — and tested — without the other.

The division that matters runs through the middle of this file: the **secret**
half (client id, client secret, refresh token) is written to disk on this
machine, and the **describable** half (which channel, its state, its defaults,
how much quota is left) is written to Firestore where the app can read it from
anywhere. Nothing that could publish to a channel ever crosses that line.
"""

from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clipforge_contracts import (
    Channel,
    ChannelConnection,
    PublishDefaults,
    PublishPlatform,
    PublishPrivacy,
)

from clipforge.config import Settings
from clipforge.observability import get_logger
from clipforge.publish.credentials import CredentialError, TokenStore
from clipforge.store.channels import ChannelStore

log = get_logger(__name__)

__all__ = ["DEFAULT_CHANNEL_ID", "ChannelRoutes"]

# The single channel the UI manages today. A constant rather than a hardcoded
# assumption scattered about: when a second channel arrives this becomes "the
# one marked isDefault", and there is exactly one place to change.
DEFAULT_CHANNEL_ID = "youtube-primary"

# How long the loopback listener waits for the browser to come back. Long enough
# to pick an account and read the unverified-app warning; short enough that a
# window closed halfway through frees the port rather than holding it until the
# worker restarts.
AUTHORISE_TIMEOUT_S = 180.0


@dataclass
class _AuthAttempt:
    """One in-flight authorisation, so the app can ask how it went.

    The OAuth dance cannot answer within the request that starts it — it waits
    on a human in a browser. So the request returns immediately and this carries
    the outcome to whichever `/status` poll asks next.
    """

    started_at: datetime
    url: str
    error: str | None = None
    done: bool = False


class ChannelRoutes:
    """Read and change a channel's configuration, from the machine only."""

    def __init__(self, settings: Settings, channels: ChannelStore | None = None) -> None:
        self._settings = settings
        self._channels = channels
        self._auth_lock = threading.Lock()
        self._auth: _AuthAttempt | None = None

    # ── Routing table ────────────────────────────────────────────────────────

    def table(self) -> dict[tuple[str, str], Any]:
        return {
            ("GET", "/status"): self.status,
            ("POST", "/youtube/client"): self.set_client,
            ("POST", "/youtube/authorise"): self.authorise,
            ("POST", "/youtube/defaults"): self.set_defaults,
            ("POST", "/youtube/disconnect"): self.disconnect,
        }

    # ── Handlers ─────────────────────────────────────────────────────────────

    def status(self, _payload: dict[str, Any]) -> dict[str, Any]:
        """What the app shows on the YouTube settings page.

        Deliberately describes the credentials without revealing them: whether a
        client is configured, whether it has been authorised, and how old the
        authorisation is — that last one because a refresh token expires after 7
        days while the OAuth consent screen is in Testing mode, and its age is
        the number that predicts the next failure.
        """
        state, message = self._connection_state()
        tokens = TokenStore(self._settings.youtube_token_store)

        age_days: int | None = None
        if tokens.exists():
            try:
                obtained = tokens.load().obtained_at
                if obtained is not None:
                    age_days = (datetime.now(UTC) - obtained).days
            except CredentialError:
                age_days = None

        with self._auth_lock:
            attempt = self._auth

        return {
            "publishingEnabled": self._settings.publishing_enabled,
            "connection": state.value,
            "message": message,
            "clientSecretsPath": str(self._settings.youtube_client_secrets),
            "hasClient": self._client_path().is_file(),
            "hasToken": tokens.exists(),
            "tokenAgeDays": age_days,
            "defaultPrivacy": self._settings.youtube_default_privacy,
            # The app polls this while a browser window is open somewhere.
            "authorising": attempt is not None and not attempt.done,
            "authError": attempt.error if attempt is not None else None,
        }

    def set_client(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Write the OAuth client file from values typed in the app.

        The whole reason this API exists. The secret arrives over loopback from
        a shell on this machine and is written to the same file
        `clipforge-worker youtube-auth` would have read — so the two paths
        converge immediately and nothing downstream needs to know which was used.
        """
        client_id = str(payload.get("clientId", "")).strip()
        client_secret = str(payload.get("clientSecret", "")).strip()

        if not client_id or not client_secret:
            raise ValueError("clientId and clientSecret are both required")
        if not client_id.endswith(".apps.googleusercontent.com"):
            # Cheap, and it catches the commonest mistake by a mile: pasting the
            # project number, or the API key, instead of the OAuth client id.
            raise ValueError(
                "that does not look like a Google OAuth client id — it should end in "
                ".apps.googleusercontent.com"
            )

        path = self._client_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # The shape `load_client_secrets` already reads, so a file written here
        # and a file downloaded from the console are indistinguishable later.
        path.write_text(
            json.dumps(
                {
                    "installed": {
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "redirect_uris": [f"http://127.0.0.1:{self._settings.youtube_auth_port}/"],
                    }
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        log.info("channels.client_written", path=str(path))

        self._publish_state()
        return {"ok": True, "next": "Press Authorise to sign in to YouTube."}

    def authorise(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run the OAuth dance from the app, rather than from a terminal.

        This exists because of the seven-day catch. While the consent screen is
        in Testing mode Google expires the refresh token every week, which turns
        `clipforge-worker youtube-auth` from a one-off into a chore — and a
        chore that needs a terminal, on the one machine, by someone who
        remembers the command. A button does the same thing.

        It returns as soon as the browser is open. The dance cannot finish
        inside this request because it waits on a person, so the outcome is
        reported through `/status`, which the app is already polling. Holding
        the request open for three minutes instead would work exactly until a
        proxy, a webview or a laptop lid decided otherwise.

        The secret still never moves: the code exchange happens here, on this
        machine, and the refresh token is written to the same encrypted file the
        CLI writes (ADR-0010).
        """
        from clipforge.publish.oauth import authorization_url
        from clipforge.publish.youtube import load_client_secrets

        if not self._client_path().is_file():
            raise ValueError("no OAuth client yet — save the client id and secret first")

        tokens = TokenStore(self._settings.youtube_token_store)
        if tokens.exists() and not payload.get("force"):
            return {"ok": True, "already": True, "message": "Already authorised."}

        with self._auth_lock:
            current = self._auth
            if current is not None and not current.done:
                # A second press while a browser is already open. Re-opening the
                # window would strand the first `state` and leave the listener
                # holding the port, so hand back the URL already in flight.
                return {"ok": True, "waiting": True, "url": current.url}

            client_id, client_secret = load_client_secrets(self._settings.youtube_client_secrets)
            redirect_uri = f"http://127.0.0.1:{self._settings.youtube_auth_port}/"
            state = secrets.token_urlsafe(24)
            url, verifier = authorization_url(
                client_id=client_id, redirect_uri=redirect_uri, state=state
            )
            attempt = _AuthAttempt(started_at=datetime.now(UTC), url=url)
            self._auth = attempt
            threading.Thread(
                target=self._finish_authorisation,
                args=(attempt, state, verifier, client_id, client_secret, redirect_uri),
                name="youtube-authorise",
                daemon=True,
            ).start()

        opened = webbrowser.open(url)
        log.info("channels.authorise_started", opened=opened)
        # The URL goes back either way: a headless or unusual desktop session
        # cannot open a browser, and the app can still show a link to click.
        return {
            "ok": True,
            "waiting": True,
            "url": url,
            "openedBrowser": opened,
            "timeoutSec": AUTHORISE_TIMEOUT_S,
        }

    def _finish_authorisation(
        self,
        attempt: _AuthAttempt,
        state: str,
        verifier: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> None:
        """Wait for the redirect, exchange the code, store the token."""
        from clipforge.publish.oauth import exchange_code, listen_for_redirect

        try:
            result = listen_for_redirect(
                self._settings.youtube_auth_port, timeout_s=AUTHORISE_TIMEOUT_S
            )
            if not secrets.compare_digest(result.state, state):
                # A mismatched state means the redirect did not come from the
                # request we made. Refusing is the entire point of sending one.
                raise ValueError("the redirect did not match this request; nothing was stored")

            tokens = exchange_code(
                code=result.code,
                verifier=verifier,
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
            )
            TokenStore(self._settings.youtube_token_store).save(tokens)
            log.info("channels.authorised")
            self._publish_state()
        except Exception as exc:  # noqa: BLE001 - reported to the app, not raised into a thread
            attempt.error = str(exc)
            log.warning("channels.authorise_failed", error=str(exc))
        finally:
            attempt.done = True

    def set_defaults(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Update the channel's publish defaults in Firestore.

        Nothing here is secret, so this is the half of the configuration a phone
        could also change — and will, once there is a path for it that does not
        involve loopback.
        """
        if self._channels is None:
            raise ValueError("no Firestore connection; defaults cannot be saved")

        privacy = str(payload.get("privacy", self._settings.youtube_default_privacy))
        defaults = PublishDefaults(
            privacy=PublishPrivacy(privacy),
            category_id=str(payload.get("categoryId", "22")),
            tags=[str(t) for t in payload.get("tags", []) if str(t).strip()],
            title_suffix=payload.get("titleSuffix") or None,
            description_template=payload.get("descriptionTemplate") or None,
        )
        channel = self._ensure_channel()
        self._channels.save(
            channel.model_copy(update={"defaults": defaults, "updated_at": datetime.now(UTC)})
        )
        return {"ok": True}

    def disconnect(self, _payload: dict[str, Any]) -> dict[str, Any]:
        """Forget the authorisation, keeping the client.

        Two separate things, so "sign this channel out" does not also mean
        "re-enter the client id and secret you set up months ago".
        """
        TokenStore(self._settings.youtube_token_store).delete()
        log.info("channels.disconnected")
        self._publish_state()
        return {"ok": True}

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _client_path(self) -> Path:
        return Path(self._settings.youtube_client_secrets).expanduser()

    def _connection_state(self) -> tuple[ChannelConnection, str | None]:
        if not self._client_path().is_file():
            return (
                ChannelConnection.NOT_CONFIGURED,
                "No OAuth client yet. Add the client id and secret to connect a channel.",
            )
        if not TokenStore(self._settings.youtube_token_store).exists():
            return (
                ChannelConnection.NEEDS_AUTH,
                "The client is set up but nobody has authorised it. "
                "Press Authorise, or run: clipforge-worker youtube-auth",
            )
        return ChannelConnection.CONNECTED, None

    def _ensure_channel(self) -> Channel:
        assert self._channels is not None  # noqa: S101 - callers check
        existing = self._channels.get(DEFAULT_CHANNEL_ID)
        if existing is not None:
            return existing

        now = datetime.now(UTC)
        state, message = self._connection_state()
        return Channel(
            id=DEFAULT_CHANNEL_ID,
            uid=self._settings.worker_id,
            platform=PublishPlatform.YOUTUBE,
            label="YouTube",
            is_default=True,
            external_channel_id=None,
            external_channel_title=None,
            connection=state,
            connection_message=message,
            authorised_at=None,
            checked_at=now,
            defaults=PublishDefaults(
                privacy=PublishPrivacy(self._settings.youtube_default_privacy),
                category_id="22",
                tags=[],
                title_suffix=None,
                description_template=None,
            ),
            quota_day=None,
            quota_used_units=None,
            uploads_remaining_today=None,
            created_at=now,
            updated_at=now,
        )

    def _publish_state(self) -> None:
        """Mirror the connection state into Firestore, so the app can show it.

        Best-effort: the worker is useful without Firestore reachable, and a
        settings page that is briefly stale is a much smaller problem than a
        `set_client` that fails after having already written the file.
        """
        if self._channels is None:
            return
        state, message = self._connection_state()
        try:
            channel = self._ensure_channel()
            self._channels.save(
                channel.model_copy(
                    update={
                        "connection": state,
                        "connection_message": message,
                        "checked_at": datetime.now(UTC),
                        "updated_at": datetime.now(UTC),
                    }
                )
            )
        except Exception as exc:  # noqa: BLE001 - reporting is never worth failing over
            log.warning("channels.state_publish_failed", error=str(exc))
