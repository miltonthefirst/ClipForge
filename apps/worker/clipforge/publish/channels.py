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


class ChannelRoutes:
    """Read and change a channel's configuration, from the machine only."""

    def __init__(self, settings: Settings, channels: ChannelStore | None = None) -> None:
        self._settings = settings
        self._channels = channels

    # ── Routing table ────────────────────────────────────────────────────────

    def table(self) -> dict[tuple[str, str], Any]:
        return {
            ("GET", "/status"): self.status,
            ("POST", "/youtube/client"): self.set_client,
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

        return {
            "publishingEnabled": self._settings.publishing_enabled,
            "connection": state.value,
            "message": message,
            "clientSecretsPath": str(self._settings.youtube_client_secrets),
            "hasClient": self._client_path().is_file(),
            "hasToken": tokens.exists(),
            "tokenAgeDays": age_days,
            "defaultPrivacy": self._settings.youtube_default_privacy,
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
        return {"ok": True, "next": "Run: clipforge-worker youtube-auth"}

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
                "Run: clipforge-worker youtube-auth",
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
