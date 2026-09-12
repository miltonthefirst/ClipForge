"""YouTube Data API v3: OAuth, resumable upload, and quota budgeting.

Three constraints shape this, and all three are documented in docs/PLAN.md §7 as
real rather than theoretical:

**Quota.** The default allowance is 10,000 units a day and an upload costs
**1,600** — about six uploads. So the budget is tracked and checked *before* an
upload starts. Discovering the limit on the seventh upload means discovering it
after the encode, the review and the user's expectation.

**Refresh tokens expire after 7 days while the OAuth consent screen is in
"Testing" mode.** The upload scope is *sensitive*, so leaving Testing requires
Google verification. This is built for the re-auth path first: an expired refresh
token produces a message that says exactly what to run, not an opaque 401.

**Resumability.** A 20 MB upload over a domestic connection fails sometimes. The
resumable protocol is used, and the upload URL is returned to the caller so a
retry continues rather than restarting — and, more importantly, so a retry does
not create a second video.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from clipforge.observability import get_logger
from clipforge.publish.credentials import CredentialError, OAuthTokens, TokenStore

log = get_logger(__name__)

__all__ = [
    "DAILY_QUOTA_UNITS",
    "UPLOAD_QUOTA_UNITS",
    "QuotaExceededError",
    "UploadResult",
    "YouTubeClient",
    "YouTubeError",
]

TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL, not a secret
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

# The scope needed to upload. Sensitive, hence the verification requirement.
UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"

DAILY_QUOTA_UNITS = 10_000
UPLOAD_QUOTA_UNITS = 1_600
LIST_QUOTA_UNITS = 1


class YouTubeError(RuntimeError):
    """An upload could not be completed."""

    def __init__(
        self, message: str, *, retryable: bool = True, code: str = "YOUTUBE_ERROR"
    ) -> None:
        self.retryable = retryable
        self.code = code
        super().__init__(message)


class QuotaExceededError(YouTubeError):
    """Today's quota cannot cover another upload."""

    def __init__(self, used: int, needed: int) -> None:
        super().__init__(
            f"today's YouTube quota cannot cover another upload: {used} of "
            f"{DAILY_QUOTA_UNITS} units used, {needed} needed. Quota resets at "
            "midnight Pacific time.",
            # It resets on its own, so this is worth waiting for rather than
            # failing the job permanently.
            retryable=True,
            code="QUOTA_EXCEEDED",
        )


@dataclass(frozen=True)
class UploadResult:
    video_id: str
    url: str
    quota_units: int


