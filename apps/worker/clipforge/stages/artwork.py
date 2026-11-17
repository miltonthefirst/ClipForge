"""ARTWORK: a song's background picture (the owner, 2026-11-18: "a very nice
background image in relation to the context of the music").

The local language model describes one scene for the song: a place, its light,
weather and colours, and the things in it, never a person or writing (the
drawn-videos rule, and nothing a viewer could take for a real someone). A new
song brings that scene from LYRICS; an older track, or an instrumental, has
one written here from its title, sound and words. The local image model
(clipforge/media/comfy.py) then paints it twice, wide for the full video and
tall for the Short, and every track it belongs to gets the pair.

Last in a GENERATE_MUSIC job, first in a MUSIC_VIDEO one (skipped when the
track already has its picture). A missing image model never fails the job: the
song is the point, and a video without a picture is drawn as before.
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clipforge_contracts import JobType, Lane, StageName, Track, TrackArtwork
from pydantic import BaseModel, Field

from clipforge.media.comfy import IMAGE_MODEL, PaintError, paint
from clipforge.media.ngoma import comfyui_busy, free_comfyui
from clipforge.media.workspace import Workspace
from clipforge.models.ollama import OllamaClient, OllamaError
from clipforge.observability import get_logger
from clipforge.stages.analyze import DEFAULT_LLM_VRAM_MB
from clipforge.stages.base import StageContext, StageOutcome
from clipforge.stages.song import SongError, card
from clipforge.store.firestore import TrackStore
from clipforge.synth.lyrics import ArtStyle

log = get_logger(__name__)

__all__ = ["TALL", "WIDE", "ArtworkStage", "art_prompt", "preview_of"]

# Drawn at about 1.3 megapixels, which FLUX.2 klein does well in some 20 s on
# this card, and scaled up to 1920x1080 (or 1080x1920) by the video's own
# filter, with a slow drift that hides the softness.
WIDE = (1536, 864)
TALL = (864, 1536)
PREVIEW_WIDTH = 320
ARTWORK_VRAM_MB = 4000


class SceneDraft(BaseModel):
    """What the model returns for a track with no scene yet."""

    scene: str = Field(..., max_length=400)
    art_style: ArtStyle


SCENE_SYSTEM = """You choose the background picture for a song's video on a music channel.

Describe one scene in one or two sentences: a place that carries the song's feeling, with its time of day, light, weather, colours and a few objects. Only places and things: an empty street, a window, a field, a skyline, a table, a sky, water, a room. Nobody is in the scene: no person, child, crowd or figure, not even far away. It is painted as it is written, so describe only what should be seen. Pick the style it is made in."""


# Words that put a person in the picture. The model is told "only places and
# things" and still wrote "children running across the field, adults
# watching" for a song about the first rain (2026-11-18); asking is not
# enough, so the scene is checked.
_PEOPLE = re.compile(
    r"\b(people|persons?|man|men|woman|women|boys?|girls?|child|children|kids?|"
    r"babies|baby|adults?|crowds?|famil(y|ies)|couples?|friends?|dancers?|singers?|"
    r"figures?|silhouettes?|faces?|hands?|neighbou?rs?|mothers?|fathers?|grand\w+|"
    r"he|she|they|someone|everyone|anyone|villagers|students?|teachers?|workers?|"
    r"farmers?|players?|musicians?|audiences?|strangers?|lovers?)\b",
    re.IGNORECASE,
)


def people_free(scene: str) -> str:
    """The scene without any sentence that puts a person in it."""
    sentences = re.split(r"(?<=[.!?;])\s+", scene.strip())
    kept = [s for s in sentences if s and not _PEOPLE.search(s)]
    return " ".join(kept).strip()


def art_prompt(scene: str, style: str) -> str:
    """What the image model is asked for: the scene, made in its style, framed for a video."""
    scene = scene.strip().rstrip(".")
    return (
        f"{scene}. {style[:1].upper() + style[1:]}, wide cinematic composition with calm open "
        "space in the middle of the frame, soft depth of field, rich harmonious colour, "
        "gentle atmospheric light, highly detailed, the place filling the whole frame edge to edge"
    )[:1500]


def preview_of(png: bytes) -> str:
    """A small JPEG of the picture, base64, for the Music page."""
    from PIL import Image

    with Image.open(io.BytesIO(png)) as image:
        small = image.convert("RGB")
        small.thumbnail((PREVIEW_WIDTH, PREVIEW_WIDTH))
        buffer = io.BytesIO()
        small.save(buffer, format="JPEG", quality=72)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _checkpoint_of(context: StageContext, stage: StageName) -> dict[str, Any]:
    for held in context.job.stages:
        if held.name is stage:
            return dict(held.checkpoint or {})
    return {}


