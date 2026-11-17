"""Running the music generator (packages/ngoma) from the worker.

ngoma lives in its own environment (ACE-Step's, torch 2.7 with CUDA 12.8) and
is run as a child process, one per job: ACE-Step's memory only grows inside a
long-lived process, and a process that exits gives the card back for certain.
The two talk through files: a request JSON in, WAVs and a manifest out, and
one JSON object per line on stdout while it works.

Before it starts, the card has to be free. The broker waits for that, but one
holder never lets go on its own: Sarungano's ComfyUI keeps its last model
loaded between jobs. When ComfyUI's queue is empty it is asked to unload, the
same call Sarungano makes before training (app/audio/voice_training.py
free_gpu); while ComfyUI is working, the song waits for it.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from clipforge.config import Settings
from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = [
    "NgomaError",
    "comfyui_busy",
    "free_comfyui",
    "ngoma_paths",
    "run_ngoma",
    "wait_for_comfyui",
]

REPO = Path(__file__).resolve().parents[4]


class NgomaError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = "NGOMA"


def ngoma_paths(settings: Settings) -> tuple[Path, Path, Path]:
    """(the generator script, the ACE-Step clone it runs in, that clone's Python)."""
    root = (
        Path(settings.ngoma_dir).expanduser() if settings.ngoma_dir else REPO / "packages" / "ngoma"
    )
    vendor = root / "vendor" / "ACE-Step-1.5"
    python = vendor / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return root / "generate.py", vendor, python


def comfyui_busy(url: str) -> bool | None:
    """True while ComfyUI has work queued or running; None when it is not there."""
    if not url:
        return None
    try:
        response = httpx.get(url.rstrip("/") + "/queue", timeout=5)
        queue = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    return bool(queue.get("queue_running") or queue.get("queue_pending"))


def free_comfyui(url: str) -> bool:
    """Ask ComfyUI to unload its models. True when it answered."""
    try:
        httpx.post(
            url.rstrip("/") + "/free", json={"unload_models": True, "free_memory": True}, timeout=30
        ).raise_for_status()
    except httpx.HTTPError:
        return False
    return True


def wait_for_comfyui(
    url: str,
    *,
    progress: Callable[[str], None],
    stopping: Callable[[], bool],
    patience_s: float = 1800.0,
    poll_s: float = 10.0,
) -> str:
    """Wait for ComfyUI to be idle, then have it let go of the card. Says what happened."""
    busy = comfyui_busy(url)
    if busy is None:
        return "ComfyUI is not running"
    deadline = time.monotonic() + patience_s
    told = False
    while busy:
        if stopping():
            raise NgomaError("stopped while waiting for ComfyUI", retryable=True)
        if time.monotonic() >= deadline:
            raise NgomaError(
                "ComfyUI (Sarungano) has been using the card for half an hour; the song will try again later",
                retryable=True,
            )
        if not told:
            progress("Waiting for Sarungano's ComfyUI to finish with the card")
            told = True
        time.sleep(poll_s)
        busy = comfyui_busy(url)
    freed = free_comfyui(url)
    log.info("ngoma.comfyui_freed", freed=freed)
    time.sleep(3)  # ComfyUI unloads on its own thread
    return "ComfyUI let go of the card" if freed else "ComfyUI did not answer"


def run_ngoma(
    settings: Settings,
    request: dict[str, Any],
    out: Path,
    *,
    on_event: Callable[[dict[str, Any]], None],
    stopping: Callable[[], bool],
) -> Path:
    """Run one request to the end; return its manifest. Raises NgomaError."""
    script, vendor, python = ngoma_paths(settings)
    if not script.is_file() or not python.is_file():
        raise NgomaError(
            f"the music generator is not installed here ({python} or {script} is missing): "
            "see packages/ngoma/README.md"
        )
    out.mkdir(parents=True, exist_ok=True)
    request_file = out / "request.json"
    request_file.write_text(json.dumps({**request, "out": str(out)}, indent=2), encoding="utf-8")
    log_file = out / "ngoma.log"

    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME")
    }
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with log_file.open("w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(  # noqa: S603 - fixed argv
            [str(python), str(script), str(request_file)],
            cwd=str(vendor),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=log_handle,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
        killed = threading.Event()

        def watchdog() -> None:
            deadline = time.monotonic() + settings.ngoma_timeout_s
            while process.poll() is None:
                if stopping() or time.monotonic() >= deadline:
                    killed.set()
                    process.kill()
                    return
                time.sleep(1)

        threading.Thread(target=watchdog, daemon=True).start()
        assert process.stdout is not None  # noqa: S101
        for line in process.stdout:
            line = line.strip()
            if not line.startswith("{"):
                log_handle.write(line + "\n")
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            log.info(
                "ngoma.event",
                **{k: v for k, v in event.items() if k != "event"},
                ngoma_event=event.get("event"),
            )
            on_event(event)
        code = process.wait()

    manifest = out / "manifest.json"
    if killed.is_set():
        if stopping():
            raise NgomaError("stopped while singing", retryable=True)
        raise NgomaError(
            f"the music generator ran past {settings.ngoma_timeout_s:.0f} s; "
            "something else was probably using the card. See " + str(log_file),
            retryable=True,
        )
    if code != 0 or not manifest.is_file():
        tail = log_file.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-5:]
        raise NgomaError(
            f"the music generator failed (exit {code}): " + " | ".join(tail)[:600], retryable=True
        )
    return manifest
