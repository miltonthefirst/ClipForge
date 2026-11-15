"""Who is asking, for every request over the home wifi.

The phone sends its Firebase ID token, the same proof of sign-in Firestore
gets. It is checked here with firebase-admin's default app, which the
home route's service initialises (clipforge/home/service.py) and which caches
Google's signing keys, so a check costs no network round trip after the
first. Approval is not read here: ClipForge's access rule is approval, not a
confirmed email (firestore.rules, isApproved), so the service checks
users/{uid} after this, and passes require_verified_email=False.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# firebase-admin's default app, initialised by service.py.
APP_NAME = "[DEFAULT]"


class NotSignedIn(Exception):
    """The request carries no usable sign-in. The message says why, for the
    phone's log rather than for a person: the phone simply uses Firebase."""


@dataclass(frozen=True)
class Caller:
    uid: str
    email: str | None


def verify(
    authorization: str | None,
    verify_token: Callable[[str], dict[str, Any]] | None = None,
    *,
    require_verified_email: bool = True,
) -> Caller:
    """The caller behind an `Authorization: Bearer <ID token>` header.
    `verify_token` (token -> claims) is the test seam."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise NotSignedIn("No sign-in was sent.")
    try:
        claims = (verify_token or _verify_with_firebase)(token.strip())
    except NotSignedIn:
        raise
    except Exception as exc:  # noqa: BLE001 - every failure means the same thing here
        raise NotSignedIn(f"The sign-in could not be checked: {type(exc).__name__}") from exc
    uid = claims.get("uid") or claims.get("sub")
    if not uid:
        raise NotSignedIn("The sign-in names no account.")
    # The blueprint's default: an address nobody has confirmed proves nothing
    # about who is holding the phone. ClipForge turns it off and checks the
    # admin's approval of the account instead, which is a stronger fact.
    if require_verified_email and claims.get("email_verified") is not True:
        raise NotSignedIn("The account's email address is not confirmed.")
    return Caller(uid=str(uid), email=claims.get("email"))


def _verify_with_firebase(token: str) -> dict[str, Any]:
    import firebase_admin
    from firebase_admin import auth

    try:
        app = firebase_admin.get_app(APP_NAME)
    except ValueError as exc:
        raise NotSignedIn("The workstation is not signed in to Firebase yet.") from exc
    return dict(auth.verify_id_token(token, app=app))
