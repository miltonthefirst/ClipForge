"""LYRICS and SING: a GENERATE_MUSIC job (docs/MUSIC-PLAN.md, M2 and M5).

LYRICS has the local model write the song: a title and lyrics from a trend, a
person's notes or an idea (clipforge/synth/lyrics.py). Notes can be a file the
phone uploaded (PDF, Word, PowerPoint): it is fetched from the bucket, read
(clipforge/synth/notes.py), boiled down to its key points when it is long, and
deleted from the bucket once the song is written. An instrumental skips the
model and only names itself.

SING hands the song to the music generator (packages/ngoma, its own process,
clipforge/media/ngoma.py), one take per seed, and imports what it made into
the library as NEW tracks, where a person listens and keeps or discards each.
Nothing is published from here: a kept track becomes videos through a
MUSIC_VIDEO job, and those are reviewed like any other clip.

Both stages are on the GPU lane and take the broker's lease, so a song never
shares the card with Whisper or the LLM. **A busy card is waited for, not
failed on** (the owner, 2026-11-18): the lease is asked for again every half
minute, an idle ComfyUI is asked to let go each time, and the stage says what
it is waiting for. Only after ``song_gpu_wait_s`` does it give up.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Protocol

from clipforge_contracts import GenerateMusicOptions, Lane, LyricsSource, StageName

from clipforge.config import Settings
from clipforge.media.ngoma import NgomaError, comfyui_busy, free_comfyui, run_ngoma
from clipforge.media.workspace import Workspace
from clipforge.models.broker import InsufficientVramError, ModelLease
from clipforge.models.ollama import OllamaClient, OllamaError
from clipforge.observability import get_logger
from clipforge.stages.analyze import DEFAULT_LLM_VRAM_MB
from clipforge.stages.base import StageContext, StageOutcome
from clipforge.store.firestore import TrackStore, TrendStore
from clipforge.synth.lyrics import (
    LYRICS_PROMPT_VERSION,
    LyricsError,
    brief_for,
    condense_notes,
    normalise_key,
    series_for,
    write_song,
)
from clipforge.synth.notes import NotesError, extract_text, word_count
from clipforge.tracks import import_manifest

log = get_logger(__name__)

__all__ = [
    "BucketNotes",
    "LyricsStage",
    "NotesFiles",
    "SingStage",
    "SongError",
    "card",
    "takes_for",
]

# How often a song waiting for the card asks again.
CARD_POLL_S = 30.0


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


@contextmanager
def card(
    context: StageContext,
    model: str,
    required_mb: int,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[ModelLease]:
    """The broker's lease, waited for as long as ``song_gpu_wait_s`` allows.

    Each refusal says on the job what is free and what is needed, and asks an
    idle ComfyUI to unload: it never lets go on its own. ComfyUI at work is
    left alone; that is Sarungano's job running.
    """
    settings = context.settings
    deadline = clock() + settings.song_gpu_wait_s
    with ExitStack() as stack:
        while True:
            try:
                lease = stack.enter_context(context.broker.acquire(model, required_mb))
                break
            except InsufficientVramError as exc:
                if context.stopping():
                    raise SongError("stopped while waiting for the card", retryable=True) from exc
                if clock() >= deadline:
                    hours = settings.song_gpu_wait_s / 3600
                    raise SongError(
                        f"the card stayed busy for {hours:g} hours: {exc}",
                        retryable=True,
                        code="SONG_VRAM",
                    ) from exc
                busy = comfyui_busy(settings.comfyui_url)
                if busy is False:
                    free_comfyui(settings.comfyui_url)
                who = " while Sarungano's ComfyUI works" if busy else ""
                context.progress(
                    f"Waiting for the card{who}: {max(exc.available_mb, 0)} MiB free, "
                    f"{exc.required_mb} needed"
                )
                log.info(
                    "song.waiting_for_card", model=model, available_mb=exc.available_mb, busy=busy
                )
                waited = 0.0
                while waited < CARD_POLL_S and not context.stopping():
                    sleep(1.0)
                    waited += 1.0
        yield lease


# ── Notes files ──────────────────────────────────────────────────────────────


class NotesFiles(Protocol):
    def fetch(self, path: str, into: Path) -> Path: ...
    def delete(self, path: str) -> None: ...


class BucketNotes:
    """Notes the phone uploaded to ``notes/{uid}/...`` in the project's bucket."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _bucket(self) -> Any:
        from google.cloud import storage as gcs  # type: ignore[attr-defined]

        if not self._settings.firebase_storage_bucket:
            raise SongError("no storage bucket is configured, so an uploaded file cannot be read")
        return gcs.Client().bucket(self._settings.firebase_storage_bucket)

    def fetch(self, path: str, into: Path) -> Path:
        into.parent.mkdir(parents=True, exist_ok=True)
        blob = self._bucket().blob(path)
        if not blob.exists():
            raise SongError("the uploaded notes are not in the bucket any more; upload them again")
        blob.download_to_filename(str(into))
        return into

    def delete(self, path: str) -> None:
        try:
            self._bucket().blob(path).delete()
        except Exception as exc:  # noqa: BLE001 - the lifecycle rule collects it anyway
            log.warning("song.notes_not_deleted", path=path, error=str(exc)[:200])


