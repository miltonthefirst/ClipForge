"""Output that outlives whatever was reading it.

A live failure, reproduced. The desktop app started a worker with its output
piped to itself, then closed. From then on every write to that pipe raised, and
a log call is a write: every thread in the worker died at its next log line,
silently, because the traceback went to the same dead stderr. Two jobs sat
RUNNING for hours behind expired leases while the heartbeat — which logs only on
failure — went on reporting a healthy worker.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from clipforge.observability import _Fallback, _SurvivingStream

pytestmark = pytest.mark.unit


class DeadPipe:
    """A stream whose reader has gone: what Windows reports is EINVAL."""

    encoding = "utf-8"

    def write(self, _text: str) -> int:
        raise OSError(22, "Invalid argument")

    def flush(self) -> None:
        raise OSError(22, "Invalid argument")

    def isatty(self) -> bool:
        return False


class Pipe:
    """A stream somebody is still reading."""

    encoding = "utf-8"

    def __init__(self) -> None:
        self.written: list[str] = []

    def write(self, text: str) -> int:
        self.written.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def test_a_healthy_stream_is_written_straight_through(tmp_path: Path) -> None:
    """And the fallback file is never created: it exists only when needed."""
    pipe = Pipe()
    stream = _SurvivingStream(pipe, _Fallback(tmp_path / "worker.log"), "stdout")  # type: ignore[arg-type]

    stream.write("hello\n")
    stream.flush()

    assert pipe.written == ["hello\n"]
    assert not (tmp_path / "worker.log").exists()


def test_a_write_to_a_dead_stream_moves_to_the_fallback(tmp_path: Path) -> None:
    """The failing line is not lost, and the file says what happened."""
    log = tmp_path / "worker.log"
    stream = _SurvivingStream(DeadPipe(), _Fallback(log), "stdout")  # type: ignore[arg-type]

    assert stream.write("first line\n") == len("first line\n")
    stream.write("second line\n")
    stream.flush()

    text = log.read_text(encoding="utf-8")
    assert "stdout is no longer being read" in text
    assert text.index("first line") < text.index("second line")


def test_a_flush_to_a_dead_stream_does_not_raise(tmp_path: Path) -> None:
    """Buffered output fails at the flush rather than the write."""
    stream = _SurvivingStream(DeadPipe(), _Fallback(tmp_path / "worker.log"), "stderr")  # type: ignore[arg-type]

    stream.flush()


def test_with_nowhere_to_fall_back_to_output_is_dropped_not_raised() -> None:
    stream = _SurvivingStream(DeadPipe(), _Fallback(None), "stdout")  # type: ignore[arg-type]

    assert stream.write("gone\n") == len("gone\n")
    stream.flush()


def test_stdout_and_stderr_share_one_fallback_file(tmp_path: Path) -> None:
    log = tmp_path / "worker.log"
    shared = _Fallback(log)
    out = _SurvivingStream(DeadPipe(), shared, "stdout")  # type: ignore[arg-type]
    err = _SurvivingStream(DeadPipe(), shared, "stderr")  # type: ignore[arg-type]

    out.write("a log line\n")
    err.write("a traceback\n")
    out.flush()

    text = log.read_text(encoding="utf-8")
    assert "a log line" in text
    assert "a traceback" in text


CHILD = textwrap.dedent(
    """
    import sys
    from pathlib import Path

    from clipforge.observability import configure_logging, get_logger

    configure_logging(fallback=Path(sys.argv[1]))
    log = get_logger("child")

    # Wait until the parent has let go of both output pipes.
    sys.stdin.read()

    log.info("logged.after.the.parent.left")
    print("printed after the parent left")
    try:
        raise RuntimeError("a traceback after the parent left")
    except RuntimeError:
        log.exception("child.failed")
    """
)


def test_logging_survives_the_parent_closing_its_end_of_the_pipe(tmp_path: Path) -> None:
    """The failure end to end: a real pipe, a real reader walking away."""
    log = tmp_path / "worker.log"
    child = subprocess.Popen(  # noqa: S603 - our own interpreter, our own script
        [sys.executable, "-c", CHILD, str(log)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    assert child.stdout is not None
    assert child.stderr is not None
    assert child.stdin is not None
    child.stdout.close()
    child.stderr.close()
    child.stdin.close()

    assert child.wait(timeout=60) == 0

    text = log.read_text(encoding="utf-8")
    assert "logged.after.the.parent.left" in text
    assert "printed after the parent left" in text
    assert "a traceback after the parent left" in text
