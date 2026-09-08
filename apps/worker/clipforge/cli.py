"""Worker command-line entrypoint.

Phase 0 ships only ``version``; the run loop arrives in Phase 2.
"""

from __future__ import annotations

import typer

from clipforge.version import __version__

app = typer.Typer(
    name="clipforge-worker",
    help="ClipForge local worker.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def version() -> None:
    """Print the worker version and exit."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
