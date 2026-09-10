"""Publishing channels at ``channels/{channelId}``.

Holds no credentials. What lives here is the describable half of a connection —
which channel, whether the worker can currently reach it, its publish defaults
and what is left of today's quota — so the app can render a settings page from
anywhere without a secret ever being readable remotely.
See docs/adr/0010-worker-held-publishing-credentials.md.
"""

from __future__ import annotations

from clipforge_contracts import Channel
from google.cloud import firestore

from clipforge.config import Settings

__all__ = ["CHANNELS", "ChannelStore"]

CHANNELS = "channels"


class ChannelStore:
    def __init__(self, client: firestore.Client, settings: Settings) -> None:
        self._db = client
        self._settings = settings

    def get(self, channel_id: str) -> Channel | None:
        snapshot = self._db.collection(CHANNELS).document(channel_id).get()
        if not snapshot.exists:
            return None
        return Channel.model_validate(snapshot.to_dict() or {})

    def all(self) -> list[Channel]:
        return [
            Channel.model_validate(doc.to_dict() or {})
            for doc in self._db.collection(CHANNELS).stream()
        ]

    def save(self, channel: Channel) -> None:
        self._db.collection(CHANNELS).document(channel.id).set(
            channel.model_dump(by_alias=True, mode="python")
        )

    def default(self) -> Channel | None:
        """The channel a publish uses when none was chosen.

        Falls back to the only one there is, which is the case today and keeps
        the caller from having to special-case a single-channel install.
        """
        channels = self.all()
        if not channels:
            return None
        return next((c for c in channels if c.is_default), channels[0])
