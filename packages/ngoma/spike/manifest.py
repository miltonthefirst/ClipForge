"""Write the manifest ClipForge imports, from what measure.py made.

    python packages/ngoma/spike/manifest.py
    clipforge-worker import-tracks packages/ngoma/out/manifest.json

Standard library only, so it runs from any Python. The manifest is the boundary
between this package and ClipForge (docs/MUSIC-PLAN.md): files plus what each
one is, and nothing that would make ClipForge import ngoma or ngoma import
ClipForge.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from measure import OUT, PROMPTS

MODEL = "ACE-Step 1.5, acestep-v15-turbo, INT8 (commit ca1e85f)"
LICENCE = "MIT (code and weights); the model card permits commercial use of the output"
TITLES = {
    "lofi": "Lo-fi",
    "cinematic": "Cinematic",
    "electronic": "Electronic",
    "acoustic": "Acoustic",
    "hiphop": "Hip-hop",
}


def main() -> None:
    entries: dict[str, dict[str, object]] = {}
    for line in (OUT / "measurements.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if not record.get("ok") or not record.get("files"):
            continue
        path = Path(record["files"][0])
        if not path.is_file():
            continue
        name = record["prompt"]
        seconds = record["seconds"]
        created = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
        entries[str(path)] = {
            "file": str(path),
            "title": f"{TITLES.get(name, name)} ({seconds} s)",
            "prompt": PROMPTS[name],
            "seconds": seconds,
            "instrumental": True,
            "seed": 1234,
            "steps": record.get("steps"),
            "model": MODEL,
            "licence": LICENCE,
            "generateSeconds": record.get("generateSeconds"),
            "createdAt": created,
        }
    manifest = OUT / "manifest.json"
    manifest.write_text(json.dumps({"tracks": list(entries.values())}, indent=2), encoding="utf-8")
    print(f"{len(entries)} tracks -> {manifest}")


if __name__ == "__main__":
    main()
