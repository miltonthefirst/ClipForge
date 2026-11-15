"""Every kind of notification, who gets it, and whether it starts on.

New features add their kind here (and to NotificationKind in the contracts);
the Settings page lists them by themselves. Modelled on Sarungano's
app/notify/kinds.py.

Two audiences:

- ``owner``: whoever the notification is about, usually the person who
  submitted the job. Given explicitly by the caller.
- ``admins``: every APPROVED account with role ADMIN.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from clipforge_contracts import NotificationKind

__all__ = ["KINDS", "Kind", "is_on"]


@dataclass(frozen=True)
class Kind:
    kind: NotificationKind
    audience: Literal["owner", "admins"]
    default: bool


KINDS: dict[NotificationKind, Kind] = {
    k.kind: k
    for k in (
        Kind(NotificationKind.JOB_FAILED, "owner", True),
        Kind(NotificationKind.CLIPS_READY, "owner", True),
        Kind(NotificationKind.PUBLISHED, "owner", True),
        Kind(NotificationKind.JOB_WAITING, "owner", True),
        Kind(NotificationKind.YOUTUBE_SIGN_IN, "admins", True),
        Kind(NotificationKind.WORKER_DOWN, "admins", True),
        Kind(NotificationKind.SIGNUP, "admins", True),
        # Asked for by pressing a button; always delivered.
        Kind(NotificationKind.TEST, "owner", True),
    )
}


def is_on(kind: NotificationKind, switches: dict[str, bool] | None) -> bool:
    """A person's switch for this kind, or its default. TEST is always on."""
    if kind is NotificationKind.TEST:
        return True
    value = (switches or {}).get(kind.value)
    return KINDS[kind].default if value is None else bool(value)
