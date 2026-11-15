"""Python port of log-fingerprint.ts, for Python backends (since 1.2.0).

It must give the same fingerprint as the TypeScript version for the same
input, so a group means the same thing whichever side computed it: re.ASCII
makes \\d and \\b match only ASCII, as JavaScript's do. Copy it verbatim.
Origin: sarungano's app/logs/fingerprint.py.
"""

from __future__ import annotations

import hashlib
import re

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_ISO_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?", re.ASCII)
_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b", re.ASCII)
_NUMBER = re.compile(r"\b\d+\b", re.ASCII)


def normalize_log_text(text: str) -> str:
    """Strips what varies between occurrences of the same problem (ids,
    timestamps, addresses, counts) before hashing."""
    text = _UUID.sub("<uuid>", text)
    text = _ISO_TS.sub("<ts>", text)
    text = _HEX.sub("<hex>", text)
    text = _NUMBER.sub("<n>", text)
    return text.strip()


def is_python_traceback(stack: str) -> bool:
    return stack.lstrip().startswith("Traceback") or '\n  File "' in stack


def top_stack_frame(stack: str | None) -> str:
    """The innermost frame: a JS stack's second line, a Python traceback's
    last 'File "..."' line (it runs outermost call first)."""
    if not stack:
        return ""
    lines = [line.strip() for line in stack.split("\n") if line.strip()]
    if is_python_traceback(stack):
        frames = [line for line in lines if line.startswith('File "')]
        frame = frames[-1] if frames else (lines[-1] if lines else "")
    else:
        frame = lines[1] if len(lines) > 1 else (lines[0] if lines else "")
    return normalize_log_text(frame)[:200]


def compute_log_fingerprint(level: str, app: str, message: str, stack: str | None = None) -> str:
    raw = f"{level}|{app}|{normalize_log_text(message)}|{top_stack_frame(stack)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
