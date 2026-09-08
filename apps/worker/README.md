# ClipForge Worker

The local half of ClipForge. Claims jobs from Firestore, then runs the pipeline
— download, transcribe, analyse, render, publish — on local hardware.

See [`docs/PLAN.md`](../../docs/PLAN.md) for the architecture, and the repository
[`README.md`](../../README.md) for setup.

## Quick reference

```bash
uv sync                      # core deps only (what CI installs)
uv sync --extra gpu          # + faster-whisper / CTranslate2 CUDA runtime
uv sync --extra gpu --extra media   # + yt-dlp

uv run pytest -m "not gpu"   # the tiers CI runs
uv run pytest -m gpu         # real hardware, opt-in
uv run ruff check .
uv run mypy
uv run python -m clipforge.diagnostics
```

## Why there is no PyTorch here

`faster-whisper` runs on CTranslate2, not PyTorch. Adding torch would cost
~2.5 GB and a class of CUDA wheel-matching problems for no benefit. VRAM
accounting uses NVML directly. See
[`docs/adr/0002-ctranslate2-without-pytorch.md`](../../docs/adr/0002-ctranslate2-without-pytorch.md).
