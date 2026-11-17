# ngoma

*Ngoma* is Shona for "drum" (a working name). This package will make music on
the local GPU with ACE-Step 1.5 (MIT code and weights): beds under ClipForge's
clips, and tracks of their own. It is built to be lifted out like Sarungano's
`izwi` and `ona`. It imports nothing from `clipforge`, and Sarungano's chapter
videos are its intended second user.

**Status:** `generate.py` is the generator ClipForge's SING stage runs
(`docs/MUSIC-PLAN.md`, M2 and M5): a request JSON in, WAVs and a manifest out,
one process per request. `spike/` holds the M1 measurements.

## Layout

| Path | What | In git |
|---|---|---|
| `generate.py` | The generator: `uv run python ../../generate.py request.json` from the clone. Instrumental or with lyrics; progress as JSON lines on stdout | yes |
| `spike/measure.py` | The M1 measurements | yes |
| `vendor/ACE-Step-1.5/` | The upstream clone, pinned at `ca1e85f` (2026-08-29), with its own `.venv` (Python 3.11 or 3.12, torch 2.7.1+cu128) | no |
| `checkpoints/` | `vae`, `Qwen3-Embedding-0.6B` and `acestep-v15-turbo` from `ACE-Step/Ace-Step1.5`. The 1.7B LM is left out because the 6 GB tier runs without it | no |
| `out/` | Generated audio and `measurements.jsonl` | no |

## Re-creating it

```sh
cd packages/ngoma
git clone https://github.com/ACE-Step/ACE-Step-1.5.git vendor/ACE-Step-1.5
git -C vendor/ACE-Step-1.5 checkout ca1e85fe9430179831e6bc6be790c332190a3866
cd vendor/ACE-Step-1.5 && uv sync
uv run python -c "from huggingface_hub import snapshot_download; snapshot_download('ACE-Step/Ace-Step1.5', local_dir='../../checkpoints', allow_patterns=['vae/*','Qwen3-Embedding-0.6B/*','acestep-v15-turbo/*','*.json','*.md'])"
uv run --with nvidia-ml-py --with psutil python ../../spike/measure.py --mode int8 --seconds 30 60 180
```

Run the measurements only while nothing else is using the GPU: ClipForge's
worker, Ollama, and Sarungano's ComfyUI all share the card.
