"""firebase-admin's default app, made once per process.

Two things need it beside Firestore (which uses google-cloud-firestore's own
client): checking a phone's sign-in on the home wifi route
(clipforge/home/service.py) and sending notifications (clipforge/notify). Both
go through here, so the credentials and the emulator switch are decided in one
place, the way clipforge/store/users.py has always done it for admin commands.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from clipforge.config import Settings

__all__ = ["default_app"]

_lock = threading.Lock()


def default_app(settings: Settings) -> Any:
    """The default firebase-admin app, initialised on first use."""
    import firebase_admin
    from firebase_admin import credentials

    with _lock:
        if not firebase_admin._apps:
            options = {"projectId": settings.firebase_project_id}
            if settings.use_emulators:
                os.environ.setdefault("FIREBASE_AUTH_EMULATOR_HOST", settings.auth_emulator_host)
                firebase_admin.initialize_app(options=options)
            elif settings.google_application_credentials:
                firebase_admin.initialize_app(
                    credentials.Certificate(settings.google_application_credentials),
                    options=options,
                )
            else:
                firebase_admin.initialize_app(options=options)
        return firebase_admin.get_app()
