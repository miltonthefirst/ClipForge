"""Painting a picture with the local image model, through ComfyUI.

ComfyUI is Sarungano's, already running on this PC (``comfyui_url``), with
FLUX.2 klein 4B (Apache 2.0) loaded as fp8 so it fits the 6 GB card. The
graph is Sarungano's ``ona`` txt2img workflow
(``sarungano/packages/ona/ona/workflows/klein-txt2img.json``, tested there on
2026-10-28: four steps, no guidance, about 13 s a 1280x720 picture), copied
here rather than imported because ona is another repository's package.

The model has no negative prompt, and naming what to leave out can draw it
in (ona found the same): a prompt says what IS in the picture.

Talks HTTP only: queue the graph on ``/prompt``, poll ``/history/{id}``, fetch
``/view``. A busy ComfyUI (Sarungano at work) simply runs ours after its own.
"""

from __future__ import annotations

import contextlib
import copy
import io
import time
from collections.abc import Callable
from typing import Any

import httpx

from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = ["IMAGE_MODEL", "PaintError", "paint", "sized"]

IMAGE_MODEL = "FLUX.2 klein 4B (fp8) in ComfyUI, Apache 2.0"
# FLUX.2 draws in steps of 16 pixels.
_STEP = 16

KLEIN_TXT2IMG: dict[str, Any] = {
    "1": {
        "class_type": "UNETLoader",
        "inputs": {"unet_name": "flux-2-klein-4b.safetensors", "weight_dtype": "fp8_e4m3fn"},
    },
    "2": {
        "class_type": "CLIPLoader",
        "inputs": {"clip_name": "qwen_3_4b.safetensors", "type": "flux2", "device": "default"},
    },
    "3": {"class_type": "VAELoader", "inputs": {"vae_name": "flux2-vae.safetensors"}},
    "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["2", 0]}},
    "5": {
        "class_type": "EmptyFlux2LatentImage",
        "inputs": {"width": 1280, "height": 720, "batch_size": 1},
    },
    "6": {"class_type": "Flux2Scheduler", "inputs": {"steps": 4, "width": 1280, "height": 720}},
    "7": {"class_type": "RandomNoise", "inputs": {"noise_seed": 0}},
    "8": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
    "9": {"class_type": "BasicGuider", "inputs": {"model": ["1", 0], "conditioning": ["4", 0]}},
    "10": {
        "class_type": "SamplerCustomAdvanced",
        "inputs": {
            "noise": ["7", 0],
            "guider": ["9", 0],
            "sampler": ["8", 0],
            "sigmas": ["6", 0],
            "latent_image": ["5", 0],
        },
    },
    "11": {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["3", 0]}},
    "12": {
        "class_type": "SaveImage",
        "inputs": {"images": ["11", 0], "filename_prefix": "clipforge-artwork"},
    },
}


class PaintError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = "ARTWORK"


def sized(width: int, height: int) -> tuple[int, int]:
    """The size FLUX.2 draws at: each side up to the next step of 16."""
    return (-(-width // _STEP) * _STEP, -(-height // _STEP) * _STEP)


def graph_for(prompt: str, width: int, height: int, seed: int) -> dict[str, Any]:
    graph = copy.deepcopy(KLEIN_TXT2IMG)
    w, h = sized(width, height)
    graph["4"]["inputs"]["text"] = prompt
    graph["5"]["inputs"].update(width=w, height=h)
    graph["6"]["inputs"].update(width=w, height=h)
    graph["7"]["inputs"]["noise_seed"] = seed
    return graph


def paint(
    url: str,
    prompt: str,
    *,
    width: int,
    height: int,
    seed: int,
    timeout_s: float = 900.0,
    stopping: Callable[[], bool] = lambda: False,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """One picture, as PNG bytes at exactly ``width`` x ``height``. Raises PaintError."""
    if not url:
        raise PaintError("no image model is configured (comfyui_url is empty)", retryable=False)
    base = url.rstrip("/")
    http = client or httpx.Client(timeout=httpx.Timeout(60.0, connect=5.0))
    try:
        try:
            response = http.post(
                f"{base}/prompt",
                json={"prompt": graph_for(prompt, width, height, seed), "client_id": "clipforge"},
            )
        except httpx.HTTPError as exc:
            raise PaintError(f"ComfyUI is not answering at {base}: {exc}") from exc
        if response.status_code >= 400:
            raise PaintError(
                f"ComfyUI refused the workflow: {response.text[:300]}", retryable=False
            )
        prompt_id = (response.json() or {}).get("prompt_id")
        if not prompt_id:
            raise PaintError("ComfyUI accepted the workflow but gave no prompt id")

        deadline = time.monotonic() + timeout_s
        failures = 0
        while True:
            if stopping():
                _cancel(http, base, prompt_id)
                raise PaintError("stopped while painting")
            try:
                history = http.get(f"{base}/history/{prompt_id}").json().get(prompt_id) or {}
                failures = 0
            except (httpx.HTTPError, ValueError) as exc:
                failures += 1
                if failures >= 5:
                    raise PaintError(f"lost ComfyUI while painting: {exc}") from exc
                sleep(2.0)
                continue
            status = (history.get("status") or {}).get("status_str")
            if status == "error":
                raise PaintError(f"ComfyUI failed painting: {str(history.get('status'))[:300]}")
            for output in (history.get("outputs") or {}).values():
                for image in output.get("images") or []:
                    if image.get("type") == "temp":
                        continue
                    data = http.get(
                        f"{base}/view",
                        params={
                            "filename": image.get("filename"),
                            "subfolder": image.get("subfolder") or "",
                            "type": image.get("type") or "output",
                        },
                    ).content
                    return _exact(data, width, height)
            if time.monotonic() >= deadline:
                _cancel(http, base, prompt_id)
                raise PaintError(f"no picture from ComfyUI within {timeout_s:.0f} s")
            sleep(1.0)
    finally:
        if client is None:
            http.close()


def _cancel(http: httpx.Client, base: str, prompt_id: str) -> None:
    with contextlib.suppress(httpx.HTTPError):
        http.post(f"{base}/queue", json={"delete": [prompt_id]})


def _exact(data: bytes, width: int, height: int) -> bytes:
    """Cut back to the size asked for, around the centre (drawn up to 15 px larger)."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            picture = image.convert("RGB")
    except OSError as exc:
        raise PaintError(f"ComfyUI's picture could not be read: {exc}") from exc
    if picture.size != (width, height):
        if picture.width < width or picture.height < height:
            picture = picture.resize((max(width, picture.width), max(height, picture.height)))
        left, top = (picture.width - width) // 2, (picture.height - height) // 2
        picture = picture.crop((left, top, left + width, top + height))
    buffer = io.BytesIO()
    picture.save(buffer, format="PNG")
    return buffer.getvalue()
