"""Generate the Pydantic half of the contracts from schemas/clipforge.json.

Run with the worker's environment, which is where datamodel-code-generator lives:

    uv run --project apps/worker python packages/contracts/scripts/generate_python.py
    uv run --project apps/worker python packages/contracts/scripts/generate_python.py --check

``--check`` regenerates into a temporary directory and compares. CI runs it, which
is what stops a schema edit from landing without its regenerated models
(docs/PLAN.md Phase 1, exit criterion 4).

The ``__init__.py`` is generated too, with explicit ``X as X`` re-exports. That form
matters: mypy's strict mode sets ``no_implicit_reexport``, so a consumer writing
``from clipforge_contracts import Job`` would fail against a plain star-import.
Generating it also means a new schema definition becomes importable without anyone
remembering to add it by hand.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess  # noqa: S404 - invoking our own pinned generator, no user input
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas" / "clipforge.json"
PKG_DIR = ROOT / "generated" / "python" / "clipforge_contracts"

BANNER = """\
# ClipForge contracts - GENERATED FILE, DO NOT EDIT.
#
# Source of truth: packages/contracts/schemas/clipforge.json
# Regenerate with:
#   uv run --project apps/worker python packages/contracts/scripts/generate_python.py
#
# Editing this file by hand is pointless: CI regenerates it and fails on any
# difference. Change the schema instead.
"""


def _exported_names() -> list[str]:
    """Every type the schema defines, plus the root registry object."""
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    names = [schema["title"], *schema["definitions"].keys()]
    return sorted(set(names))


def _render_init(names: list[str]) -> str:
    lines = [BANNER, '"""ClipForge wire protocol, generated from JSON Schema."""', ""]
    lines.append("from clipforge_contracts.models import (")
    lines.extend(f"    {name} as {name}," for name in names)
    lines.append(")")
    lines.append("")
    lines.append("__all__ = [")
    lines.extend(f'    "{name}",' for name in names)
    lines.append("]")
    lines.append("")
    return "\n".join(lines)


def _generate_into(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    models = target / "models.py"

    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "datamodel_code_generator",
            "--input",
            str(SCHEMA),
            "--input-file-type",
            "jsonschema",
            "--output",
            str(models),
            "--output-model-type",
            "pydantic_v2.BaseModel",
            "--target-python-version",
            "3.12",
            "--use-double-quotes",
            "--use-standard-collections",
            "--use-union-operator",
            "--use-schema-description",
            "--use-field-description",
            "--field-constraints",
            "--snake-case-field",
            # Fields are snake_case in Python and camelCase on the wire. Without
            # this, Python code would be forced to construct models by their
            # camelCase alias — `Job(sourceId=...)` — which is exactly the kind
            # of leak the contracts package exists to prevent. With it, Python
            # uses snake_case and `model_dump(by_alias=True)` emits the wire form.
            "--allow-population-by-field-name",
            # --snake-case-field also lowercases enum *members*, which would give
            # `JobStatus.queued`. The values are unaffected either way, but the
            # members are referenced constantly in worker code and UPPER_CASE is
            # what a Python reader expects of an enum.
            "--capitalise-enum-members",
            "--disable-timestamp",
            # Pinned rather than left to the default. datamodel-code-generator
            # warns that its default formatters will change in a future release,
            # and a formatter change would show up here as a spurious staleness
            # failure on an unrelated PR.
            "--formatters",
            "black",
            "isort",
            "--custom-file-header",
            BANNER.rstrip("\n"),
        ],
        check=True,
    )

    # Normalise to LF. .gitattributes checks every generated file out as LF on
    # all platforms, but Python's text mode writes CRLF on Windows — so without
    # this the generator leaves a tree that looks dirty on Windows and clean on
    # Linux. (The --check comparison itself is unaffected either way, because
    # read_text() translates line endings on the way in.)
    models.write_text(models.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    (target / "__init__.py").write_text(
        _render_init(_exported_names()), encoding="utf-8", newline="\n"
    )
    (target / "py.typed").write_text("", encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify the committed output is current")
    args = parser.parse_args()

    if not args.check:
        _generate_into(PKG_DIR)
        print(f"Wrote {PKG_DIR}")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp) / "clipforge_contracts"
        _generate_into(fresh)

        stale: list[str] = []
        for name in ("models.py", "__init__.py"):
            want = (fresh / name).read_text(encoding="utf-8")
            got_path = PKG_DIR / name
            got = got_path.read_text(encoding="utf-8") if got_path.exists() else None
            if got != want:
                stale.append(name)

        if stale:
            print(
                "FAIL: generated Pydantic contracts are stale: "
                + ", ".join(stale)
                + "\n      The schema changed but the generated output was not regenerated."
                "\n      Run: uv run --project apps/worker python"
                " packages/contracts/scripts/generate_python.py",
                file=sys.stderr,
            )
            return 1

    print("PASS: generated Pydantic contracts are up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
