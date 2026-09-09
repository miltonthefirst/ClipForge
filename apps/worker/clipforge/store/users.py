"""Accounts: the Firestore profile, and the Auth record behind it.

Two systems hold half of a user each. Firebase Auth owns the credential and the
uid; Firestore owns the role and the approval status, at ``users/{uid}``, where
security rules can resolve it with one ``get()``.

Everything here needs Admin credentials, which is why it lives on the worker
rather than in the PWA. Approving your own account is not a thing a client
should be able to attempt, however carefully the rules are written.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from clipforge_contracts import UserProfile, UserRole, UserStatus
from google.cloud import firestore

from clipforge.config import Settings
from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = ["USERS", "AuthAccount", "UserAdmin", "UserNotFoundError", "UserStore"]

USERS = "users"


class UserNotFoundError(RuntimeError):
    """No account with that email exists in Firebase Auth."""


@dataclass(frozen=True)
class AuthAccount:
    """The Auth half: who they are and how they sign in."""

    uid: str
    email: str
    display_name: str | None
    providers: tuple[str, ...]
    disabled: bool

    def describe_providers(self) -> str:
        return ", ".join(self.providers) or "none"


class UserStore:
    """Profiles at ``users/{uid}``."""

    def __init__(self, client: firestore.Client, settings: Settings) -> None:
        self._db = client
        self._settings = settings

    def get(self, uid: str) -> UserProfile | None:
        snapshot = self._db.collection(USERS).document(uid).get()
        if not snapshot.exists:
            return None
        return UserProfile.model_validate(snapshot.to_dict() or {})

    def all(self) -> list[UserProfile]:
        return [
            UserProfile.model_validate(doc.to_dict() or {})
            for doc in self._db.collection(USERS).stream()
        ]

    def save(self, profile: UserProfile) -> None:
        self._db.collection(USERS).document(profile.uid).set(
            profile.model_dump(by_alias=True, mode="python")
        )

    def admins(self) -> list[UserProfile]:
        return [
            u for u in self.all() if u.role is UserRole.ADMIN and u.status is UserStatus.APPROVED
        ]


class UserAdmin:
    """Auth-side account management, through the Admin SDK.

    Wrapped rather than used directly so the CLI has one place to look up an
    account by email — which is what a person actually has to hand — and the
    lazy import stays in one spot. `firebase_admin` initialises a global app on
    first use, and a worker that never touches accounts should not pay for that.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._ready = False

    def _ensure(self) -> None:
        if self._ready:
            return
        import firebase_admin
        from firebase_admin import credentials

        if not firebase_admin._apps:
            if self._settings.use_emulators:
                firebase_admin.initialize_app(
                    options={"projectId": self._settings.firebase_project_id}
                )
            else:
                firebase_admin.initialize_app(
                    credentials.Certificate(self._settings.google_application_credentials),
                    options={"projectId": self._settings.firebase_project_id},
                )
        self._ready = True

    def by_email(self, email: str) -> AuthAccount:
        self._ensure()
        from firebase_admin import auth as fb_auth

        try:
            record = fb_auth.get_user_by_email(email)
        except fb_auth.UserNotFoundError as exc:
            raise UserNotFoundError(
                f"no account for {email}. Register in the app first, then run this again — "
                "the uid has to exist before it can be given a role."
            ) from exc

        return AuthAccount(
            uid=record.uid,
            email=record.email or email,
            display_name=record.display_name,
            providers=tuple(p.provider_id for p in record.provider_data),
            disabled=record.disabled,
        )

    def list_accounts(self, limit: int = 100) -> list[AuthAccount]:
        self._ensure()
        from firebase_admin import auth as fb_auth

        found: list[AuthAccount] = []
        for record in fb_auth.list_users().iterate_all():
            found.append(
                AuthAccount(
                    uid=record.uid,
                    email=record.email or "(no email)",
                    display_name=record.display_name,
                    providers=tuple(p.provider_id for p in record.provider_data),
                    disabled=record.disabled,
                )
            )
            if len(found) >= limit:
                break
        return found

    def set_password(self, uid: str, password: str) -> None:
        """Give an account a password it did not have.

        The reason this exists: an account created by Google sign-in has no
        password, and Google will refuse to sign it in again on a new device
        until a 48-hour verification completes. Adding a password to the *same*
        uid sidesteps that without creating a second account — so nothing that
        already references the uid has to be migrated.
        """
        self._ensure()
        from firebase_admin import auth as fb_auth

        fb_auth.update_user(uid, password=password)
        log.info("user.password_set", uid=uid)


def new_profile(
    account: AuthAccount,
    *,
    role: UserRole = UserRole.MEMBER,
    status: UserStatus = UserStatus.PENDING,
    decided_by: str | None = None,
    now: datetime | None = None,
) -> UserProfile:
    now = now or datetime.now(UTC)
    decided = status is not UserStatus.PENDING
    return UserProfile(
        uid=account.uid,
        email=account.email,
        display_name=account.display_name,
        photo_url=None,
        role=role,
        status=status,
        created_at=now,
        decided_at=now if decided else None,
        decided_by=decided_by if decided else None,
    )
