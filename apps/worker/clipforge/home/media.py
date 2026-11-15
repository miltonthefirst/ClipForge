"""Clips straight from this PC over the home wifi (docs/HOME-NETWORK-PLAN.md;
the home-route blueprint, section 5).

A phone at home asks the listener for a link to a clip (POST /media-links,
signed in and approved) and plays it from GET /media/{token}. Away from home
nothing changes: the phone uses the clip's bucket copy (ADR-0018), as before.

A link is its own key: an <audio> or <video> element cannot send a
sign-in, so the token is long, random, handed only to an approved account,
expires, and is good only on the home network (the listener refuses
anything else).

What a link serves is a copy in CLIP_DIR, never the clip's own file: on
Windows a file being played is held open, and a remake or a re-render
replacing it (os.replace) would fail. A copy is named by what it holds (the
source's path, size and time, or the bytes), so the same clip asked for
again is the same copy, under the same link while it has a while to run
(and the phone's cache works). A copy goes a while after its last link,
not at once: a phone may be mid-play, and a file held open cannot be
deleted on Windows. What a restart leaves behind is swept when the
listener starts.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# How long a link lasts.
LINK_HOURS = 12
# A link asked for again is handed out again while it has this long left.
REUSE_WHILE = timedelta(hours=1)
# Links kept at once.
MAX_LINKS = 5000
# A copy outlives its last link this long: a phone may still be playing it.
KEEP_AFTER_SECONDS = 600
# Expired links are looked for at most this often.
SWEEP_EVERY_SECONDS = 60
# One folder per project, so ClipForge and Sarungano never sweep each other's copies.
CLIP_DIR = os.path.join(tempfile.gettempdir(), "clipforge-home-media")

_EXTENSIONS = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "image/png": ".png",
    "image/jpeg": ".jpg",
}


@dataclass
class Offered:
    path: str
    content_type: str
    expires: datetime


_lock = threading.Lock()
_offered: dict[str, Offered] = {}  # token -> what it serves, oldest first
_by_copy: dict[str, str] = {}  # copy's path -> its newest token
_doomed: dict[str, float] = {}  # copy's path -> when it may go (monotonic)
_last_sweep = 0.0


def offer(
    base_url: str,
    content_type: str,
    *,
    path: str | None = None,
    content: bytes | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    """A link on the home wifi to a copy of one file (or clip), and when it expires."""
    now = now or datetime.now(timezone.utc)
    if path is not None:
        stat = os.stat(path)
        key = f"{os.path.abspath(path)}|{stat.st_size}|{stat.st_mtime_ns}"
    else:
        key = hashlib.sha256(content or b"").hexdigest()
    copy = os.path.join(
        CLIP_DIR, hashlib.sha256(key.encode()).hexdigest()[:40] + _EXTENSIONS.get(content_type, "")
    )
    with _lock:
        token = _by_copy.get(copy)
        known = _offered.get(token) if token else None
        if token and known and known.expires - now > REUSE_WHILE:
            return _link(base_url, token), known.expires.isoformat()
        _doomed.pop(copy, None)  # wanted again
    if not os.path.exists(copy):
        os.makedirs(CLIP_DIR, exist_ok=True)
        temporary = f"{copy}.{secrets.token_hex(4)}.tmp"
        if path is not None:
            shutil.copyfile(path, temporary)
        else:
            with open(temporary, "wb") as f:
                f.write(content or b"")
        os.replace(temporary, copy)
    token = secrets.token_urlsafe(32)
    expires = now + timedelta(hours=LINK_HOURS)
    with _lock:
        _offered[token] = Offered(copy, content_type, expires)
        _by_copy[copy] = token
        while len(_offered) > MAX_LINKS:
            _drop(next(iter(_offered)))  # the oldest
    _sweep(now)
    return _link(base_url, token), expires.isoformat()


def find(token: str, now: datetime | None = None) -> Offered | None:
    """What a link points at, while it lasts and its copy is there."""
    now = now or datetime.now(timezone.utc)
    _sweep(now)
    with _lock:
        found = _offered.get(token)
    if found is None or found.expires <= now or not os.path.exists(found.path):
        return None
    return found


def _link(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/media/{token}"


def _drop(token: str) -> None:
    """Under the lock: a link ends; its copy goes a while after its last."""
    gone = _offered.pop(token, None)
    if gone and _by_copy.get(gone.path) == token:
        del _by_copy[gone.path]
        _doomed[gone.path] = time.monotonic() + KEEP_AFTER_SECONDS


def _sweep(now: datetime, force: bool = False) -> None:
    """Ends expired links and deletes copies whose time has come; at most
    every SWEEP_EVERY_SECONDS, and the deleting outside the lock. A copy
    that cannot go yet (on Windows, one still being played) is tried again
    at the next sweep."""
    global _last_sweep
    with _lock:
        if not force and time.monotonic() - _last_sweep < SWEEP_EVERY_SECONDS:
            return
        _last_sweep = time.monotonic()
        for token in [t for t, o in _offered.items() if o.expires <= now]:
            _drop(token)
        due = [p for p, at in _doomed.items() if force or at <= time.monotonic()]
    for copy in due:
        try:
            os.remove(copy)
        except FileNotFoundError:
            pass
        except OSError:
            continue
        with _lock:
            if copy not in _by_copy:
                _doomed.pop(copy, None)


def clear() -> None:
    """Every link ends and every copy goes, those a crash left included:
    the listener stopped or started (links live in memory only), or a test
    is done. A copy held open is left to the next clear."""
    with _lock:
        for token in list(_offered):
            _drop(token)
    _sweep(datetime.now(timezone.utc), force=True)
    try:
        leftovers = os.listdir(CLIP_DIR)
    except OSError:
        return
    for name in leftovers:
        try:
            os.remove(os.path.join(CLIP_DIR, name))
        except OSError:
            pass
