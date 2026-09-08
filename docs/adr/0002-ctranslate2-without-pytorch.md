# ADR-0002 — Run Whisper on CTranslate2, with no PyTorch dependency

- **Status:** Accepted
- **Date:** 2026-09-07
- **Phase:** 0

## Context

`docs/PLAN.md` Phase 0 originally specified a PyTorch install, with the exit
criterion `torch.cuda.is_available() is True`. That was carried over from the
assumption that local Whisper implies PyTorch.

It does not. `faster-whisper` is a CTranslate2 re-implementation of Whisper;
CTranslate2 is a standalone C++ inference engine with its own CUDA kernels and no
torch dependency whatsoever. Nothing else in the v0.1–v0.3 pipeline needs torch
either: the LLM runs out-of-process in Ollama, ffmpeg handles all media, and VAD
ships as ONNX inside faster-whisper.

Keeping torch would have cost roughly 2.5 GB of install, a CUDA-variant wheel
index that must be kept in step with the driver, and a recurring class of
"installed the CPU-only build by accident, everything is 20x slower" failures.

## Decision

The worker does not depend on PyTorch. Whisper runs on CTranslate2 via
`faster-whisper`, and VRAM accounting uses NVML directly through `nvidia-ml-py`
rather than `torch.cuda`.

Two consequences follow that are not obvious:

1. **Model unloading is `del model` plus refcount drop, not
   `torch.cuda.empty_cache()`.** `docs/PLAN.md` §2.1 has been corrected. The
   `ModelBroker` (Phase 2) must verify release through NVML, since there is no
   torch allocator to interrogate.

2. **CUDA shared libraries must be registered manually on Windows.** CTranslate2
   loads `cublas64_12.dll` and `cudnn64_9.dll` by bare filename. The pip packages
   `nvidia-cublas-cu12` and `nvidia-cudnn-cu12` place them in
   `site-packages/nvidia/<component>/bin`, which Windows does not search. This
   was confirmed empirically during Phase 0 — the first CUDA transcription failed
   with `Library cublas64_12.dll is not found or cannot be loaded`.
   `clipforge.models.cuda` fixes it with `os.add_dll_directory()` and must run
   before any `faster_whisper` import.

## Consequences

- `uv sync` (core) installs in seconds and works on any machine, GPU or not,
  which is what lets CI run the `unit` and `integration` tiers on plain runners.
- GPU dependencies are isolated in the `gpu` extra and imported lazily.
- Phase 0's exit criterion changed from a torch probe to a real five-second CUDA
  transcription (`clipforge.diagnostics.smoke_transcribe`). This is a strictly
  better check: it exercises the actual code path, including the DLL resolution
  above, which a torch probe would not have caught at all.
- If a future phase needs torch — a torch-only diarisation model, say — this ADR
  must be superseded rather than quietly amended.

## Alternatives considered

- **`openai-whisper`** (the reference implementation). Requires torch, is
  markedly slower, and offers no word-level timestamps without extra tooling.
  Word-level timestamps are load-bearing for Phases 5 and 6.
- **`whisper.cpp`.** Excellent and dependency-light, but the Python bindings are
  less mature and its CUDA support is weaker than CTranslate2's on Windows.
- **Keeping torch anyway, for future flexibility.** Paying 2.5 GB and a
  wheel-matching problem now against a speculative future need is the wrong
  trade. Adding it later is a one-line change to `pyproject.toml`.
