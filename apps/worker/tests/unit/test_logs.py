"""Capturing the worker's and the agent's errors (the logging blueprint)."""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from clipforge.logs import capture
from clipforge.logs.fingerprint import compute_log_fingerprint
from clipforge.logs.store import Ingested

pytestmark = pytest.mark.unit


class Sink:
    def __init__(self) -> None:
        self.ingested: list[dict[str, Any]] = []
        self.announced: list[str] = []
        self._seen: set[str] = set()

    def ingest(self, app: str, level: str, message: str, **kwargs: Any) -> Ingested:
        self.ingested.append({"app": app, "level": level, "message": message, **kwargs})
        fp = compute_log_fingerprint(level, app, message, kwargs.get("stack"))
        new = fp not in self._seen
        self._seen.add(fp)
        return Ingested(fp, new, False, False, 1)

    def on_new(self, app: str, message: str, fingerprint: str) -> None:
        self.announced.append(fingerprint)


@pytest.fixture
def sink() -> Iterator[Sink]:
    structlog.reset_defaults()
    structlog.configure(processors=[structlog.processors.KeyValueRenderer()])
    target = Sink()
    capture.install("worker", target.ingest, target.on_new)
    yield target
    capture.uninstall()
    structlog.reset_defaults()


def settle(sink: Sink, count: int) -> None:
    deadline = time.monotonic() + 5
    while len(sink.ingested) < count and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)


def test_an_error_line_reaches_the_logs_with_what_it_knew(sink: Sink) -> None:
    structlog.get_logger().error(
        "stage.failed", error="ffmpeg exited 1", job_id="j1", stage="RENDER"
    )
    settle(sink, 1)

    entry = sink.ingested[0]
    assert entry["message"] == "stage.failed: ffmpeg exited 1"
    assert entry["extra"]["job_id"] == "j1"
    assert entry["extra"]["stage"] == "RENDER"


def test_an_exception_brings_its_traceback(sink: Sink) -> None:
    try:
        raise ValueError("bad clip")
    except ValueError:
        structlog.get_logger().exception("job.crashed")
    settle(sink, 1)

    assert "ValueError: bad clip" in sink.ingested[0]["message"]
    assert "Traceback" in (sink.ingested[0]["stack"] or "")


def test_warnings_and_info_are_not_captured(sink: Sink) -> None:
    structlog.get_logger().warning("agent.publish_failed")
    structlog.get_logger().info("job.completed")
    settle(sink, 0)
    assert sink.ingested == []


def test_a_machine_that_could_not_be_reached_is_not_an_error(sink: Sink) -> None:
    try:
        raise ConnectionRefusedError("connection refused")
    except ConnectionRefusedError:
        structlog.get_logger().exception("youtube.upload")
    settle(sink, 0)
    assert sink.ingested == []


def test_a_new_group_is_announced_and_a_repeat_is_not(sink: Sink) -> None:
    for _ in range(3):
        structlog.get_logger().error("disk.full", error="P: has 0 bytes free")
    settle(sink, 3)
    assert len(sink.announced) == 1


def test_a_loop_cannot_flood_the_logs(sink: Sink) -> None:
    for n in range(capture.PER_MINUTE + 20):
        structlog.get_logger().error("loop", error=f"attempt {n}")
    settle(sink, capture.PER_MINUTE)
    assert len(sink.ingested) == capture.PER_MINUTE


@pytest.mark.parametrize(
    ("message", "exc", "category"),
    [
        ("upload failed", ConnectionResetError("reset"), "network"),
        ("getaddrinfo failed for youtube.googleapis.com", None, "network"),
        ("could not write the clip", PermissionError("Access is denied"), "permission-denied"),
        # Failures of this machine's own work, not of reaching another.
        ("local model unavailable", None, None),
        ("ffmpeg timed out", None, None),
        ("bad DNS", socket.gaierror("x"), "network"),
    ],
)
def test_classifying(message: str, exc: BaseException | None, category: str | None) -> None:
    assert capture.classify(message, exc) == category


def test_without_install_reporting_is_a_quiet_no(sink: Sink) -> None:
    capture.uninstall()
    assert capture.report("worker", "error", "nobody listening") is False
