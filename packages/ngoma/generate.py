"""Make tracks from a request file, and say what was made in a manifest.

    cd packages/ngoma/vendor/ACE-Step-1.5
    uv run python ../../generate.py request.json

The request is JSON:

    {"out": "<dir>", "steps": 8,
     "tracks": [{"title": "...", "series": "...", "caption": "...",
                 "lyrics": "[Verse]\\n...", "instrumental": false,
                 "seconds": 150, "seed": 7, "bpm": 100, "keyscale": "A minor",
                 "vocalLanguage": "en", "inspiration": "..."}]}

Each track is written to ``<out>/<n>-<seed>.wav`` and appended to
``<out>/manifest.json`` as soon as it exists, so a failure part-way keeps what
was made. The manifest is the format ClipForge's ``import-tracks`` reads
(docs/MUSIC-PLAN.md): this file imports nothing from ClipForge.

One process per request, on purpose: ACE-Step's RAM grows with every track in
a long-lived process (eleven in one run ended in a segfault in M1), and a fresh
process returns all of it. Progress goes to stdout as one JSON object per line.

Runs the 6 GB tier: the 2B turbo DiT only, INT8 weights, compiled, offloaded to
the CPU between uses. No 5 Hz LM, so nothing rewrites the caption or guesses
the tempo: the request says it.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
MODEL = "ACE-Step 1.5, acestep-v15-turbo, INT8 (commit ca1e85f)"
LICENCE = "MIT (code and weights); the model card permits commercial use of the output"


def emit(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}), flush=True)


def load_handler(checkpoints: Path):
    os.environ.setdefault("ACESTEP_CHECKPOINTS_DIR", str(checkpoints))
    # Nothing is downloaded here: a missing file must fail loudly, not start a
    # multi-GB fetch that looks like a hung generation.
    os.environ["HF_HUB_OFFLINE"] = "1"

    import acestep.core.generation.handler.init_service_downloads as downloads
    from acestep.model_downloader import _contains_model_weights

    def dit_tier_present(checkpoints_dir: str | None = None) -> bool:
        root = Path(checkpoints_dir or os.environ["ACESTEP_CHECKPOINTS_DIR"])
        parts = ("acestep-v15-turbo", "vae", "Qwen3-Embedding-0.6B")
        return all(_contains_model_weights(root / part) for part in parts)

    # The upstream check also wants the 1.7B LM, which the 6 GB tier leaves out.
    downloads.check_main_model_exists = dit_tier_present

    from acestep.handler import AceStepHandler

    handler = AceStepHandler()
    status, ok = handler.initialize_service(
        project_root=str(HERE),
        config_path="acestep-v15-turbo",
        device="cuda",
        compile_model=True,
        quantization="int8_weight_only",
        offload_to_cpu=True,
        offload_dit_to_cpu=True,
    )
    if not ok:
        raise RuntimeError(f"ACE-Step did not start: {status}")
    return handler


def make(handler, track: dict[str, Any], steps: int, out: Path, index: int) -> dict[str, Any]:
    from acestep.inference import GenerationConfig, GenerationParams, generate_music

    instrumental = bool(track.get("instrumental", False))
    lyrics = "[Instrumental]" if instrumental else str(track["lyrics"])
    seed = int(track.get("seed", 1234))
    seconds = float(track["seconds"])
    params = GenerationParams(
        caption=str(track["caption"])[:512],
        lyrics=lyrics[:4096],
        instrumental=instrumental,
        duration=seconds,
        seed=seed,
        bpm=track.get("bpm"),
        keyscale=str(track.get("keyscale") or ""),
        vocal_language=str(track.get("vocalLanguage") or ("unknown" if instrumental else "en")),
        inference_steps=steps,
        shift=3.0,
        thinking=False,
        use_cot_caption=False,
        use_cot_language=False,
        use_cot_metas=False,
    )
    config = GenerationConfig(batch_size=1, audio_format="wav", use_random_seed=False, seeds=[seed])
    work = out / f"work-{index}"
    work.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    result = generate_music(handler, None, params, config, save_dir=str(work))
    took = round(time.perf_counter() - started, 1)
    if not result.success or not result.audios:
        raise RuntimeError(f"generation failed: {getattr(result, 'error', None) or getattr(result, 'status_message', '')}")
    produced = Path(result.audios[0]["path"])
    final = out / f"{index:02d}-{seed}.wav"
    produced.replace(final)
    return {
        "file": final.name,
        "title": str(track.get("title") or track["caption"])[:120],
        "series": track.get("series"),
        "prompt": str(track["caption"])[:512],
        "lyrics": None if instrumental else lyrics,
        "inspiration": track.get("inspiration"),
        "seconds": seconds,
        "bpm": track.get("bpm"),
        "key": track.get("keyscale"),
        "instrumental": instrumental,
        "seed": seed,
        "steps": steps,
        "model": MODEL,
        "licence": LICENCE,
        "generateSeconds": took,
        "createdAt": datetime.now(UTC).isoformat(),
    }


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(request["out"]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    steps = int(request.get("steps", 8))
    tracks = request["tracks"]
    manifest = out / "manifest.json"
    made: list[dict[str, Any]] = []

    emit("loading", tracks=len(tracks))
    started = time.perf_counter()
    handler = load_handler(Path(request.get("checkpoints") or HERE / "checkpoints"))
    emit("loaded", seconds=round(time.perf_counter() - started, 1))

    failed = 0
    for index, track in enumerate(tracks, start=1):
        emit("track", index=index, of=len(tracks), title=track.get("title"))
        try:
            entry = make(handler, track, steps, out, index)
        except Exception as exc:  # one bad track should not lose the others
            failed += 1
            emit("failed", index=index, error=f"{type(exc).__name__}: {exc}"[:500])
            continue
        made.append(entry)
        manifest.write_text(json.dumps({"tracks": made}, indent=2), encoding="utf-8")
        emit("made", index=index, file=entry["file"], seconds=entry["generateSeconds"])

    emit("done", made=len(made), failed=failed, manifest=str(manifest))
    return 0 if made else 1


if __name__ == "__main__":
    sys.exit(main())
