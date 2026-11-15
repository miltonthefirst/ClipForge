"""Grouped error monitoring for the worker and the agent (the logging blueprint).

Errors are folded by fingerprint into ``logGroups/{fp}`` with a count, the
newest 25 occurrences kept, and shown to admins on the Logs page, where they
are resolved, watched or deleted. A group that is new, back after being
resolved, or watched raises a NEW_ERROR notification instead of the
blueprint's email. See ``.blueprints.json`` for how this maps onto the
Firebase-shaped spec.
"""

from __future__ import annotations

from typing import Any

from clipforge_contracts import NotificationKind

__all__ = ["install_for"]


def install_for(app: str, settings: Any, client: Any) -> None:
    """Capture this process's errors into the Logs page."""
    from clipforge.logs import capture
    from clipforge.logs.store import LogStore
    from clipforge.notify import Notifier

    store = LogStore(client)
    notifier = Notifier(settings, client)
    where = {"worker": "the worker", "agent": "the agent"}.get(app, app)

    def on_new(source: str, message: str, fingerprint: str) -> None:
        notifier.notify(
            NotificationKind.NEW_ERROR,
            title=f"An error in {where}",
            body=message[:300],
            link=f"/logs/{fingerprint}",
            fold=f"log-{fingerprint}",
        )

    capture.install(app, store.ingest, on_new)
