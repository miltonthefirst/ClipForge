"""LYRICS and SING: a GENERATE_MUSIC job (docs/MUSIC-PLAN.md, M2 and M5).

LYRICS has the local model write the song: a title and lyrics from a trend, a
person's notes or an idea (clipforge/synth/lyrics.py). An instrumental skips
the model and only names itself.

SING hands the song to the music generator (packages/ngoma, its own process,
clipforge/media/ngoma.py), one take per seed, and imports what it made into
the library as NEW tracks, where a person listens and keeps or discards each.
Nothing is published from here: a kept track becomes videos through a
MUSIC_VIDEO job, and those are reviewed like any other clip.

Both stages are on the GPU lane and take the broker's lease, so a song never
shares the card with Whisper or the LLM. The lease is for the whole child
process, which needs about 5 GB of the 6.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from typing import Any

from clipforge_contracts import GenerateMusicOptions, Lane, LyricsSource, StageName

from clipforge.media.ngoma import NgomaError, run_ngoma, wait_for_comfyui
from clipforge.media.workspace import Workspace
from clipforge.models.broker import InsufficientVramError
from clipforge.models.ollama import OllamaClient, OllamaError
from clipforge.observability import get_logger
from clipforge.stages.analyze import DEFAULT_LLM_VRAM_MB
from clipforge.stages.base import StageContext, StageOutcome
from clipforge.store.firestore import TrackStore, TrendStore
from clipforge.synth.lyrics import (
    LYRICS_PROMPT_VERSION,
    LyricsError,
    brief_for,
    normalise_key,
    series_for,
    write_song,
)
from clipforge.tracks import import_manifest

log = get_logger(__name__)

__all__ = ["LyricsStage", "SingStage", "SongError", "takes_for"]


class SongError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, code: str = "SONG") -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code


def _options(context: StageContext) -> GenerateMusicOptions:
    options = context.job.generate_music_options
    if options is None:
        raise SongError("a GENERATE_MUSIC job needs generateMusicOptions")
    return options


def _checkpoint_of(context: StageContext, stage: StageName) -> dict[str, Any]:
    for held in context.job.stages:
        if held.name is stage:
            return dict(held.checkpoint or {})
    return {}


def base_seed(context: StageContext, options: GenerateMusicOptions) -> int:
    if options.seed is not None:
        return int(options.seed)
    return int(hashlib.sha256(context.job.id.encode()).hexdigest()[:6], 16)


class LyricsStage:
    name = StageName.LYRICS
    lane = Lane.GPU

    def __init__(
        self,
        *,
        trends: TrendStore,
        client_factory: Callable[[StageContext], OllamaClient] | None = None,
    ) -> None:
        self._trends = trends
        self._client_factory = client_factory

    def run(self, context: StageContext) -> StageOutcome:
        options = _options(context)
        series = series_for(options)
        if options.lyrics_from is LyricsSource.NONE:
            title = f"{series} instrumental"
            return StageOutcome(
                checkpoint={
                    "title": title,
                    "lyrics": None,
                    "mood": "",
                    "bpm": None,
                    "key": None,
                    "series": series,
                    "inspiration": None,
                }
            )

        trend = None
        if options.lyrics_from is LyricsSource.TREND:
            if not options.trend_id:
                raise SongError("a song about a trend needs the trend")
            trend = self._trends.get(options.trend_id)
        try:
            brief = brief_for(options, trend)
        except LyricsError as exc:
            raise SongError(str(exc), code="LYRICS") from exc

        client = self._build_client(context)
        if not client.is_available():
            raise SongError(
                "no model is reachable to write the words; start Ollama",
                retryable=True,
                code="LYRICS",
            )
        context.progress("Waiting for the GPU")
        with context.broker.acquire(f"ollama:{client.model}", DEFAULT_LLM_VRAM_MB) as leased:
            context.progress("Writing the song")
            try:
                draft = write_song(client, brief, options, seed=base_seed(context, options))
            except OllamaError as exc:
                raise SongError(str(exc), retryable=exc.retryable, code="LYRICS") from exc
            except LyricsError as exc:
                raise SongError(str(exc), code="LYRICS") from exc
            leased.observe()
        return StageOutcome(
            checkpoint={
                "title": draft.title,
                "lyrics": draft.lyrics,
                "theme": draft.theme,
                "mood": draft.mood,
                "bpm": draft.bpm,
                "key": normalise_key(draft.key),
                "series": series,
                "inspiration": brief.inspiration,
                "modelVersion": f"ollama:{client.model}",
                "promptVersion": LYRICS_PROMPT_VERSION,
                "llm": dict(client.stats.summary()),
            }
        )

    def _build_client(self, context: StageContext) -> OllamaClient:
        if self._client_factory is not None:
            return self._client_factory(context)
        settings = context.settings
        return OllamaClient(
            host=settings.ollama_host, model=settings.ollama_model, num_ctx=settings.ollama_num_ctx
        )


def _voice(vocal: str | None, take: int) -> str:
    # The generated enum is inline in the schema, so compared as the string it is.
    if vocal == "FEMALE":
        return "female vocal"
    if vocal == "MALE":
        return "male vocal"
    return "female vocal" if take % 2 == 0 else "male vocal"


def takes_for(
    context: StageContext, options: GenerateMusicOptions, song: dict[str, Any]
) -> list[dict[str, Any]]:
    """The generator's request: one entry per take, the same song with its own seed and voice."""
    instrumental = options.lyrics_from is LyricsSource.NONE or not song.get("lyrics")
    count = options.takes or 2
    seed = base_seed(context, options)
    title = str(song.get("title") or "Untitled")
    tracks = []
    for take in range(count):
        parts = [options.style.strip().rstrip(",")]
        if instrumental:
            parts.append("instrumental")
        else:
            parts.append(_voice(options.vocal, take))
            if song.get("mood"):
                parts.append(str(song["mood"]))
        caption = ", ".join(p for p in parts if p)[:512]
        tracks.append(
            {
                "title": title if take == 0 else f"{title} (take {take + 1})",
                "series": song.get("series"),
                "inspiration": song.get("inspiration"),
                "caption": caption,
                "lyrics": None if instrumental else song["lyrics"],
                "instrumental": instrumental,
                "seconds": options.seconds or 150,
                "seed": seed + take,
                "bpm": song.get("bpm"),
                "keyscale": song.get("key"),
                "vocalLanguage": "unknown" if instrumental else "en",
            }
        )
    return tracks


