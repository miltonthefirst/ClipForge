"""MUSIC-PLAN.md phase M1: can ACE-Step 1.5 make usable music on the RTX 3050?

Run from the ACE-Step clone's environment:

    cd packages/ngoma/vendor/ACE-Step-1.5
    uv run python ../../spike/measure.py --mode int8 --seconds 30 60 180

Each run loads the 2B turbo DiT only (no 5Hz LM: the 6 GB tier has none),
generates one instrumental per length per prompt, and appends one JSON line
per track to packages/ngoma/out/measurements.jsonl:

- torch's own peak allocation for this process (exact, ours alone);
- the card's total use at its peak, sampled by NVML every 50 ms (includes
  whatever else is on the card, e.g. Ollama or ComfyUI; the baseline before
  loading is recorded so the difference can be read);
- load time, generation time, and this process's peak RAM.

Modes:
- int8:    INT8 weight-only + compile + full CPU offload (the docs' 6 GB tier).
- offload: no quantisation, full CPU offload, DiT offloaded between steps.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PACKAGE = HERE.parent
OUT = PACKAGE / "out"

PROMPTS = {
    "lofi": "warm lo-fi hip hop, mellow Rhodes piano, soft vinyl crackle, laid-back drums, 80 bpm, instrumental",
    "cinematic": "uplifting cinematic build, strings and piano, steady pulse, hopeful, instrumental",
    "electronic": "bright upbeat electronic, punchy synth bass, four-on-the-floor kick, energetic, instrumental",
    "acoustic": "gentle acoustic guitar fingerpicking, light percussion, calm and bright, instrumental",
    "hiphop": "confident boom bap beat, deep bass, crisp snare, jazzy sample-style chords, instrumental",
}


class GpuSampler:
    """The card's used memory, sampled until stopped; its peak is kept."""

    def __init__(self, every: float = 0.05):
        import pynvml

        pynvml.nvmlInit()
        self._nvml = pynvml
        self._handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        self._every = every
        self._stop = threading.Event()
        self.peak_mib = 0
        self._thread: threading.Thread | None = None

    def used_mib(self) -> int:
        return self._nvml.nvmlDeviceGetMemoryInfo(self._handle).used // (1024 * 1024)

    def __enter__(self):
        self.peak_mib = self.used_mib()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            self.peak_mib = max(self.peak_mib, self.used_mib())
            time.sleep(self._every)

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join()


def peak_ram_mib() -> int:
    import psutil

    info = psutil.Process().memory_info()
    return int(getattr(info, "peak_wset", info.rss) // (1024 * 1024))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["int8", "offload"], default="int8")
    parser.add_argument("--seconds", type=int, nargs="+", default=[30, 60, 180])
    parser.add_argument("--prompts", nargs="+", default=list(PROMPTS))
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--steps", type=int, default=8)
    args = parser.parse_args()

    os.environ.setdefault("ACESTEP_CHECKPOINTS_DIR", str(PACKAGE / "checkpoints"))
    # Nothing is downloaded during a measurement: a missing file must fail
    # loudly, not start a multi-GB fetch that looks like a hung generation.
    os.environ["HF_HUB_OFFLINE"] = "1"
    OUT.mkdir(exist_ok=True)

    import torch

    # ACE-Step counts the 1.7B LM as part of the "main model" and fetches the
    # whole 10 GB repository when its weights are absent. The 6 GB tier runs
    # without the LM, so only the three parts it loads are required here.
    import acestep.core.generation.handler.init_service_downloads as downloads
    from acestep.model_downloader import _contains_model_weights

    def dit_tier_present(checkpoints_dir: Any = None) -> bool:
        root = Path(checkpoints_dir or os.environ["ACESTEP_CHECKPOINTS_DIR"])
        parts = ("acestep-v15-turbo", "vae", "Qwen3-Embedding-0.6B")
        return all(_contains_model_weights(root / part) for part in parts)

    downloads.check_main_model_exists = dit_tier_present

    from acestep.handler import AceStepHandler
    from acestep.inference import GenerationConfig, GenerationParams, generate_music

    sampler = GpuSampler()
    baseline = sampler.used_mib()
    record_base = {
        "mode": args.mode,
        "gpu": torch.cuda.get_device_name(0),
        "baselineCardMiB": baseline,
        "torch": torch.__version__,
    }
    print(json.dumps({"event": "start", **record_base}), flush=True)

    handler = AceStepHandler()
    started = time.perf_counter()
    with sampler:
        status, ok = handler.initialize_service(
            project_root=str(PACKAGE),
            config_path="acestep-v15-turbo",
            device="cuda",
            compile_model=args.mode == "int8",
            quantization="int8_weight_only" if args.mode == "int8" else None,
            offload_to_cpu=True,
            offload_dit_to_cpu=True,
        )
    load_s = time.perf_counter() - started
    print(json.dumps({"event": "loaded", "ok": ok, "loadSeconds": round(load_s, 1),
                      "cardPeakMiB": sampler.peak_mib, "status": str(status)[-400:]}), flush=True)
    if not ok:
        return 2

    results = OUT / "measurements.jsonl"
    for seconds in args.seconds:
        for name in args.prompts:
            torch.cuda.reset_peak_memory_stats()
            params = GenerationParams(
                caption=PROMPTS[name],
                lyrics="[Instrumental]",
                instrumental=True,
                duration=float(seconds),
                seed=args.seed,
                inference_steps=args.steps,
                shift=3.0,
                thinking=False,
                use_cot_caption=False,
                use_cot_language=False,
                use_cot_metas=False,
            )
            config = GenerationConfig(batch_size=1, audio_format="wav", use_random_seed=False, seeds=[args.seed])
            save_dir = OUT / f"{args.mode}-{seconds}s"
            started = time.perf_counter()
            with sampler:
                result = generate_music(handler, None, params, config, save_dir=str(save_dir))
            gen_s = time.perf_counter() - started
            line = {
                **record_base,
                "prompt": name,
                "seconds": seconds,
                "steps": args.steps,
                "ok": bool(result.success),
                "error": None if result.success else str(result.error)[-400:],
                "generateSeconds": round(gen_s, 1),
                "realtimeFactor": round(seconds / gen_s, 2) if gen_s else None,
                "torchPeakMiB": torch.cuda.max_memory_allocated() // (1024 * 1024),
                "cardPeakMiB": sampler.peak_mib,
                "peakRamMiB": peak_ram_mib(),
                "files": [a.get("path") for a in (result.audios or [])] if result.success else [],
            }
            print(json.dumps(line), flush=True)
            with results.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
