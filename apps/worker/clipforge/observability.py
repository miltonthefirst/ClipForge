"""Structured logging with job and stage correlation.

Every log line emitted while a stage runs carries ``job_id``, ``stage`` and
``worker_id`` without the call site passing them. That is the difference between
"an error happened" and "an error happened in TRANSCRIBE on job abc123, on
worker desktop-01, attempt 2" — which is the only form that is any use when a
job failed overnight.

Two renderers: ``console`` for a human at a terminal, ``json`` for anything that
will be grepped or shipped. Set with ``CLIPFORGE_LOG_FORMAT``.
"""

from __future__ import annotations

import contextlib
import io
import logging
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

import structlog

__all__ = ["bind_job", "configure_logging", "get_logger", "open_log_file"]


def open_log_file(path: Path, *, max_bytes: int = 5 * 1024 * 1024) -> TextIO:
    """Open a log file for appending, rotating it first if it has grown too big.

    Rotation is one generation and a size check at open, which is all a file
    written only when something has already gone wrong needs.
    """
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.stat().st_size > max_bytes:
        with contextlib.suppress(OSError):
            previous = path.with_name(path.name + ".1")
            previous.unlink(missing_ok=True)
            path.rename(previous)
    return path.open("a", encoding="utf-8", buffering=1)


class _Fallback:
    """Where output goes once the stream it was meant for has gone away.

    One per process, shared by stdout and stderr, and opened only on the first
    failure — a worker whose console never goes away never creates the file.
    With no path, or a path that cannot be opened, output is discarded: losing a
    log line is the outcome this whole arrangement exists to make survivable.
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._stream: TextIO | None = None
        self._opened = False
        self._lock = threading.Lock()

    def lost(self, name: str) -> None:
        """Say, in the file, when and which stream stopped being read."""
        self.write(
            f"--- {datetime.now(UTC).isoformat()} {name} is no longer being read "
            f"(the process that started this one has probably exited); "
            f"continuing here ---\n"
        )

    def write(self, text: str) -> int:
        with self._lock:
            stream = self._open()
            if stream is not None:
                try:
                    stream.write(text)
                except (OSError, ValueError):
                    self._stream = None
        return len(text)

    def flush(self) -> None:
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.flush()
                except (OSError, ValueError):
                    self._stream = None

    def _open(self) -> TextIO | None:
        if not self._opened:
            self._opened = True
            if self._path is not None:
                with contextlib.suppress(OSError):
                    self._stream = open_log_file(self._path)
        return self._stream


class _SurvivingStream(io.TextIOBase):
    """A standard stream that outlives whatever was reading it.

    The worker is started with its output piped to a parent — the desktop app,
    or the agent — and nothing stops that parent exiting first. After that every
    write to the pipe raises ``OSError`` (``EINVAL`` on Windows, ``EPIPE``
    elsewhere), and a log call is a write. So every thread died at its next log
    line, silently, because the traceback went to the same dead stderr: job
    threads between claiming a job and starting its stage, the reaper, the
    handler that would have stopped the worker. The heartbeat, which logs only
    on failure, went on reporting a healthy worker the whole time. Observed on
    2026-09-26 as two jobs RUNNING for hours with leases long expired.

    A write that fails here moves this stream to the fallback for good, and
    reports success either way. Nothing that prints can be killed by printing.
    """

    def __init__(self, primary: TextIO | None, fallback: _Fallback, name: str) -> None:
        super().__init__()
        self._primary = primary
        self._fallback = fallback
        self._name = name

    @property
    def encoding(self) -> str:  # type: ignore[override]
        # Read by click, which rewraps a stream whose encoding it cannot see —
        # and would rewrap it around the dead pipe, undoing all of this.
        return getattr(self._primary, "encoding", None) or "utf-8"

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        try:
            return self._primary is not None and self._primary.isatty()
        except (OSError, ValueError):
            return False

    def fileno(self) -> int:
        if self._primary is None:
            raise io.UnsupportedOperation(f"{self._name} has no file descriptor")
        return self._primary.fileno()

    def write(self, text: str) -> int:
        if self._primary is not None:
            try:
                return self._primary.write(text)
            except (OSError, ValueError):
                self._abandon()
        return self._fallback.write(text)

    def flush(self) -> None:
        if self._primary is not None:
            try:
                self._primary.flush()
                return
            except (OSError, ValueError):
                self._abandon()
        self._fallback.flush()

    def _abandon(self) -> None:
        self._primary = None
        self._fallback.lost(self._name)


def _guard_standard_streams(fallback: Path | None) -> None:
    """Replace stdout and stderr with streams that cannot raise on write.

    Global rather than handed to the loggers, because the loggers are not the
    only writers: tracebacks, warnings, `typer.echo` and every library progress
    bar go to the same two streams. Idempotent, so a second call does not wrap a
    wrapper.
    """
    shared = _Fallback(fallback)
    for name in ("stdout", "stderr"):
        current = getattr(sys, name)
        if isinstance(current, _SurvivingStream):
            continue
        setattr(sys, name, _SurvivingStream(current, shared, name))


def configure_logging(
    *, level: str = "INFO", fmt: str = "console", fallback: Path | None = None
) -> None:
    """Configure structlog once, at worker startup.

    ``fallback`` is the file output moves to if stdout or stderr stops being
    read. With none, that output is dropped rather than raised.
    """
    _guard_standard_streams(fallback)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    if fmt == "json":
        processors += [
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
        processors += [structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


@contextmanager
def bind_job(**values: object) -> Iterator[None]:
    """Attach correlation fields to every log line inside the block.

    Uses context variables rather than a passed-around logger, so a helper five
    frames deep still logs with the right job id without taking a logger
    parameter it has no other use for. The tokens are reset on exit, so a stage
    cannot leak its identity into the scheduler's own logging.
    """
    tokens = structlog.contextvars.bind_contextvars(**values)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