def _safe_notes_path(path: str, uid: str) -> str:
    """Only the job owner's own uploads; nothing that climbs out of the folder."""
    if not path.startswith(f"notes/{uid}/") or ".." in path.split("/"):
        raise SongError("those notes are not this job owner's upload")
    return path


class LyricsStage:
    name = StageName.LYRICS
    lane = Lane.GPU

    def __init__(
        self,
        *,
        trends: TrendStore,
        workspace: Workspace | None = None,
        notes_files: NotesFiles | None = None,
        client_factory: Callable[[StageContext], OllamaClient] | None = None,
    ) -> None:
        self._trends = trends
        self._workspace = workspace
        self._notes_files = notes_files
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

        notes_text = None
        uploaded = None
        if options.lyrics_from is LyricsSource.NOTES and options.notes_file:
            uploaded = _safe_notes_path(options.notes_file, context.job.uid)
            notes_text = self._read_upload(context, uploaded, options)

        client = self._build_client(context)
        if not client.is_available():
            raise SongError(
                "no model is reachable to write the words; start Ollama",
                retryable=True,
                code="LYRICS",
            )
        context.progress("Waiting for the GPU")
        key_points: list[str] = []
        with card(context, f"ollama:{client.model}", DEFAULT_LLM_VRAM_MB) as leased:
            try:
                if options.lyrics_from is LyricsSource.NOTES:
                    text = "\n\n".join(t for t in (notes_text, options.notes) if t)
                    key_points = condense_notes(
                        client,
                        text,
                        focus=options.focus,
                        seconds=options.seconds or 150,
                        progress=context.progress,
                    )
                brief = brief_for(
                    options,
                    trend,
                    notes_text=notes_text,
                    key_points=key_points,
                    file_name=options.notes_file_name,
                )
                context.progress("Writing the song")
                draft = write_song(client, brief, options, seed=base_seed(context, options))
            except OllamaError as exc:
                raise SongError(str(exc), retryable=exc.retryable, code="LYRICS") from exc
            except LyricsError as exc:
                raise SongError(str(exc), code="LYRICS") from exc
            leased.observe()

        if uploaded and self._notes_files is not None:
            # Written: the file has done its job, and notes are the owner's.
            self._notes_files.delete(uploaded)
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
                "notesWords": word_count(notes_text) if notes_text else None,
                "keyPoints": key_points[:20],
                "modelVersion": f"ollama:{client.model}",
                "promptVersion": LYRICS_PROMPT_VERSION,
                "llm": dict(client.stats.summary()),
            }
        )

    def _read_upload(self, context: StageContext, path: str, options: GenerateMusicOptions) -> str:
        if self._notes_files is None or self._workspace is None:
            raise SongError("this worker cannot read uploaded notes")
        name = Path(options.notes_file_name or path.rsplit("/", 1)[-1]).name
        local = self._workspace.tmp_dir / f"notes-{context.job.id}" / name
        context.progress(f"Reading {name}")
        try:
            self._notes_files.fetch(path, local)
            return extract_text(local)
        except NotesError as exc:
            raise SongError(str(exc), code="NOTES") from exc
        finally:
            shutil.rmtree(local.parent, ignore_errors=True)

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

        # An idle ComfyUI keeps its last model on the card; ask it to let go
        # before the first try, so an idle card is not waited on for nothing.
        if comfyui_busy(settings.comfyui_url) is False:
            free_comfyui(settings.comfyui_url)

        context.progress("Waiting for the GPU")
        try:
            with card(context, "ngoma:ace-step-1.5", settings.ngoma_vram_mb) as leased:
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
