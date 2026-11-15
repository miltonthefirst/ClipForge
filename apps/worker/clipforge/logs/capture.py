"""How errors in the worker and the agent reach the Logs page.

The logging blueprint's client half, for Python, after Sarungano's
app/logs/capture.py, with one difference that matters: ClipForge logs through
structlog straight to stdout (clipforge/observability.py), so a handler on the
root logger would see almost nothing. The capture is therefore a structlog
processor, plus a root handler for third-party libraries and the excepthooks
for what escapes entirely.

``report()`` is the one door. It never raises, never blocks and never
recurses: it is called from inside logging, where a failure to report must not
become a second failure. It only queues; one background thread writes to
Firestore and raises the notification, and everything that thread itself logs
is ignored, so a broken Firestore cannot report its own breakage forever.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import socket
import sys
import threading
import time
import traceback
import urllib.error
from collections import deque
from collections.abc import Callable, Iterator
from typing import Any

__all__ = ["CAPTURED", "classify", "install", "report", "uninstall"]

# What is captured. Errors only, as Sarungano's owner chose: the agent and the
# worker log transient warnings (a publish retried, a listener replaced) that
# would bury the real problems.
CAPTURED = ("error",)
# One busy loop must not fill the Logs page or Firestore.
PER_MINUTE = 30
QUEUE_MAX = 200

_state = threading.local()
_installed: dict[str, Any] = {}
_Item = tuple[str, str, str, str | None, str | None, dict[str, str]]
# None is the writer's signal to stop.
_queue: queue.Queue[_Item | None] | None = None
_recent: deque[float] = deque()
_recent_lock = threading.Lock()

_NETWORK_ERROR_NAMES = {
    "ConnectionError",
    "ConnectTimeout",
    "ReadTimeout",
    "Timeout",
    "ConnectError",
    "TransportError",
    "ServiceUnavailable",
    "DeadlineExceeded",
    "RetryError",
}
_NETWORK_MARKERS = (
    "network-request-failed",
    "getaddrinfo failed",
    "name or service not known",
    "temporary failure in name resolution",
    "failed to establish a new connection",
    "connection refused",
    "connection reset",
    "connection aborted",
    "max retries exceeded",
    "winerror 10054",
    "winerror 10060",
    "winerror 10061",
)
_PERMISSION_MARKERS = ("permission-denied", "permission_denied", "access is denied")
# Their records come from inside an ingest, or say nothing a person can act on.
_IGNORED_LOGGERS = ("google", "grpc", "urllib3", "clipforge.logs")


# ── Classifying (the blueprint, section 5) ────────────────────────────────────


def _chain(exc: BaseException | None) -> Iterator[BaseException]:
    seen = 0
    while exc is not None and seen < 10:
        yield exc
        exc = exc.__cause__
        seen += 1


def _is_network_exception(exc: BaseException) -> bool:
    if isinstance(exc, ConnectionError | TimeoutError | socket.gaierror):
        return True
    if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
        return True
    return any(cls.__name__ in _NETWORK_ERROR_NAMES for cls in type(exc).__mro__)


def classify(message: str, exc: BaseException | None = None) -> str | None:
    causes = list(_chain(exc))
    text = message.lower()
    if any(isinstance(cause, PermissionError) for cause in causes) or any(
        marker in text for marker in _PERMISSION_MARKERS
    ):
        return "permission-denied"
    if any(_is_network_exception(cause) for cause in causes) or any(
        marker in text for marker in _NETWORK_MARKERS
    ):
        return "network"
    return None


# ── Reporting ────────────────────────────────────────────────────────────────


def report(
    app: str,
    level: str,
    message: str,
    *,
    exc: BaseException | None = None,
    stack: str | None = None,
    extra: dict[str, str] | None = None,
) -> bool:
    """Queue one occurrence. True when it was queued; never raises."""
    if getattr(_state, "reporting", False) or _queue is None:
        return False
    try:
        if exc is not None and stack is None:
            stack = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        category = classify(message, exc)
        # A machine that could not be reached is noise at the "error" level:
        # a warning, which is below what is captured.
        if category == "network" and level == "error":
            level = "warn"
        if level not in CAPTURED or not _within_rate():
            return False
        _queue.put_nowait((app, level, message, stack, category, dict(extra or {})))
        return True
    except Exception:  # noqa: BLE001 - must never raise
        return False


def _within_rate() -> bool:
    now = time.monotonic()
    with _recent_lock:
        while _recent and now - _recent[0] > 60:
            _recent.popleft()
        if len(_recent) >= PER_MINUTE:
            return False
        _recent.append(now)
        return True


def _writer(ingest: Callable[..., Any], on_new: Callable[[str, str, str], None]) -> None:
    # Everything this thread logs is about the log store itself.
    _state.reporting = True
    pending = _queue
    if pending is None:
        return
    while True:
        item = pending.get()
        if item is None:
            return
        app, level, message, stack, category, extra = item
        try:
            result = ingest(app, level, message, stack=stack, category=category, extra=extra)
            if result.is_new or result.reopened or result.watched:
                on_new(app, message, result.fingerprint)
        except Exception as failure:  # noqa: BLE001 - the thread must outlive it
            _echo(f"[logs] could not record an error ({failure!r}): {message[:200]}")


def _echo(text: str) -> None:
    # sys.stderr is None under pythonw.exe (the agent's scheduled task).
    if sys.stderr is not None:
        with contextlib.suppress(Exception):
            print(text, file=sys.stderr)


# ── Wiring a process up ──────────────────────────────────────────────────────


def _extra_from(event_dict: dict[str, Any]) -> dict[str, str]:
    """What the log line knew, as short strings: the job, the stage, the rest."""
    skip = {"event", "level", "timestamp", "exc_info", "exception", "stack_info", "_record"}
    extra = {
        key: str(value)[:200]
        for key, value in event_dict.items()
        if key not in skip and value is not None
    }
    extra["host"] = socket.gethostname()
    extra["pid"] = str(os.getpid())
    return dict(list(extra.items())[:20])


def structlog_processor(app: str) -> Callable[[Any, str, dict[str, Any]], dict[str, Any]]:
    """A structlog processor that reports error lines and passes them on."""

    def process(_logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        if method_name in ("error", "exception", "critical") and not getattr(
            _state, "reporting", False
        ):
            exc_info = event_dict.get("exc_info")
            exc: BaseException | None = None
            if exc_info is True:
                exc = sys.exc_info()[1]
            elif isinstance(exc_info, BaseException):
                exc = exc_info
            elif isinstance(exc_info, tuple) and len(exc_info) == 3:
                exc = exc_info[1]
            message = str(event_dict.get("event", ""))
            detail = event_dict.get("error")
            if detail:
                message = f"{message}: {detail}"
            elif exc is not None:
                message = f"{message}: {type(exc).__name__}: {exc}"
            stack = (
                event_dict.get("exception")
                if isinstance(event_dict.get("exception"), str)
                else None
            )
            report(app, "error", message, exc=exc, stack=stack, extra=_extra_from(event_dict))
        return event_dict

    return process


class _RootHandler(logging.Handler):
    """Third-party libraries that log through the standard library."""

    def __init__(self, app: str) -> None:
        super().__init__(level=logging.ERROR)
        self.app = app

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(_IGNORED_LOGGERS):
            return
        try:
            exc = record.exc_info[1] if record.exc_info else None
            report(
                self.app,
                "error",
                record.getMessage(),
                exc=exc,
                extra={"logger": record.name, "where": f"{record.module}:{record.lineno}"},
            )
        except Exception:  # noqa: BLE001 - logging.Handler's contract
            self.handleError(record)


def install(
    app: str,
    ingest: Callable[..., Any],
    on_new: Callable[[str, str, str], None],
) -> None:
    """Wire this process up; safe to call twice. `ingest` is LogStore.ingest,
    `on_new(app, message, fingerprint)` raises the notification."""
    global _queue
    if _installed:
        return
    import structlog

    _queue = queue.Queue(maxsize=QUEUE_MAX)
    writer = threading.Thread(target=_writer, args=(ingest, on_new), name="log-writer", daemon=True)
    writer.start()

    config = structlog.get_config()
    processors = list(config["processors"])
    # Before the renderer, which turns the event into a string.
    processors.insert(max(len(processors) - 1, 0), structlog_processor(app))
    structlog.configure(processors=processors)

    handler = _RootHandler(app)
    logging.getLogger().addHandler(handler)

    previous_excepthook = sys.excepthook
    previous_thread_hook = threading.excepthook

    def excepthook(exc_type: Any, exc: Any, tb: Any) -> None:
        if not issubclass(exc_type, KeyboardInterrupt):
            report(
                app,
                "error",
                f"Uncaught: {exc_type.__name__}: {exc}",
                stack="".join(traceback.format_exception(exc_type, exc, tb)),
            )
        previous_excepthook(exc_type, exc, tb)

    def thread_hook(args: Any) -> None:
        if args.exc_type is not SystemExit and args.exc_value is not None:
            name = args.thread.name if args.thread else "?"
            report(
                app,
                "error",
                f"Thread {name} died: {args.exc_type.__name__}: {args.exc_value}",
                stack="".join(
                    traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
                ),
            )
        previous_thread_hook(args)

    sys.excepthook = excepthook
    threading.excepthook = thread_hook
    _installed.update(
        handler=handler,
        excepthook=previous_excepthook,
        thread_hook=previous_thread_hook,
        processors=config["processors"],
        writer=writer,
    )


def uninstall(timeout: float = 5.0) -> None:
    """Undo install(), after the queue drains; for tests and clean shutdowns."""
    global _queue
    if not _installed:
        return
    import structlog

    if _queue is not None:
        _queue.put(None)
        _installed["writer"].join(timeout=timeout)
    logging.getLogger().removeHandler(_installed["handler"])
    sys.excepthook = _installed["excepthook"]
    threading.excepthook = _installed["thread_hook"]
    structlog.configure(processors=_installed["processors"])
    _installed.clear()
    _queue = None
    with _recent_lock:
        _recent.clear()
