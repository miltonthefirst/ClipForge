"""The COMPOSE job: a video that did not exist.

Five stages, and the shape of them is the point.

**SCRIPT** holds the GPU for one model call: given a topic, an angle and the
facts a trend carried, it writes a conversation — a cast, and scenes of
lines each said by a named character; given dialogue a person wrote, it only
casts and stages it. **NARRATE** speaks every line on the CPU in that
character's own voice and joins them, which is also what times the whole
video: each line's start and end are known exactly. **ALIGN** holds the GPU
again, briefly, to listen back with Whisper for the word timings the
captions want. **DRAW** renders every scene as stick-figure cartoon frames on
the CPU, where the time actually goes, with the figure whose line is playing
doing the talking. **ASSEMBLE** joins them behind a title card, lays the
voices over, burns the captions, and writes a clip like any other.

Everything the model contributes is a choice from a vocabulary — a pose, a
mood, a prop, a kind of voice — and everything the renderer draws is a
function of the scene, the seed and the clock. So a composed clip is
reproducible from its own record, which no cut clip is, and nothing in it
can resemble a real person, which is a rule of the visual mode and not a
hope about the prompt.
See docs/adr/0027-drawn-cartoons-as-the-first-visual-mode.md.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
import wave
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from clipforge_contracts import (
    AppliedCompose,
    Clip,
    ClipLocation,
    ClipPreview,
    CompileTransition,
    ComposeCharacter,
    ComposeLine,
    ComposeOptions,
    ComposeScene,
    ComposeStyle,
    Lane,
    ReviewState,
    SceneMood,
    ScriptAuthor,
    StageName,
    StickActor,
    StickMood,
    StickPose,
    StickProp,
    Transcript,
    TranscriptWord,
    VoiceKind,
)

from clipforge.media.assemble import (
    AssembledClip,
    concat_segments,
    output_args,
    render_title_card,
)
from clipforge.media.captions import build_ass, group_into_cues
from clipforge.media.cartoon import render_scene
from clipforge.media.poster import PosterError, extract_poster
from clipforge.media.profiles import RenderProfile, load_profile
from clipforge.media.render import RenderError, escape_filter_path
from clipforge.media.speech import (
    DEFAULT_VOICES,
    SpeechError,
    SpeechSynth,
    kokoro_language,
    write_wav,
)
from clipforge.media.workspace import Workspace
from clipforge.models.ollama import OllamaClient, OllamaError
from clipforge.observability import get_logger
from clipforge.stages.analyze import DEFAULT_LLM_VRAM_MB
from clipforge.stages.base import StageContext, StageOutcome
from clipforge.store.blobs import BlobStore
from clipforge.store.firestore import ClipStore
from clipforge.synth.scenes import (
    Line,
    SceneSpec,
    TimedLine,
    TimedScene,
    actors_for,
    cast_from_response,
    full_script,
    parse_dialogue,
    scenes_from_response,
    time_scenes,
    word_count,
)
from clipforge.synth.script import SCRIPT_PROMPT_VERSION, write_script
from clipforge.synth.voices import assign_voices, is_narrator, voice_kind_for_name

log = get_logger(__name__)

__all__ = [
    "AlignStage",
    "ComposeAssembleStage",
    "ComposeError",
    "DrawStage",
    "NarrateStage",
    "ScriptStage",
    "compose_dir",
]

#: The beat between two lines of one scene, and the longer one between scenes.
LINE_GAP_SEC = 0.35
SCENE_GAP_SEC = 0.6
#: Silence after the last line, so the video does not end on the last syllable.
TAIL_SEC = 0.6


class ComposeError(RuntimeError):
    """The video cannot be made as asked. ``retryable`` and ``code`` are read by the runner."""

    def __init__(self, message: str, *, retryable: bool = False, code: str = "COMPOSE") -> None:
        self.retryable = retryable
        self.code = code
        super().__init__(message)


class Aligned(Protocol):
    @property
    def transcript(self) -> Transcript: ...


class Aligner(Protocol):
    """The sliver of the Whisper transcriber ALIGN uses."""

    def transcribe(
        self, audio_path: Path, *, source_id: str, language: str | None = None
    ) -> Aligned: ...


def compose_dir(workspace: Workspace, job_id: str) -> Path:
    """Where a job's narration and drawn scenes live between stages.

    Under tmp, because the finished clip is what is kept; a crash between
    stages resumes from whatever is still here, and a collected tmp costs a
    redraw rather than a lost clip.
    """
    return workspace.tmp_dir / "compose" / job_id


def _options(context: StageContext) -> ComposeOptions:
    options = context.job.compose_options
    if options is None:
        raise ComposeError("this job has no compose options", code="COMPOSE_NO_OPTIONS")
    return options


def _checkpoint_of(context: StageContext, stage: StageName) -> dict[str, Any]:
    for held in context.job.stages:
        if held.name is stage:
            return dict(held.checkpoint or {})
    return {}


# ── The scene record, to and from a checkpoint ────────────────────────────────


def _scene_dict(scene: SceneSpec) -> dict[str, Any]:
    return {
        "index": scene.index,
        "lines": [{"speaker": line.speaker, "text": line.text} for line in scene.lines],
        "actors": [
            {"name": actor.name, "pose": actor.pose.value, "mood": actor.mood.value}
            for actor in scene.actors
        ],
        "props": [prop.value for prop in scene.props],
        "mood": scene.mood.value,
        "label": scene.label,
    }


def _scenes_from(checkpoint: dict[str, Any]) -> list[SceneSpec]:
    scenes: list[SceneSpec] = []
    for index, raw in enumerate(checkpoint.get("scenes") or []):
        scenes.append(
            SceneSpec(
                index=int(raw.get("index", index)),
                lines=tuple(
                    Line(
                        speaker=str(item.get("speaker") or "Narrator"),
                        text=str(item.get("text") or ""),
                    )
                    for item in raw.get("lines") or []
                ),
                actors=tuple(
                    StickActor(
                        name=str(a.get("name") or "someone"),
                        pose=StickPose(a.get("pose") or "STAND"),
                        mood=StickMood(a.get("mood") or "NEUTRAL"),
                    )
                    for a in raw.get("actors") or []
                ),
                props=tuple(StickProp(p) for p in raw.get("props") or []),
                mood=SceneMood(raw.get("mood") or "CALM"),
                label=raw.get("label") or None,
            )
        )
    return scenes


def _cast_from(checkpoint: dict[str, Any]) -> list[tuple[str, VoiceKind]]:
    return [
        (str(member.get("name") or ""), VoiceKind(member.get("kind") or "WOMAN_US"))
        for member in checkpoint.get("cast") or []
        if member.get("name")
    ]


def _timeline_from(checkpoint: dict[str, Any]) -> list[TimedLine]:
    return [
        TimedLine(
            speaker=str(item.get("speaker") or "Narrator"),
            text=str(item.get("text") or ""),
            scene=int(item.get("scene") or 0),
            voice=str(item.get("voice") or ""),
            start_sec=float(item.get("startSec") or 0.0),
            end_sec=float(item.get("endSec") or 0.0),
        )
        for item in checkpoint.get("lines") or []
    ]


def _words_from(checkpoint: dict[str, Any]) -> list[TranscriptWord]:
    return [
        TranscriptWord(
            text=str(w.get("text") or ""),
            start_sec=float(w.get("startSec") or 0.0),
            end_sec=float(w.get("endSec") or 0.0),
        )
        for w in checkpoint.get("words") or []
    ]


def _plain_scenes(lines: Sequence[Line]) -> list[SceneSpec]:
    """Dialogue with no model to stage it: two lines a scene, the speakers on screen."""
    scenes: list[SceneSpec] = []
    for start in range(0, len(lines), 2):
        chunk = lines[start : start + 2]
        scenes.append(
            SceneSpec(
                index=len(scenes),
                lines=tuple(chunk),
                actors=actors_for([line.speaker for line in chunk], ()),
                props=(),
                mood=SceneMood.CALM,
            )
        )
    return scenes


def _plain_cast(lines: Sequence[Line]) -> list[tuple[str, VoiceKind]]:
    cast: list[tuple[str, VoiceKind]] = []
    seen: set[str] = set()
    for line in lines:
        key = line.speaker.casefold()
        if key in seen or is_narrator(line.speaker):
            continue
        cast.append((line.speaker, voice_kind_for_name(line.speaker, len(cast))))
        seen.add(key)
    return cast


# ── SCRIPT ────────────────────────────────────────────────────────────────────


class ScriptStage:
    """Write the conversation and stage it, or stage the conversation given."""

    name = StageName.SCRIPT
    lane = Lane.GPU

    def __init__(
        self, *, client_factory: Callable[[StageContext], OllamaClient] | None = None
    ) -> None:
        self._client_factory = client_factory

    def run(self, context: StageContext) -> StageOutcome:
        options = _options(context)
        given = parse_dialogue(options.script) if options.script else None
        if given is not None and not given:
            raise ComposeError("the script given has no words in it", code="COMPOSE_EMPTY_SCRIPT")

        client = self._build_client(context)
        available = client.is_available()
        if not available and given is None:
            raise ComposeError(
                "no model is reachable to write the script; start Ollama, or give the words "
                "yourself and the video will be drawn without it",
                retryable=True,
                code="COMPOSE_NO_MODEL",
            )

        title = options.topic.strip()[:80]
        author = ScriptAuthor.OPERATOR if given is not None else ScriptAuthor.MODEL
        model_version: str | None = None
        prompt_version: str | None = None
        llm: dict[str, Any] = {}
        peak: int | None = None
        scenes: list[SceneSpec]
        cast: list[tuple[str, VoiceKind]]

        if available:
            context.progress("Waiting for the GPU")
            with context.broker.acquire(f"ollama:{client.model}", DEFAULT_LLM_VRAM_MB) as leased:
                context.progress("Staging the dialogue" if given else "Writing the conversation")
                response = None
                try:
                    response = write_script(client, options, lines=given)
                except OllamaError as exc:
                    if exc.retryable:
                        raise ComposeError(str(exc), retryable=True, code="COMPOSE_MODEL") from exc
                    if given is None:
                        raise ComposeError(
                            f"the model could not write a usable script: {exc}",
                            code="COMPOSE_MODEL",
                        ) from exc
                    log.warning("compose.staging_failed", error=str(exc)[:200])
                peak = leased.observe()
            llm = dict(client.stats.summary())
            if response is not None:
                scenes = scenes_from_response(response, script=given)
                cast = cast_from_response(response, scenes)
                title = response.title.strip()[:80] or title
                model_version = f"ollama:{client.model}"
                prompt_version = SCRIPT_PROMPT_VERSION
            else:
                scenes = _plain_scenes(given or [])
                cast = _plain_cast(given or [])
        else:
            scenes = _plain_scenes(given or [])
            cast = _plain_cast(given or [])

        if not scenes:
            raise ComposeError("the model returned no scenes", code="COMPOSE_MODEL")
        script = full_script(scenes)
        return StageOutcome(
            checkpoint={
                "title": title,
                "script": script,
                "cast": [{"name": name, "kind": kind.value} for name, kind in cast],
                "scenes": [_scene_dict(scene) for scene in scenes],
                "scriptBy": author.value,
                "modelVersion": model_version,
                "promptVersion": prompt_version,
                "llm": llm,
            },
            peak_vram_mb=peak or None,
            detail=f"{len(scenes)} scenes, {len(cast)} characters, {word_count(script)} words"
            + (" from the dialogue given" if given else ""),
        )

    def _build_client(self, context: StageContext) -> OllamaClient:
        if self._client_factory is not None:
            return self._client_factory(context)
        settings = context.settings
        return OllamaClient(
            host=settings.ollama_host, model=settings.ollama_model, num_ctx=settings.ollama_num_ctx
        )


# ── NARRATE ───────────────────────────────────────────────────────────────────


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    """A mono 16-bit file back as float samples, the way `write_wav` made it."""
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
        channels = handle.getnchannels()
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32767.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, rate


class NarrateStage:
    """Speak every line in its character's voice, and join them into one track.

    Line by line rather than all at once, for two reasons that both matter:
    each character gets a voice of their own, and each line's start and end
    are then known exactly, which is what times the scenes and the talking.
    """

    name = StageName.NARRATE
    lane = Lane.CPU

    def __init__(
        self,
        *,
        workspace: Workspace,
        synth_factory: Callable[[StageContext], SpeechSynth] | None = None,
    ) -> None:
        self._workspace = workspace
        self._synth_factory = synth_factory

    def run(self, context: StageContext) -> StageOutcome:
        options = _options(context)
        script_cp = _checkpoint_of(context, StageName.SCRIPT)
        scenes = _scenes_from(script_cp)
        if not any(scene.lines for scene in scenes):
            raise ComposeError("SCRIPT left no lines to speak", code="COMPOSE_NO_SCRIPT")

        folder = compose_dir(self._workspace, context.job.id)
        folder.mkdir(parents=True, exist_ok=True)
        language = options.language or "en-us"
        narrator = options.voice or DEFAULT_VOICES.get(
            kokoro_language(language), context.settings.speech_default_voice
        )
        cast = _cast_from(script_cp)
        voices = assign_voices(cast, narrator_voice=narrator, seed=options.seed or 0)
        synth = self._build_synth(context)

        pieces: list[np.ndarray] = []
        timeline: list[dict[str, Any]] = []
        rate: int | None = None
        cursor = 0.0
        spoken_lines = 0
        total_lines = sum(len(scene.lines) for scene in scenes)
        for scene in scenes:
            for index, line in enumerate(scene.lines):
                voice = (
                    narrator
                    if is_narrator(line.speaker)
                    else voices.get(line.speaker.casefold(), narrator)
                )
                spoken_lines += 1
                context.progress(f"Speaking line {spoken_lines} of {total_lines} as {line.speaker}")
                try:
                    spoken = synth.speak(
                        line.text,
                        voice=voice,
                        language=language,
                        speed=1.0,
                        destination=folder / f"line-{scene.index:02d}-{index:02d}.wav",
                    )
                except SpeechError as exc:
                    raise ComposeError(
                        f"{line.speaker}'s line could not be spoken: {exc}", code="COMPOSE_SPEECH"
                    ) from exc
                samples, sample_rate = _read_wav(spoken.path)
                if rate is None:
                    rate = sample_rate
                elif sample_rate != rate:
                    raise ComposeError(
                        f"the voices do not agree on a sample rate ({sample_rate} vs {rate})",
                        code="COMPOSE_SPEECH",
                    )
                gap = 0.0
                if pieces:
                    gap = LINE_GAP_SEC if index > 0 else SCENE_GAP_SEC
                if gap:
                    pieces.append(np.zeros(int(gap * rate), dtype=np.float32))
                    cursor += gap
                start = cursor
                pieces.append(samples)
                cursor += len(samples) / rate
                timeline.append(
                    {
                        "scene": scene.index,
                        "index": index,
                        "speaker": line.speaker,
                        "text": line.text,
                        "voice": voice,
                        "startSec": round(start, 3),
                        "endSec": round(cursor, 3),
                    }
                )
        if rate is None or not pieces:
            raise ComposeError("nothing was spoken", code="COMPOSE_SPEECH")
        pieces.append(np.zeros(int(TAIL_SEC * rate), dtype=np.float32))
        duration = write_wav(
            np.concatenate(pieces), sample_rate=rate, destination=folder / "narration.wav"
        )
        return StageOutcome(
            checkpoint={
                "path": str(folder / "narration.wav"),
                "durationSec": round(duration, 3),
                "voice": narrator,
                "engine": synth.engine,
                "language": language,
                "cast": [
                    {
                        "name": name,
                        "kind": kind.value,
                        "voice": voices.get(name.casefold(), narrator),
                    }
                    for name, kind in cast
                ],
                "lines": timeline,
                "characters": sum(len(line.text) for scene in scenes for line in scene.lines),
            },
            detail=f"{duration:.0f}s of dialogue in {len(set(voices.values())) or 1} voices",
        )

    def _build_synth(self, context: StageContext) -> SpeechSynth:
        if self._synth_factory is not None:
            return self._synth_factory(context)
        from clipforge.media.speech import KokoroSynth

        settings = context.settings
        return KokoroSynth(
            model_path=Path(settings.speech_model_path).expanduser(),
            voices_path=Path(settings.speech_voices_path).expanduser(),
        )


# ── ALIGN ─────────────────────────────────────────────────────────────────────


class AlignStage:
    """Listen back to the dialogue, so the captions follow the voices word by word.

    Best-effort by design. Without a transcriber the video is still drawn —
    the scenes are timed from the lines regardless — but without captions,
    and the stage says so.
    """

    name = StageName.ALIGN
    lane = Lane.GPU

    def __init__(
        self, *, transcriber_factory: Callable[[StageContext], Aligner | None] | None = None
    ) -> None:
        self._transcriber_factory = transcriber_factory

    def run(self, context: StageContext) -> StageOutcome:
        narration = _checkpoint_of(context, StageName.NARRATE)
        path = Path(str(narration.get("path") or ""))
        if not narration.get("path") or not path.is_file():
            raise ComposeError(
                "the narration is no longer on disk; the job will speak it again",
                retryable=True,
                code="COMPOSE_LOST",
            )
        transcriber = self._build(context)
        if transcriber is None:
            return StageOutcome(
                skipped=True,
                detail="no transcriber on this worker: the captions are left off",
            )
        context.progress("Listening back to the dialogue")
        try:
            result = transcriber.transcribe(path, source_id=f"compose-{context.job.id}")
        except Exception as exc:  # noqa: BLE001 - alignment is best-effort
            log.warning("compose.align_failed", error=str(exc)[:200])
            return StageOutcome(
                skipped=True,
                detail=f"the dialogue could not be aligned ({str(exc)[:160]}): the captions "
                "are left off",
            )
        words = [w for s in result.transcript.segments for w in (s.words or [])]
        return StageOutcome(
            checkpoint={
                "words": [
                    {
                        "text": w.text,
                        "startSec": round(w.start_sec, 3),
                        "endSec": round(w.end_sec, 3),
                    }
                    for w in words
                ],
                "count": len(words),
            },
            detail=f"{len(words)} words timed",
        )

    def _build(self, context: StageContext) -> Aligner | None:
        if self._transcriber_factory is not None:
            return self._transcriber_factory(context)
        try:
            from clipforge.models.whisper import WhisperTranscriber
        except ImportError:
            return None
        settings = context.settings
        return WhisperTranscriber(
            model=settings.whisper_model,
            compute_type=settings.whisper_compute_type,
            device=settings.whisper_device,
            broker=context.broker,
        )


# ── DRAW ──────────────────────────────────────────────────────────────────────

SceneRenderer = Callable[..., AssembledClip]


class DrawStage:
    """Draw every scene as a silent segment, one checkpoint per scene."""

    name = StageName.DRAW
    lane = Lane.CPU

    def __init__(self, *, workspace: Workspace, renderer: SceneRenderer | None = None) -> None:
        self._workspace = workspace
        self._render: SceneRenderer = renderer or render_scene

    def run(self, context: StageContext) -> StageOutcome:
        options = _options(context)
        settings = context.settings
        scenes = _scenes_from(_checkpoint_of(context, StageName.SCRIPT))
        if not scenes:
            raise ComposeError("SCRIPT left no scenes to draw", code="COMPOSE_NO_SCRIPT")
        narration = _checkpoint_of(context, StageName.NARRATE)
        total = float(narration.get("durationSec") or 0.0)
        if total <= 0:
            raise ComposeError("NARRATE recorded no duration", code="COMPOSE_NO_SCRIPT")
        timed = time_scenes(scenes, lines=_timeline_from(narration), total_sec=total)

        folder = compose_dir(self._workspace, context.job.id)
        folder.mkdir(parents=True, exist_ok=True)
        done: dict[str, Any] = dict((context.checkpoint or {}).get("scenes") or {})
        profile = load_profile(settings.render_profile)
        seed = options.seed or 0

        for scene in timed:
            key = str(scene.index)
            existing = done.get(key)
            if existing and Path(str(existing.get("path"))).is_file():
                continue
            if context.stopping():
                return StageOutcome(incomplete=True, checkpoint={"scenes": done})
            label = f"Drawing scene {scene.index + 1} of {len(timed)}"
            context.progress(label)
            try:
                rendered = self._render(
                    scene,
                    folder / f"scene-{scene.index:02d}.mp4",
                    profile=profile,
                    seed=seed,
                    fps=settings.compose_fps,
                    supersample=settings.compose_supersample,
                    encoder=settings.video_encoder,
                    ffmpeg_bin=settings.ffmpeg_bin,
                    on_progress=lambda i, n, label=label: context.progress(
                        f"{label}: frame {i} of {n}"
                    ),
                )
            except RenderError as exc:
                raise ComposeError(
                    f"scene {scene.index + 1} could not be drawn: {exc}",
                    retryable=True,
                    code="COMPOSE_DRAW",
                ) from exc
            done[key] = {
                "path": str(rendered.path),
                "startSec": scene.start_sec,
                "endSec": scene.end_sec,
                "durationSec": round(rendered.duration_sec, 3),
            }
        return StageOutcome(
            checkpoint={
                "scenes": done,
                "timed": [_timed_dict(scene) for scene in timed],
                "fps": settings.compose_fps,
            },
            detail=f"drew {len(timed)} scenes over {total:.0f}s",
        )


def _timed_dict(scene: TimedScene) -> dict[str, Any]:
    return {
        **_scene_dict(scene),
        "startSec": scene.start_sec,
        "endSec": scene.end_sec,
        "spoken": [
            {
                "speaker": line.speaker,
                "text": line.text,
                "voice": line.voice,
                "startSec": line.start_sec,
                "endSec": line.end_sec,
            }
            for line in scene.timed_lines
        ],
    }


# ── ASSEMBLE ──────────────────────────────────────────────────────────────────


class ComposeAssembleStage:
    """Join the scenes behind a card, lay the voices over, burn the captions, write the clip."""

    name = StageName.ASSEMBLE
    lane = Lane.CPU

    def __init__(self, *, clips: ClipStore, workspace: Workspace, blobs: BlobStore) -> None:
        self._clips = clips
        self._workspace = workspace
        self._blobs = blobs

    def run(self, context: StageContext) -> StageOutcome:
        options = _options(context)
        settings = context.settings
        script_cp = _checkpoint_of(context, StageName.SCRIPT)
        narration_cp = _checkpoint_of(context, StageName.NARRATE)
        align_cp = _checkpoint_of(context, StageName.ALIGN)
        draw_cp = _checkpoint_of(context, StageName.DRAW)

        drawn = draw_cp.get("scenes") or {}
        ordered = [drawn[key] for key in sorted(drawn, key=int)]
        if not ordered:
            raise ComposeError("DRAW left nothing to assemble", code="COMPOSE_NOTHING")
        missing = [e for e in ordered if not Path(str(e.get("path"))).is_file()]
        narration_path = Path(str(narration_cp.get("path") or ""))
        if missing or not narration_path.is_file():
            raise ComposeError(
                "the drawn scenes or the narration are no longer on disk; the job will "
                "draw them again",
                retryable=True,
                code="COMPOSE_LOST",
            )

        profile = load_profile(settings.render_profile)
        clip_id = uuid.uuid4().hex
        scratch = self._workspace.tmp_dir / f"compose-out-{clip_id}"
        scratch.mkdir(parents=True, exist_ok=True)
        title = str(script_cp.get("title") or options.topic).strip()[:200]
        script = str(script_cp.get("script") or "")
        warnings: list[str] = []

        try:
            parts: list[tuple[Path, float]] = []
            offset = 0.0
            if options.title_card is not False:
                context.progress("Rendering the title card")
                card = render_title_card(
                    title,
                    scratch / "card.mp4",
                    profile=profile,
                    work_dir=scratch,
                    encoder=settings.video_encoder,
                    ffmpeg_bin=settings.ffmpeg_bin,
                )
                parts.append((card.path, card.duration_sec))
                offset = card.duration_sec
            parts += [(Path(str(e["path"])), float(e["durationSec"])) for e in ordered]

            context.progress("Joining the scenes")
            silent = concat_segments(
                [path for path, _ in parts],
                scratch / "silent.mp4",
                durations=[duration for _, duration in parts],
                transition=CompileTransition.CUT,
                profile=profile,
                encoder=settings.video_encoder,
                ffmpeg_bin=settings.ffmpeg_bin,
            )

            subtitles: Path | None = None
            words = _words_from(align_cp)
            if options.captions is not False:
                if words:
                    cues = group_into_cues(
                        words,
                        style=profile.captions,
                        clip_start_sec=-offset,
                        clip_end_sec=silent.duration_sec - offset,
                    )
                    if cues:
                        subtitles = scratch / "captions.ass"
                        subtitles.write_text(
                            build_ass(cues, style=profile.captions), encoding="utf-8", newline="\n"
                        )
                else:
                    warnings.append("captions were left off: the dialogue was not aligned")

            context.progress("Laying the voices over the pictures")
            final = _mux(
                silent.path,
                narration_path,
                scratch / "clip.mp4",
                offset_sec=offset,
                duration_sec=silent.duration_sec,
                subtitles=subtitles,
                profile=profile,
                encoder=settings.video_encoder,
                ffmpeg_bin=settings.ffmpeg_bin,
            )
            try:
                images = extract_poster(
                    final.path,
                    duration_sec=final.duration_sec,
                    work_dir=scratch,
                    ffmpeg_bin=settings.ffmpeg_bin,
                )
            except PosterError as exc:
                raise ComposeError(f"the poster could not be taken: {exc}", retryable=True) from exc

            context.progress("Uploading the video")
            ref = self._blobs.put(
                f"clips/{context.job.uid}/{clip_id}.mp4", final.path, content_type="video/mp4"
            )

            now = datetime.now(UTC)
            cast_record = [
                ComposeCharacter(
                    name=str(member.get("name") or "")[:40],
                    kind=VoiceKind(member["kind"]) if member.get("kind") else None,
                    voice=str(member.get("voice") or "")[:40],
                )
                for member in (narration_cp.get("cast") or [])[:5]
                if member.get("name")
            ]
            scenes_record = [
                ComposeScene(
                    lines=[
                        ComposeLine(
                            speaker=str(line.get("speaker") or "")[:40],
                            text=str(line.get("text") or "")[:300],
                            voice=str(line.get("voice") or "")[:40],
                            start_sec=round(float(line.get("startSec") or 0.0) + offset, 3),
                            end_sec=round(float(line.get("endSec") or 0.0) + offset, 3),
                        )
                        for line in (s.get("spoken") or [])[:6]
                    ],
                    start_sec=round(float(s.get("startSec") or 0.0) + offset, 3),
                    end_sec=round(float(s.get("endSec") or 0.0) + offset, 3),
                    actors=[
                        StickActor(
                            name=str(a.get("name") or "someone")[:40],
                            pose=StickPose(a.get("pose") or "STAND"),
                            mood=StickMood(a.get("mood") or "NEUTRAL"),
                        )
                        for a in (s.get("actors") or [])[:3]
                    ],
                    props=[StickProp(p) for p in (s.get("props") or [])[:3]],
                    mood=SceneMood(s.get("mood") or "CALM"),
                    label=(s.get("label") or None),
                )
                for s in (draw_cp.get("timed") or [])[:16]
            ]
            clip = Clip(
                id=clip_id,
                uid=context.job.uid,
                candidate_id=f"compose-{context.job.id}",
                source_id=None,
                job_id=context.job.id,
                lineage_id=clip_id,
                version=1,
                location=(
                    ClipLocation.REMOTE
                    if (ref.storage_path or ref.playback_url)
                    else ClipLocation.LOCAL
                ),
                local_path=str(ref.local_path),
                playback_url=ref.playback_url,
                storage_path=ref.storage_path,
                playback_expires_at=ref.expires_at,
                duration_sec=round(final.duration_sec, 3),
                width_px=final.width,
                height_px=final.height,
                size_bytes=ref.size_bytes,
                render_profile=profile.identifier,
                title=title,
                description=_describe(options, script, cast_record)[:2000],
                tags=_tags(options.topic),
                review=ReviewState.PENDING,
                compose=AppliedCompose(
                    topic=options.topic[:200],
                    title=title,
                    script=script[:3000],
                    cast=cast_record,
                    scenes=scenes_record
                    or [
                        ComposeScene(
                            lines=[],
                            start_sec=offset,
                            end_sec=round(final.duration_sec, 3),
                            actors=[],
                            props=[],
                            mood=SceneMood.CALM,
                        )
                    ],
                    voice=str(narration_cp.get("voice") or options.voice or "")[:40],
                    engine=str(narration_cp.get("engine") or "")[:80],
                    language=str(narration_cp.get("language") or options.language or "en-us")[:16],
                    style=options.style or ComposeStyle.STICK,
                    seed=options.seed or 0,
                    script_by=ScriptAuthor(script_cp.get("scriptBy") or "MODEL"),
                    model_version=script_cp.get("modelVersion"),
                    prompt_version=script_cp.get("promptVersion"),
                    title_card=options.title_card is not False,
                    captions=subtitles is not None,
                    warnings=warnings[:8],
                    trend_id=options.trend_id,
                ),
                created_at=now,
            )
            usable = images.width_px > 0 and images.height_px > 0
            self._clips.save(
                clip,
                preview=ClipPreview(
                    clip_id=clip_id,
                    poster_base64=images.poster_base64,
                    filmstrip_base64=images.filmstrip_base64,
                    width_px=images.width_px,
                    height_px=images.height_px,
                    byte_size=images.byte_size,
                    created_at=now,
                )
                if usable
                else None,
            )
            return StageOutcome(
                checkpoint={
                    "clipId": clip_id,
                    "scenes": len(ordered),
                    "durationSec": round(final.duration_sec, 2),
                    "captions": subtitles is not None,
                },
                detail=f"drew a {final.duration_sec:.0f}s video in {len(ordered)} scenes",
            )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
            shutil.rmtree(compose_dir(self._workspace, context.job.id), ignore_errors=True)


def _mux(
    video: Path,
    narration: Path,
    destination: Path,
    *,
    offset_sec: float,
    duration_sec: float,
    subtitles: Path | None,
    profile: RenderProfile,
    encoder: str,
    ffmpeg_bin: str,
    timeout_s: float = 900.0,
) -> AssembledClip:
    """The silent join plus the voices, delayed past the card, with captions burned in."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_suffix(destination.suffix + ".partial")
    delay_ms = max(0, round(offset_sec * 1000))
    video_chain = (
        f"[0:v]subtitles='{escape_filter_path(subtitles)}'[v]"
        if subtitles is not None
        else "[0:v]null[v]"
    )
    graph = f"[1:a]adelay={delay_ms}|{delay_ms},apad[a];{video_chain}"
    argv = [
        ffmpeg_bin,
        "-y",
        "-v",
        "error",
        "-i",
        str(video),
        "-i",
        str(narration),
        "-filter_complex",
        graph,
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-t",
        f"{duration_sec:.3f}",
        *output_args(profile, encoder),
        str(staging),
    ]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, capture_output=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"laying the voices over took longer than {timeout_s:.0f}s") from exc
    if completed.returncode != 0:
        raise RenderError(
            "ffmpeg failed laying the voices over: "
            + completed.stderr.decode(errors="replace")[-800:]
        )
    staging.replace(destination)
    return AssembledClip(
        path=destination,
        duration_sec=duration_sec,
        size_bytes=destination.stat().st_size,
        width=profile.width if hasattr(profile, "width") else 1080,
        height=profile.height if hasattr(profile, "height") else 1920,
    )


def _describe(options: ComposeOptions, script: str, cast: Sequence[ComposeCharacter]) -> str:
    lines = [options.topic.strip()]
    if options.angle:
        lines += ["", options.angle.strip()]
    if cast:
        lines += ["", "Cast: " + ", ".join(f"{member.name} ({member.voice})" for member in cast)]
    lines += ["", script.strip()]
    lines += ["", "Drawn as stick-figure cartoons by ClipForge. No real person is depicted."]
    return "\n".join(lines)


def _tags(topic: str) -> list[str]:
    words = [w.strip(".,;:!?\"'()").lower() for w in topic.split()]
    tags: list[str] = []
    for word in words:
        if len(word) >= 4 and word not in tags:
            tags.append(word)
    return tags[:8]
