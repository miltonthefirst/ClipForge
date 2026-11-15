"""Telling people when something needs them, on their phone.

Copied in design from Sarungano (app/notify, NOTIFICATIONS-PLAN.md), at the
owner's request (2026-11-15): one ``notify()``, a list of kinds each person
switches on or off, devices registered per browser, a list kept in the app, and
Firebase Cloud Messaging to carry it.

Who sends: the worker (a job failed, clips are ready, a clip went out, YouTube
wants signing in again) and the agent (a job is waiting with no worker running,
the worker will not stay up, someone signed up). Both have the Admin SDK. There
is no server of any other kind, so when the PC is off nothing is sent, and the
app says that already by showing the PC as off.

What a notification does:

1. It is written to ``users/{uid}/notifications/{id}`` for each recipient who
   has the kind switched on, so one missed on the phone is still in the list.
   A ``fold`` key makes the id stable, so the same job failing twice rewrites
   one entry rather than adding two.
2. It is pushed to each of their devices (``users/{uid}/devices``). The push
   carries a ``notification`` part, because the phone's service worker is
   Angular's, which draws a push itself only when it has one, and opens
   ``link`` on a tap (``onActionClick``). A token Cloud Messaging says is gone
   is deleted.

Nothing here ever raises into its caller: a job that finished must not be
marked failed because a phone could not be told.
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from clipforge_contracts import NotificationKind

from clipforge.config import Settings
from clipforge.notify.kinds import KINDS, is_on
from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = ["Notifier", "Push", "fcm_push"]

USERS = "users"
ADMIN_CACHE_SECONDS = 300.0
TITLE_MAX = 120
BODY_MAX = 400

# (tokens, title, body, link, tag) -> tokens Cloud Messaging says are gone.
Push = Callable[[list[str], str, str, str, str | None], list[str]]


class Notifier:
    def __init__(
        self,
        settings: Settings,
        client: Any,
        *,
        push: Push | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._db = client
        self._push = push or fcm_push(settings)
        self._clock = clock
        self._lock = threading.Lock()
        self._admins: tuple[float, list[str]] | None = None

    def notify(
        self,
        kind: NotificationKind,
        *,
        title: str,
        body: str = "",
        link: str | None = None,
        to: Iterable[str] = (),
        fold: str | None = None,
    ) -> int:
        """Tell the kind's audience. Returns how many people it reached.

        ``to`` names the owners for an ``owner`` kind; an ``admins`` kind
        ignores it and finds the admins itself.
        """
        try:
            recipients = self.admins() if KINDS[kind].audience == "admins" else sorted(set(to))
            sent = 0
            for uid in recipients:
                if self._deliver(uid, kind, title, body, link, fold):
                    sent += 1
            log.info("notify.sent", kind=kind.value, recipients=sent)
            return sent
        except Exception as exc:  # noqa: BLE001 - never into the caller
            log.warning("notify.failed", kind=kind.value, error=f"{type(exc).__name__}: {exc}")
            return 0

    def admins(self) -> list[str]:
        """Approved admins, read at most every five minutes."""
        now = time.monotonic()
        with self._lock:
            cached = self._admins
        if cached is not None and now - cached[0] < ADMIN_CACHE_SECONDS:
            return cached[1]
        found = sorted(
            snapshot.id
            for snapshot in self._db.collection(USERS).stream()
            if (snapshot.to_dict() or {}).get("role") == "ADMIN"
            and (snapshot.to_dict() or {}).get("status") == "APPROVED"
        )
        with self._lock:
            self._admins = (now, found)
        return found

    def _deliver(
        self,
        uid: str,
        kind: NotificationKind,
        title: str,
        body: str,
        link: str | None,
        fold: str | None,
    ) -> bool:
        user = self._db.collection(USERS).document(uid)
        profile = user.get()
        # A CLI job's "local", a deleted account, someone not approved: nobody
        # to tell, and nothing to write into a profile that does not exist.
        if not profile.exists or (profile.to_dict() or {}).get("status") != "APPROVED":
            return False
        switches = user.collection("settings").document("notifications").get().to_dict() or {}
        if not is_on(kind, switches.get("kinds")):
            return False

        item_id = _item_id(kind, fold)
        user.collection("notifications").document(item_id).set(
            {
                "id": item_id,
                "kind": kind.value,
                "title": title[:TITLE_MAX],
                "body": body[:BODY_MAX],
                "link": link,
                "createdAt": self._clock(),
                "readAt": None,
            }
        )

        devices = list(user.collection("devices").stream())
        tokens = [str((d.to_dict() or {}).get("token") or "") for d in devices]
        by_token = {token: d.id for token, d in zip(tokens, devices, strict=True) if token}
        if by_token:
            gone = self._push(
                list(by_token), title[:TITLE_MAX], body[:BODY_MAX], link or "/", item_id
            )
            for token in gone:
                user.collection("devices").document(by_token[token]).delete()
        return True


def _item_id(kind: NotificationKind, fold: str | None) -> str:
    if not fold:
        return f"{kind.value.lower()}-{uuid.uuid4().hex[:12]}"
    return f"{kind.value.lower()}-{hashlib.sha256(fold.encode()).hexdigest()[:16]}"


def fcm_push(settings: Settings) -> Push:
    """Send through Firebase Cloud Messaging with the default app."""

    def push(tokens: list[str], title: str, body: str, link: str, tag: str | None) -> list[str]:
        from firebase_admin import exceptions, messaging

        from clipforge.firebase_app import default_app

        app = default_app(settings)
        notification = messaging.WebpushNotification(
            title=title,
            body=body or None,
            icon="/icons/icon-192.png",
            badge="/icons/icon-192.png",
            tag=tag,
            renotify=bool(tag),
            # Angular's service worker opens this on a tap.
            data={
                "onActionClick": {
                    "default": {"operation": "navigateLastFocusedOrOpen", "url": link}
                }
            },
        )
        messages = [
            messaging.Message(
                token=token,
                webpush=messaging.WebpushConfig(
                    headers={"TTL": str(3 * 24 * 3600), "Urgency": "normal"},
                    notification=notification,
                ),
            )
            for token in tokens
        ]
        gone: list[str] = []
        for start in range(0, len(messages), 500):
            response = messaging.send_each(messages[start : start + 500], app=app)
            for token, answer in zip(tokens[start : start + 500], response.responses, strict=True):
                if answer.success:
                    continue
                error = answer.exception
                if isinstance(error, messaging.UnregisteredError) or (
                    isinstance(error, exceptions.InvalidArgumentError)
                    and "registration token" in str(error).lower()
                ):
                    gone.append(token)
                else:
                    log.warning("notify.push_failed", error=f"{type(error).__name__}: {error}")
        return gone

    return push