class ArtworkStage:
    name = StageName.ARTWORK
    lane = Lane.GPU

    def __init__(
        self,
        *,
        tracks: TrackStore,
        workspace: Workspace,
        client_factory: Callable[[StageContext], OllamaClient] | None = None,
        painter: Callable[..., bytes] = paint,
    ) -> None:
        self._tracks = tracks
        self._workspace = workspace
        self._client_factory = client_factory
        self._paint = painter

    def run(self, context: StageContext) -> StageOutcome:
        job = context.job
        if job.type is JobType.MUSIC_VIDEO:
            options = job.music_video_options
            track_ids = [options.track_id] if options else []
        else:
            track_ids = list(_checkpoint_of(context, StageName.SING).get("trackIds") or [])
        tracks = [t for t in (self._tracks.get(i) for i in track_ids) if t is not None]
        if not tracks:
            raise SongError("there is no track to paint a picture for")
        if job.type is JobType.MUSIC_VIDEO and _has_artwork(tracks[0]):
            return StageOutcome(skipped=True, checkpoint={"artwork": "already there"})

        song = _checkpoint_of(context, StageName.LYRICS)
        scene, style = song.get("scene"), song.get("artStyle")
        if not scene or not style:
            scene, style = self._describe(context, tracks[0])

        place = people_free(scene)
        if len(place) < 12:
            # Every sentence had someone in it: the place alone, from the song's own theme.
            theme = song.get("theme") or tracks[0].prompt
            place = people_free(f"An empty, quiet place that feels like this: {theme}.") or (
                "A wide open landscape at golden hour under a vast sky."
            )
        prompt = art_prompt(place, style)
        seed = int(hashlib.sha256(f"{job.id}:art".encode()).hexdigest()[:8], 16)
        folder = self._workspace.music_dir / "artwork"
        folder.mkdir(parents=True, exist_ok=True)
        settings = context.settings
        try:
            context.progress("Waiting for the GPU to paint the background")
            with card(context, "comfyui:flux2-klein-4b", ARTWORK_VRAM_MB):
                context.progress("Painting the background")
                wide = self._paint(
                    settings.comfyui_url,
                    prompt,
                    width=WIDE[0],
                    height=WIDE[1],
                    seed=seed,
                    stopping=context.stopping,
                )
                context.progress("Painting the tall background for the Short")
                tall = self._paint(
                    settings.comfyui_url,
                    prompt,
                    width=TALL[0],
                    height=TALL[1],
                    seed=seed,
                    stopping=context.stopping,
                )
        except PaintError as exc:
            # The song stands without a picture; its video is drawn as before.
            log.warning("artwork.not_painted", job_id=job.id, error=str(exc)[:300])
            return StageOutcome(checkpoint={"artwork": None, "reason": str(exc)[:300]})
        finally:
            # FLUX stays loaded in ComfyUI after a job: give the card back,
            # unless Sarungano has work of its own queued there now.
            if comfyui_busy(settings.comfyui_url) is False:
                free_comfyui(settings.comfyui_url)

        wide_path = folder / f"{job.id}-wide.png"
        tall_path = folder / f"{job.id}-tall.png"
        wide_path.write_bytes(wide)
        tall_path.write_bytes(tall)
        artwork = TrackArtwork(
            wide=str(wide_path.resolve()),
            tall=str(tall_path.resolve()),
            prompt=prompt,
            model=IMAGE_MODEL,
            preview_base64=preview_of(wide),
            created_at=datetime.now(UTC),
        )
        for track in tracks:
            self._tracks.set_artwork(track.id, artwork)
        return StageOutcome(
            checkpoint={
                "artwork": str(wide_path.resolve()),
                "prompt": prompt,
                "tracks": [t.id for t in tracks],
            }
        )

    def _describe(self, context: StageContext, track: Track) -> tuple[str, str]:
        """A scene for a track that came without one: an older song, an instrumental."""
        client = self._build_client(context)
        fallback = (
            f"A quiet landscape at golden hour that feels like {track.prompt[:120]}",
            "digital painting",
        )
        if not client.is_available():
            return fallback
        about = [f"Title: {track.title}", f"The sound: {track.prompt}"]
        if track.lyrics:
            about.append(f"The words:\n{track.lyrics[:2500]}")
        try:
            context.progress("Waiting for the GPU")
            with card(context, f"ollama:{client.model}", DEFAULT_LLM_VRAM_MB):
                context.progress("Choosing the background")
                draft = client.generate_structured(
                    schema_model=SceneDraft,
                    system=SCENE_SYSTEM,
                    prompt="\n".join(about),
                    temperature=0.7,
                    seed=int(hashlib.sha256(track.id.encode()).hexdigest()[:6], 16),
                )
        except OllamaError as exc:
            log.warning("artwork.no_scene", track=track.id, error=str(exc)[:200])
            return fallback
        return draft.scene, draft.art_style

    def _build_client(self, context: StageContext) -> OllamaClient:
        if self._client_factory is not None:
            return self._client_factory(context)
        settings = context.settings
        return OllamaClient(
            host=settings.ollama_host, model=settings.ollama_model, num_ctx=settings.ollama_num_ctx
        )


def _has_artwork(track: Track) -> bool:
    art = track.artwork
    return bool(art and Path(art.wide).is_file() and Path(art.tall).is_file())
