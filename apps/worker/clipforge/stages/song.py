"""LYRICS and SING: a GENERATE_MUSIC job (docs/MUSIC-PLAN.md, M2 and M5).

LYRICS has the local model write the song: a title and lyrics from a trend, a
person's notes or an idea (clipforge/synth/lyrics.py). A pasted brief
(clipforge/synth/song_brief.py) brings its own lyrics, sung word for word, and says
how the song should sound; the model reads that, and writes lyrics only when
the brief has none. A song someone likes (clipforge/media/reference.py) is
fetched, measured and listened to, then deleted: an original is written in its
style, and checked for any phrase taken from it. Notes can be a file the
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
from clipforge.media.reference import Reference, fetch_reference
from clipforge.media.sources import IngestError
from clipforge.media.workspace import Workspace
from clipforge.models.broker import InsufficientVramError, ModelLease
from clipforge.models.ollama import OllamaClient, OllamaError
from clipforge.observability import get_logger
from clipforge.stages.analyze import DEFAULT_LLM_VRAM_MB
from clipforge.stages.base import StageContext, StageOutcome
from clipforge.store.firestore import TrackStore, TrendStore
from clipforge.synth.lyrics import (
    LYRICS_PROMPT_VERSION,
    SHORTEST_SONG,
    LyricsError,
    SongBrief,
    brief_for,
    condense_notes,
    names_in,
    normalise_key,
    series_for,
    song_seconds,
    target_seconds,
    write_song,
)
from clipforge.synth.notes import NotesError, extract_text, word_count
from clipforge.synth.song_brief import (
    SONG_BRIEF_PROMPT_VERSION,
    decode_brief,
    decode_reference,
    find_lyrics,
    language_code,
    voice_caption,
)
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
# The longest song the card was measured making (docs/MUSIC-PLAN.md, M5).
LONGEST_SONG_S = 480
# Fewer words heard than this in a liked song, and it is an instrumental.
FEWEST_SUNG_WORDS = 15
# What Whisper asks of the card (clipforge/models/whisper.py).
WHISPER_VRAM_MB = 1800

# What a liked song is heard as: its words and their language.
Listener = Callable[[StageContext, Path, Path], tuple[str, str | None]]


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
        # A liked song (M7): fetched, measured and listened to. Injected so the
        # unit tier needs no network, no ffmpeg and no GPU.
        fetch: Callable[[str, Path], Reference] = fetch_reference,
        tempo_of: Callable[[Path, str], float | None] | None = None,
        listen: Listener | None = None,
    ) -> None:
        self._trends = trends
        self._workspace = workspace
        self._notes_files = notes_files
        self._client_factory = client_factory
        self._fetch = fetch
        self._tempo_of = tempo_of or _tempo_of
        self._listen = listen or _listen

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
                    # An instrumental has no words to measure: it is as long as allowed.
                    "seconds": target_seconds(options),
                }
            )

        if options.lyrics_from is LyricsSource.BRIEF:
            return StageOutcome(checkpoint=self._from_brief(context, options))
        if options.lyrics_from is LyricsSource.REFERENCE:
            return StageOutcome(checkpoint=self._from_reference(context, options))

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

        client = self._ready_client(context)
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
                        seconds=target_seconds(options),
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
                # As long as the words need, unless an exact length was asked for.
                "seconds": options.seconds
                or song_seconds(draft.lyrics, draft.bpm, longest=target_seconds(options)),
                "scene": draft.scene,
                "artStyle": draft.art_style,
                "notesWords": word_count(notes_text) if notes_text else None,
                "keyPoints": key_points[:20],
                "modelVersion": f"ollama:{client.model}",
                "promptVersion": LYRICS_PROMPT_VERSION,
                "llm": dict(client.stats.summary()),
            }
        )

    def _from_brief(self, context: StageContext, options: GenerateMusicOptions) -> dict[str, Any]:
        """A song as a pasted brief describes it: its lyrics as they are, the rest read by the model."""
        text = (options.brief or "").strip()
        if not text:
            raise SongError("a song from a brief needs the brief", code="LYRICS")
        given = find_lyrics(text)
        client = self._ready_client(context)
        seed = base_seed(context, options)
        draft = None
        context.progress("Waiting for the GPU")
        with card(context, f"ollama:{client.model}", DEFAULT_LLM_VRAM_MB) as leased:
            try:
                context.progress("Reading your brief")
                spec = decode_brief(client, text, given, seed=seed)
                style = (options.style or spec.style).strip()[:200]
                asked = max(30, min(spec.seconds, LONGEST_SONG_S)) if spec.seconds else None
                if given is None and spec.vocal != "instrumental":
                    context.progress("Writing the song from your brief")
                    writing = options.model_copy(
                        update={"style": style, "max_seconds": asked or options.max_seconds}
                    )
                    draft = write_song(client, brief_for(options), writing, seed=seed)
            except OllamaError as exc:
                raise SongError(str(exc), retryable=exc.retryable, code="LYRICS") from exc
            except LyricsError as exc:
                raise SongError(str(exc), code="LYRICS") from exc
            leased.observe()

        lyrics = given or (draft.lyrics if draft else None)
        # The brief's tempo and key when it gave them; the writer's otherwise.
        bpm = spec.bpm if 40 <= spec.bpm <= 200 else (draft.bpm if draft else None)
        key = normalise_key(spec.key) or (normalise_key(draft.key) if draft else None)
        # A title the brief itself gives wins; else the one written with the words.
        named = bool(spec.title) and spec.title.lower() in text.lower()
        title = spec.title if named or draft is None else draft.title
        if lyrics is None:
            seconds = asked or target_seconds(options)
        elif given:
            # The person's words are sung in full, and a length asked for that
            # is longer than they need is filled with music.
            needed = song_seconds(lyrics, bpm, longest=LONGEST_SONG_S)
            seconds = max(needed, asked or 0)
        else:
            seconds = options.seconds or song_seconds(
                lyrics, bpm, longest=asked or target_seconds(options)
            )
        return {
            "title": (title or "Untitled")[:60],
            "lyrics": lyrics,
            "theme": spec.about,
            "mood": spec.mood,
            "bpm": bpm,
            "key": key,
            "series": series_for(options, style),
            "inspiration": f"Your brief: {spec.about}"[:200] if spec.about else "Your brief",
            "seconds": seconds,
            "scene": spec.scene or (draft.scene if draft else ""),
            "artStyle": spec.art_style,
            "style": style,
            "voice": voice_caption(spec.vocal),
            # The writer writes English; a brief's own lyrics are sung in theirs.
            "language": language_code(spec.language) if given else "en",
            "lyricsGiven": given is not None,
            "modelVersion": f"ollama:{client.model}",
            "promptVersion": SONG_BRIEF_PROMPT_VERSION
            + (f"+{LYRICS_PROMPT_VERSION}" if draft else ""),
            "llm": dict(client.stats.summary()),
        }

    def _from_reference(
        self, context: StageContext, options: GenerateMusicOptions
    ) -> dict[str, Any]:
        """An original in the style of a song someone likes: never its melody, words or title."""
        url = (options.reference_url or "").strip()
        if not url:
            raise SongError("a song in the style of another needs its link", code="REFERENCE")
        if self._workspace is None:
            raise SongError("this worker cannot fetch a song to study")
        folder = self._workspace.tmp_dir / f"reference-{context.job.id}"
        try:
            context.progress("Fetching the song you like")
            reference = self._fetch(url, folder)
            context.progress("Measuring its tempo")
            tempo = self._tempo_of(reference.audio, context.settings.ffmpeg_bin)
            context.progress("Listening to its words")
            heard, language = self._listen(context, reference.audio, folder)
        except IngestError as exc:
            raise SongError(str(exc), retryable=exc.retryable, code="REFERENCE") from exc
        finally:
            # Studied: the original is not kept, not even in the bin.
            shutil.rmtree(folder, ignore_errors=True)

        client = self._ready_client(context)
        seed = base_seed(context, options)
        # As long as the original (the owner, 2026-11-21), within what the card
        # was measured making. The writer aims its verses and breaks at it.
        longest = max(
            SHORTEST_SONG,
            min(round(reference.duration_s or target_seconds(options)), LONGEST_SONG_S),
        )
        # Its title and channel are names: none of their words may be used.
        avoid = names_in(reference.title, reference.channel)
        draft = None
        context.progress("Waiting for the GPU")
        with card(context, f"ollama:{client.model}", DEFAULT_LLM_VRAM_MB) as leased:
            try:
                context.progress("Describing its sound")
                spec = decode_reference(
                    client,
                    title=reference.title,
                    channel=reference.channel,
                    tags=reference.tags,
                    description=reference.description,
                    duration_s=reference.duration_s,
                    tempo=tempo,
                    heard=heard,
                    language=language,
                    seed=seed,
                )
                style = (options.style or spec.style).strip()[:200]
                sung = len(heard.split()) >= FEWEST_SUNG_WORDS and spec.vocal != "instrumental"
                if sung:
                    context.progress("Writing an original song in its style")
                    brief = SongBrief(
                        source=LyricsSource.REFERENCE,
                        material=(
                            f"Its theme: {spec.about}\nIts feeling: {spec.mood}\n"
                            "Write about that theme from a new angle, with your own images."
                        ),
                        inspiration=f"In the style of: {reference.title}"[:200],
                        avoid=avoid,
                    )
                    writing = options.model_copy(update={"style": style, "max_seconds": longest})
                    draft = write_song(
                        client,
                        brief,
                        writing,
                        seed=seed,
                        original=f"{reference.title}\n{heard}",
                    )
            except OllamaError as exc:
                raise SongError(str(exc), retryable=exc.retryable, code="LYRICS") from exc
            except LyricsError as exc:
                raise SongError(str(exc), code="LYRICS") from exc
            leased.observe()

        series = series_for(options, style)
        bpm = round(tempo) if tempo and 40 <= tempo <= 200 else (draft.bpm if draft else None)
        if draft:
            title = draft.title
        else:
            # An instrumental's title is the model's, unless it echoes the original.
            borrowed = {word.lower() for word in avoid}
            echoes = any(word.lower() in borrowed for word in spec.title.split())
            title = f"{series} instrumental" if echoes or not spec.title else spec.title
        lyrics = draft.lyrics if draft else None
        return {
            "title": title[:60],
            "lyrics": lyrics,
            "theme": spec.about,
            "mood": spec.mood,
            "bpm": bpm,
            "key": normalise_key(draft.key) if draft else None,
            "series": series,
            "inspiration": f"In the style of: {reference.title}"[:200],
            # Exactly the original's length, not just what the words need: a
            # shorter song is filled out with music to the same running time.
            "seconds": longest,
            "scene": spec.scene or (draft.scene if draft else ""),
            "artStyle": spec.art_style,
            "style": style,
            "voice": voice_caption(spec.vocal),
            # The writer writes English, whatever the original was sung in.
            "language": "en",
            "lyricsGiven": False,
            "reference": {
                "url": reference.url,
                "title": reference.title,
                "channel": reference.channel,
                "tempo": round(tempo, 1) if tempo else None,
                "wordsHeard": len(heard.split()),
                "language": language,
            },
            "modelVersion": f"ollama:{client.model}",
            "promptVersion": SONG_BRIEF_PROMPT_VERSION
            + (f"+{LYRICS_PROMPT_VERSION}" if draft else ""),
            "llm": dict(client.stats.summary()),
        }

    def _ready_client(self, context: StageContext) -> OllamaClient:
        client = self._build_client(context)
        if not client.is_available():
            raise SongError(
                "no model is reachable to write the words; start Ollama",
                retryable=True,
                code="LYRICS",
            )
        return client

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


def _tempo_of(audio: Path, ffmpeg: str) -> float | None:
    """A liked song's tempo, measured; None when no beat is clear."""
    from clipforge.media.beats import analyse

    try:
        return analyse(audio, ffmpeg).tempo_bpm
    except Exception as exc:  # noqa: BLE001 - a tempo is a nicety; the model picks one
        log.warning("reference.no_tempo", error=str(exc)[:200])
        return None


