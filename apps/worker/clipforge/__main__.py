"""``python -m clipforge`` — the same CLI as the ``clipforge-worker`` script.

Worth having for one specific reason: Task Scheduler starts the agent with
``pythonw.exe``, which has no console and so never flashes a window at logon.
``pythonw.exe`` can be handed a module but not a console-script shim, so without
this file the autostart would have to go through a wrapper whose only job was to
find the shim — one more thing between the machine booting and the worker being
startable from a phone.

It also has to make the process survivable before anything else runs. Under
``pythonw.exe`` the standard streams are ``None``, and a surprising amount of
ordinary library code does not expect that: Rich builds a console from
``sys.stderr`` when Typer imports it, and structlog binds ``sys.stdout`` by name
at import time. Each fails in its own confusing way, none of which says "there is
nowhere to write". Substituting the null device costs nothing when the streams
exist, and the agent replaces them with its log file a moment later.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

for name, mode in (("stdin", "r"), ("stdout", "w"), ("stderr", "w")):
    if getattr(sys, name, None) is None:
        setattr(sys, name, Path(os.devnull).open(mode, encoding="utf-8"))  # noqa: SIM115

from clipforge.cli import app  # noqa: E402 - must follow the stream repair above

if __name__ == "__main__":
    app()