class SingStage:
    name = StageName.SING
    lane = Lane.GPU

    def __init__(self, *, tracks: TrackStore, workspace: Workspace) -> None:
        self._tracks = tracks
        self._workspace = workspace

    def run(self, context: StageContext) -> StageOutcome:
        options = _options(context)
        song = _checkpoint_of(context, StageName.LYRICS)
        if not song:
            raise SongError("the song has not been written yet")
        settings = context.settings
        takes = takes_for(context, options, song)
        out = self._workspace.tmp_dir / f"song-{context.job.id}"

        context.progress("Making room on the card")
        try:
            room = wait_for_comfyui(
                settings.comfyui_url, progress=context.progress, stopping=context.stopping
            )
        except NgomaError as exc:
            raise SongError(str(exc), retryable=exc.retryable, code=exc.code) from exc
        log.info("song.room", note=room)

        context.progress("Waiting for the GPU")
        try:
            with context.broker.acquire("ngoma:ace-step-1.5", settings.ngoma_vram_mb) as leased:
                context.progress("Loading the music model")

                def on_event(event: dict[str, Any]) -> None:
                    kind = event.get("event")
                    if kind == "track":
                        context.progress(f"Singing take {event.get('index')} of {event.get('of')}")
                    elif kind == "failed":
                        log.warning(
                            "song.take_failed", index=event.get("index"), error=event.get("error")
                        )

                manifest = run_ngoma(
                    settings,
                    {"steps": 8, "tracks": takes},
                    out,
                    on_event=on_event,
                    stopping=context.stopping,
                )
                leased.observe()
        except InsufficientVramError as exc:
            raise SongError(str(exc), retryable=True, code="SONG_VRAM") from exc
        except NgomaError as exc:
            raise SongError(str(exc), retryable=exc.retryable, code=exc.code) from exc

        # Say which job made each one, then bring them into the library.
        made = json.loads(manifest.read_text(encoding="utf-8"))
        for entry in made.get("tracks", []):
            entry["jobId"] = context.job.id
        manifest.write_text(json.dumps(made, indent=2), encoding="utf-8")
        context.progress("Adding the takes to the library")
        result = import_manifest(manifest, self._workspace.music_dir, self._tracks)
        track_ids = result.added + result.updated
        if not track_ids:
            raise SongError("the music generator made nothing usable", retryable=True)
        shutil.rmtree(out, ignore_errors=True)
        return StageOutcome(
            checkpoint={
                "trackIds": track_ids,
                "takes": len(takes),
                "made": len(track_ids),
            }
        )