def _listen(context: StageContext, audio: Path, folder: Path) -> tuple[str, str | None]:
    """The words sung in a liked song, and their language, by Whisper.

    Only to learn its theme and language, and to check the new words borrow
    none of them. Waits for room on the card the way the song stages do.
    """
    from clipforge.media.audio import extract_audio
    from clipforge.models.whisper import WhisperTranscriber

    settings = context.settings
    wav = folder / "reference-16k.wav"
    extract_audio(audio, wav, ffmpeg_bin=settings.ffmpeg_bin)
    deadline = time.monotonic() + settings.song_gpu_wait_s
    while not context.broker.fits(WHISPER_VRAM_MB):
        if context.stopping() or time.monotonic() >= deadline:
            raise SongError("the card stayed busy", retryable=True, code="SONG_VRAM")
        if comfyui_busy(settings.comfyui_url) is False:
            free_comfyui(settings.comfyui_url)
        context.progress("Waiting for the card to listen to the song")
        time.sleep(CARD_POLL_S)
    transcriber = WhisperTranscriber(
        model=settings.whisper_model,
        compute_type=settings.whisper_compute_type,
        device=settings.whisper_device,
        broker=context.broker,
    )
    try:
        result = transcriber.transcribe(wav, source_id=f"reference-{context.job.id}")
    except InsufficientVramError as exc:
        raise SongError(f"no room on the card: {exc}", retryable=True, code="SONG_VRAM") from exc
    words = " ".join(segment.text.strip() for segment in result.transcript.segments)
    return words, result.transcript.language


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
    # A brief's song carries the sound, the voice and the language it asked for.
    style = str(song.get("style") or options.style or "")
    for take in range(count):
        parts = [style.strip().rstrip(",")]
        if instrumental:
            parts.append("instrumental")
        else:
            parts.append(str(song.get("voice") or _voice(options.vocal, take)))
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
                "seconds": int(song.get("seconds") or target_seconds(options)),
                "seed": seed + take,
                "bpm": song.get("bpm"),
                "keyscale": song.get("key"),
                "vocalLanguage": "unknown" if instrumental else str(song.get("language") or "en"),
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
