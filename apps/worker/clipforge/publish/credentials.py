"""OAuth credentials, held on the worker and never anywhere else.

Decision **D7**, and the single most valuable secret in the project. A YouTube
refresh token can upload to, and delete from, the operator's channel
indefinitely. Firestore is a *shared, replicated, remotely-readable* store; a
refresh token in it is a credential in someone else's datacentre, protected only
by security rules that the Admin SDK bypasses anyway.

So the token lives in a file on the worker, encrypted at rest, and the PWA never
sees it. What the PWA sends is a publish **intent** — "this clip, on this basis" —
and the worker decides whether to act on it.

## On the encryption

The key is derived from a machine-local secret, not from a password the user
types: the worker runs unattended and must be able to publish a scheduled clip at
04:00 without someone present to unlock anything. That is a deliberate and stated
limitation — encryption at rest here protects against a token being read out of a
backup, a synced folder or a shoulder-surfed terminal, **not** against an attacker
who already has code execution as this user. Claiming otherwise would be worse
than not encrypting at all, because it would invite trusting it further than it
deserves.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import platform
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = ["CredentialError", "OAuthTokens", "TokenStore"]


class CredentialError(RuntimeError):
    """A credential is missing, unreadable, or refuses to decrypt."""


@dataclass(frozen=True)
class OAuthTokens:
    """What the worker needs to act on the operator's behalf."""

    refresh_token: str
    access_token: str | None = None
    expires_at: datetime | None = None
    scopes: tuple[str, ...] = ()
    obtained_at: datetime | None = None

    def is_expired(self, now: datetime | None = None) -> bool:
        """Whether the access token needs refreshing.

        A minute of slack: a token that expires while a 20 MB upload is in flight
        fails the upload, and re-requesting one early costs nothing.
        """
        if self.access_token is None or self.expires_at is None:
            return True
        return self.expires_at <= (now or datetime.now(UTC)).replace(microsecond=0)

    def redacted(self) -> dict[str, object]:
        """A form safe to log. Never log the tokens themselves."""
        return {
            "hasRefreshToken": bool(self.refresh_token),
            "hasAccessToken": bool(self.access_token),
            "expiresAt": self.expires_at.isoformat() if self.expires_at else None,
            "scopes": list(self.scopes),
        }


def _machine_key() -> bytes:
    """A key derived from stable, machine-local material.

    Not a password: the worker publishes on a schedule and must run unattended.
    See the module docstring on exactly what this does and does not protect
    against.
    """
    material = "|".join(
        [
            platform.node(),
            platform.machine(),
            # Ties the key to this user on this machine, so a copied file does
            # not decrypt under a different account.
            os.environ.get("USERNAME") or os.environ.get("USER") or "",
            "clipforge/youtube/v1",
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).digest()


def _xor(payload: bytes, key: bytes) -> bytes:
    """A keystream cipher over SHA-256 counter blocks.

    Deliberately dependency-free rather than reaching for `cryptography`: this
    protects a file at rest against casual reading, and the threat model in the
    module docstring is explicit that it is not doing more. Pulling in a crypto
    library would imply a guarantee the key derivation cannot back.
    """
    stream = bytearray()
    counter = 0
    while len(stream) < len(payload):
        stream += hashlib.sha256(key + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(a ^ b for a, b in zip(payload, stream, strict=False))


class TokenStore:
    """Reads and writes the worker's OAuth tokens, encrypted at rest."""

    def __init__(self, path: Path) -> None:
        self._path = path.expanduser()

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.is_file()

    def save(self, tokens: OAuthTokens) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "refreshToken": tokens.refresh_token,
                "accessToken": tokens.access_token,
                "expiresAt": tokens.expires_at.isoformat() if tokens.expires_at else None,
                "scopes": list(tokens.scopes),
                "obtainedAt": (tokens.obtained_at or datetime.now(UTC)).isoformat(),
            }
        ).encode("utf-8")

        staging = self._path.with_suffix(self._path.suffix + ".partial")
        staging.write_bytes(base64.b64encode(_xor(payload, _machine_key())))
        staging.replace(self._path)
        self._restrict_permissions()

        log.info("credentials.saved", path=str(self._path), **tokens.redacted())

    def load(self) -> OAuthTokens:
        if not self.exists():
            raise CredentialError(
                f"no YouTube credentials at {self._path}. Run: clipforge-worker youtube-auth"
            )
        try:
            payload = json.loads(_xor(base64.b64decode(self._path.read_bytes()), _machine_key()))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise CredentialError(
                f"could not decrypt {self._path}. The key is derived from this machine and "
                "user, so a file copied from elsewhere will not open. "
                "Re-run: clipforge-worker youtube-auth"
            ) from exc

        expires = payload.get("expiresAt")
        obtained = payload.get("obtainedAt")
        return OAuthTokens(
            refresh_token=str(payload["refreshToken"]),
            access_token=payload.get("accessToken"),
            expires_at=datetime.fromisoformat(expires) if expires else None,
            scopes=tuple(payload.get("scopes") or ()),
            obtained_at=datetime.fromisoformat(obtained) if obtained else None,
        )

    def delete(self) -> None:
        self._path.unlink(missing_ok=True)

    def _restrict_permissions(self) -> None:
        """Owner-only, where the platform supports it.

        A no-op on Windows, where the meaningful control is ACLs rather than
        mode bits — noted rather than silently skipped, because a reader of this
        code should not assume the file is protected there.
        """
        with contextlib.suppress(OSError):
            self._path.chmod(stat.S_IRUSR | stat.S_IWUSR)
