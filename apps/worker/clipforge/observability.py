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

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog

__all__ = ["bind_job", "configure_logging", "get_logger"]


def configure_logging(*, level: str = "INFO", fmt: str = "console") -> None:
    """Configure structlog once, at worker startup."""
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