@dataclass
class QuotaLedger:
    """What today's quota has been spent on.

    Tracked locally rather than queried, because the API offers no endpoint for
    remaining quota — and a check that cost quota to perform would be its own
    small joke.
    """

    day: str
    used_units: int = 0

    @classmethod
    def today(cls) -> QuotaLedger:
        return cls(day=datetime.now(UTC).strftime("%Y-%m-%d"))

    def roll_over(self, now: datetime | None = None) -> None:
        today = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
        if today != self.day:
            self.day = today
            self.used_units = 0

    def can_afford(self, units: int) -> bool:
        self.roll_over()
        return self.used_units + units <= DAILY_QUOTA_UNITS

    def spend(self, units: int) -> None:
        self.roll_over()
        self.used_units += units

    @property
    def uploads_remaining(self) -> int:
        self.roll_over()
        return max(0, (DAILY_QUOTA_UNITS - self.used_units) // UPLOAD_QUOTA_UNITS)


class YouTubeClient:
    """Uploads to the operator's channel, using credentials held on this machine."""

    def __init__(
        self,
        *,
        tokens: TokenStore,
        client_id: str,
        client_secret: str,
        ledger: QuotaLedger | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = 300.0,
    ) -> None:
        self._tokens = tokens
        self._client_id = client_id
        self._client_secret = client_secret
        self._ledger = ledger or QuotaLedger.today()
        self._transport = transport
        self._timeout_s = timeout_s

    @property
    def quota(self) -> QuotaLedger:
        return self._ledger

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self._timeout_s, transport=self._transport)

    # ── Credentials ──────────────────────────────────────────────────────────

    def access_token(self, *, now: datetime | None = None) -> str:
        """A usable access token, refreshing if needed.

        The refreshed token is written back, so a long-running worker does not
        re-refresh on every upload — and so a restart mid-session does not need
        the network before it can do anything.
        """
        stored = self._tokens.load()
        if not stored.is_expired(now):
            assert stored.access_token is not None  # noqa: S101 - implied by is_expired
            return stored.access_token

        with self._client() as client:
            response = client.post(
                TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": stored.refresh_token,
                    "grant_type": "refresh_token",
                },
            )

        if response.status_code == 400:
            # The 7-day expiry in Testing mode lands here. Say what to do about
            # it rather than surfacing "invalid_grant".
            raise YouTubeError(
                "the YouTube refresh token was rejected. While the OAuth consent screen is "
                "in Testing mode, refresh tokens expire after 7 days — re-authorise with: "
                "clipforge-worker youtube-auth",
                retryable=False,
                code="REAUTH_REQUIRED",
            )
        if response.status_code != 200:
            raise YouTubeError(
                f"token refresh failed: {response.status_code} {response.text[:200]}"
            )

        body = response.json()
        refreshed = OAuthTokens(
            refresh_token=stored.refresh_token,
            access_token=str(body["access_token"]),
            expires_at=(now or datetime.now(UTC))
            + timedelta(seconds=int(body.get("expires_in", 3600)) - 60),
            scopes=stored.scopes,
            obtained_at=stored.obtained_at,
        )
        self._tokens.save(refreshed)
        return refreshed.access_token or ""

    # ── Upload ───────────────────────────────────────────────────────────────

    def start_resumable_upload(
        self,
        *,
        title: str,
        description: str,
        tags: list[str],
        privacy: str = "unlisted",
        category_id: str = "22",
    ) -> str:
        """Reserve an upload session and return its URL.

        Called *before* any bytes move, and the URL is persisted by the caller.
        That ordering is what makes a retry resume one video rather than create
        a second.
        """
        if not self._ledger.can_afford(UPLOAD_QUOTA_UNITS):
            raise QuotaExceededError(self._ledger.used_units, UPLOAD_QUOTA_UNITS)

        metadata = {
            "snippet": {
                # YouTube rejects titles over 100 characters outright, and a hook
                # is not written to a length limit.
                "title": title[:100],
                "description": description[:4900],
                "tags": tags[:20],
                "categoryId": category_id,
            },
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
        }

        with self._client() as client:
            response = client.post(
                UPLOAD_URL,
                params={"uploadType": "resumable", "part": "snippet,status"},
                headers={
                    "Authorization": f"Bearer {self.access_token()}",
                    "Content-Type": "application/json; charset=UTF-8",
                    "X-Upload-Content-Type": "video/mp4",
                },
                content=json.dumps(metadata),
            )

        if response.status_code == 403 and "quota" in response.text.lower():
            raise QuotaExceededError(self._ledger.used_units, UPLOAD_QUOTA_UNITS)
        if response.status_code not in (200, 201):
            raise YouTubeError(
                f"could not start the upload: {response.status_code} {response.text[:300]}"
            )

        location = response.headers.get("Location")
        if not location:
            raise YouTubeError("YouTube accepted the upload request but returned no session URL")
        return str(location)

    def upload_bytes(self, session_url: str, clip: Path) -> UploadResult:
        """Send the file to an already-reserved session."""
        if not clip.is_file():
            raise YouTubeError(f"no such clip file: {clip}", retryable=False, code="CLIP_MISSING")

        size = clip.stat().st_size
        with self._client() as client, clip.open("rb") as handle:
            response = client.put(
                session_url,
                headers={"Content-Length": str(size), "Content-Type": "video/mp4"},
                content=handle.read(),
            )

        if response.status_code in (200, 201):
            body = response.json()
            video_id = str(body.get("id", ""))
            if not video_id:
                raise YouTubeError("YouTube reported success but returned no video id")
            # Charged only on success: a failed attempt does not consume the
            # upload cost, and over-counting would refuse uploads we could make.
            self._ledger.spend(UPLOAD_QUOTA_UNITS)
            log.info("youtube.uploaded", video_id=video_id, size_mb=size // (1024 * 1024))
            return UploadResult(
                video_id=video_id,
                url=f"https://www.youtube.com/watch?v={video_id}",
                quota_units=UPLOAD_QUOTA_UNITS,
            )

        if response.status_code == 308:
            # The protocol's "incomplete" — resumable, and the caller still holds
            # the session URL.
            raise YouTubeError(
                "the upload was interrupted; retrying will resume the same session",
                retryable=True,
                code="UPLOAD_INCOMPLETE",
            )
        raise YouTubeError(f"upload failed: {response.status_code} {response.text[:300]}")

    def find_video(self, video_id: str) -> dict[str, object] | None:
        """Ask the platform whether a video already exists.

        This is how a retry reconciles instead of double-posting: the publication
        record is written before the attempt, so a retry knows what to look for.
        """
        with self._client() as client:
            response = client.get(
                VIDEOS_URL,
                params={"part": "status,snippet", "id": video_id},
                headers={"Authorization": f"Bearer {self.access_token()}"},
            )
        if response.status_code != 200:
            return None
        self._ledger.spend(LIST_QUOTA_UNITS)
        items = response.json().get("items") or []
        return items[0] if items else None


def load_client_secrets(path: Path) -> tuple[str, str]:
    """Read a Google OAuth client id and secret from the downloaded JSON."""
    if not path.is_file():
        raise CredentialError(
            f"no OAuth client secrets at {path}. Create an OAuth client in the Google "
            "Cloud console, download the JSON, and point "
            "CLIPFORGE_YOUTUBE_CLIENT_SECRETS at it."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CredentialError(f"could not read {path}: {exc}") from exc

    block = payload.get("installed") or payload.get("web") or {}
    client_id = block.get("client_id")
    client_secret = block.get("client_secret")
    if not client_id or not client_secret:
        raise CredentialError(
            f"{path} is not a Google OAuth client secrets file (no client_id/client_secret)"
        )
    return str(client_id), str(client_secret)
