"""The REMAKE stage: a reviewer's correction becomes a new clip.

## Why this is its own job

The same reason PUBLISH and MUSIC are. A correction is a thing someone decides
*while watching a finished clip*, which is on the far side of a CLIP job that
ended hours earlier and cannot be reopened. Bolting it on as a fifth CLIP stage
would give every harvest job a stage it can never run (decision D10).

## Why it produces a new clip rather than editing one

Because the clip that was reviewed is the clip that was reviewed. A remake is
a second opinion, not an erratum: the reviewer may well prefer the original once
they see the alternative, and the only way that stays possible is for both to
exist. The new one carries ``derivedFromClipId``, so they are relatable without
being confusable.

## Two paths, and what decides between them

**Reframing re-cuts from the source.** A rendered clip has already thrown the
discarded pixels away; no amount of processing puts back the third of the pitch
that was never in the file. So any framing change needs the original media still
on this machine, and says so plainly when the workspace collector has taken it.

**Re-voicing does not.** Swapping the audio leaves the picture untouched, so the
video stream is copied — instant and lossless — and only the audio is rebuilt.
A voice-only remake therefore costs seconds and works long after the source is
gone.

A remake that does both re-cuts first and mixes onto the result, which is why
the two halves of this module are written to compose rather than to alternate.

## The order captions are decided in

Captions are burned into pixels by RENDER, so a new voice makes the existing
ones a lie — they show words nobody is saying. REBUILD is therefore not a
setting of the render but a consequence of it: the narration is synthesised,
transcribed back with Whisper to recover word timings that match what was
actually *said* rather than what was written, and only then is the picture
rendered with captions built from that. Which is why REBUILD re-cuts the picture
even when the framing is unchanged, and why it is the one voice option that
takes a GPU lease.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from clipforge_contracts import (
    AppliedRemake,
    AppliedVoice,
    Clip,
    ClipLocation,
    ClipPreview,
    Framing,
    FramingMode,
    Lane,
    NoteInterpretation,
    PanKeyframe,
    Preference,
    RemakeOptions,
    ReviewState,
    SpeechMode,
    StageName,
    Transcript,
    UnsupportedAsk,
    VoiceCaptions,
    VoiceOptions,
)

from clipforge.analysis.feedback import ClipFacts, speakability, translation_landed
from clipforge.analysis.preferences import (
    apply_to_options,
    dedupe,
    propose,
    standing_guidance,
)
from clipforge.analysis.remake import (
    apply_interpretation,
    describe,
    interpret_note,
    translate,
)
from clipforge.config import Settings
from clipforge.media.captions import build_ass, group_into_cues
from clipforge.media.ffprobe import MediaInfo, probe
from clipforge.media.framing import FramingError, build_video_chain, resolve_keyframes
from clipforge.media.narration import build_ffmpeg_args as build_narration_args
from clipforge.media.narration import plan_narration
from clipforge.media.poster import PosterError, extract_poster
from clipforge.media.profiles import RenderProfile, load_profile
from clipforge.media.render import RenderError, RenderRequest, render_clip
from clipforge.media.speech import DEFAULT_VOICES, SpeechError, SpeechSynth, kokoro_language
from clipforge.media.tracking import TrackError, plan_track
from clipforge.media.workspace import Workspace
from clipforge.models.broker import InsufficientVramError
from clipforge.models.ollama import OllamaClient
from clipforge.models.whisper import model_version
from clipforge.observability import get_logger
from clipforge.stages.base import StageContext, StageOutcome
from clipforge.store.blobs import BlobStore
from clipforge.store.firestore import (
    CandidateStore,
    ClipStore,
    PreferenceStore,
    SourceStore,
)
from clipforge.store.transcripts import TranscriptArchive

log = get_logger(__name__)

__all__ = ["RemakeStage", "RemakeStageError"]

# Reading one short note. Far smaller than a full analysis run, but it is the
# same model, so it takes the same lease against the same budget.
NOTE_LLM_VRAM_MB = 3600

# What to tell the reviewer about each thing the system cannot do. Phrased as an
# answer to the person who asked, not as a capability gap, because that is what
# they are reading: a sentence next to a clip that is missing something.
_UNSUPPORTED_WORDING: dict[UnsupportedAsk, str] = {
    UnsupportedAsk.REMOVE_WATERMARK: (
        "removing a watermark or channel bug is not something ClipForge can do "
        "\u2014 it would need the area painted over, and nothing here does that"
    ),
    UnsupportedAsk.REMOVE_OVERLAY_TEXT: (
        "removing burnt-in text from the source is not possible; captions this "
        "pipeline added can be removed, but text already in the footage cannot"
    ),
    UnsupportedAsk.CHANGE_MUSIC: (
        "changing the music is a separate job \u2014 use Add music on the clip page, "
        "which replaces or beds a track and records its rights"
    ),
    UnsupportedAsk.ZOOM_ON_SUBJECT: (
        "zooming onto a particular subject is not supported; the closest is "
        "Follow the action, which moves the window to wherever the motion is"
    ),
    UnsupportedAsk.SLOW_MOTION: "changing the speed of the footage is not supported",
    UnsupportedAsk.REORDER_OR_CUT_MIDDLE: (
        "cutting or reordering the middle of a clip is not supported \u2014 only the "
        "start and end can be moved"
    ),
    UnsupportedAsk.COLOUR_OR_GRADE: "colour and grading changes are not supported",
    UnsupportedAsk.SOMETHING_ELSE: ("part of that request is not something ClipForge can do yet"),
}


class Aligned(Protocol):
    """The one field of a transcription result this stage reads."""

    @property
    def transcript(self) -> Transcript: ...


class Aligner(Protocol):
    """The sliver of `WhisperTranscriber` this stage uses.

    A Protocol rather than the class, so a test can rebuild captions without
    CUDA and so this module does not import the GPU extra merely to state a
    type. Narrow on purpose: everything else Whisper offers — speech spans,
    realtime factor, VRAM peaks — belongs to TRANSCRIBE, not here.
    """

    def transcribe(
        self, audio_path: Path, *, source_id: str, language: str | None = None
    ) -> Aligned: ...


class RemakeStageError(RuntimeError):
    """The remake cannot be produced as asked. Never retryable.

    Every failure here is a property of the request or of what is on disk — a
    source the collector has taken, a language with no voice, a framing mode
    with no keyframes — and a retry reproduces each of them exactly while
    spending the time again.
    """


@dataclass(frozen=True)
class _Cut:
    """Where in the source this remake takes its picture from."""

    start_sec: float
    end_sec: float

    @property
    def duration_sec(self) -> float:
        return max(0.0, self.end_sec - self.start_sec)


class RemakeStage:
    """Re-cut, re-frame and re-voice one clip, into a new one."""

    name = StageName.REMAKE
    # The CPU lane, like RENDER and MUSIC. It may take a GPU lease part-way
    # through — to read a note, or to re-align rebuilt captions — and takes it
    # through the broker like everything else rather than by occupying the GPU
    # lane for a job that is mostly ffmpeg.
    lane = Lane.CPU

    def __init__(
        self,
        *,
        settings: Settings,
        clips: ClipStore,
        candidates: CandidateStore,
        sources: SourceStore,
        archive: TranscriptArchive,
        workspace: Workspace,
        blobs: BlobStore,
        preferences: PreferenceStore | None = None,
        speech: SpeechSynth | None = None,
        transcriber: Aligner | None = None,
        client_factory: Callable[[], OllamaClient] | None = None,
    ) -> None:
        self._settings = settings
        self._clips = clips
        self._candidates = candidates
        self._sources = sources
        self._archive = archive
        self._workspace = workspace
        self._blobs = blobs
        self._preferences = preferences
        self._speech = speech
        self._transcriber = transcriber
        self._client_factory = client_factory
        # Anything the stage downgraded rather than refused. Surfaced in the
        # outcome, so a clip that came back missing something says why — the
        # alternative is a reviewer looking at an uncaptioned clip with no
        # explanation anywhere.
        self._notes: list[str] = []
        # Parts of the request understood and NOT carried out. Distinct from
        # `_notes`, which is for things that were done but may disappoint.
        self._refusals: list[str] = []

    # ── Entry point ──────────────────────────────────────────────────────────

    def run(self, context: StageContext) -> StageOutcome:
        job = context.job
        options = job.remake_options
        if options is None or not job.clip_id:
            raise RemakeStageError("a REMAKE job needs a clipId and remakeOptions")

        original = self._clips.get(job.clip_id)
        if original is None:
            raise RemakeStageError(f"no such clip: {job.clip_id}")

        accepted = self._accepted_preferences(original)
        resolved, interpretation = self._resolve(context, options, original, accepted)
        resolved, used = _prefill(resolved, accepted)
        cut = self._cut(original, resolved)
        profile = load_profile(resolved.profile or _profile_name(original.render_profile))

        self._notes = []  # per run, not per stage instance
        self._refusals = []
        clip_id = uuid.uuid4().hex
        scratch = self._workspace.tmp_dir / f"remake-{clip_id}"
        scratch.mkdir(parents=True, exist_ok=True)

        try:
            voice, utterance_text = self._narrate(context, resolved.voice, original, cut, scratch)
            picture, keyframes = self._picture(
                context, resolved, original, cut, profile, scratch, voice, utterance_text
            )
            final = self._mix(resolved.voice, picture, voice, original, cut, scratch)
            outcome = self._publish(
                context,
                clip_id=clip_id,
                original=original,
                final=final,
                profile=profile,
                options=resolved,
                interpretation=interpretation,
                keyframes=keyframes,
                voice=voice,
                cut=cut,
                scratch=scratch,
            )
            # After the clip exists, never before. Learning is a bonus pass over
            # a finished result — a model that is unreachable, slow or unhelpful
            # must cost the lesson and nothing else.
            self._learn(context, original, resolved, used)
            return outcome
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    # ── Reading the request ──────────────────────────────────────────────────

    def _resolve(
        self,
        context: StageContext,
        options: RemakeOptions,
        original: Clip,
        accepted: list[Preference],
    ) -> tuple[RemakeOptions, NoteInterpretation | None]:
        """Fold any note into the request, leaving stated fields alone."""
        note = (options.notes or "").strip()
        if not note or not options.interpret_notes:
            return options, None

        client = self._ollama()
        if client is None or not client.is_available():
            log.info("remake.no_model", reason="ollama unreachable")
            return options, NoteInterpretation(
                understood=False,
                summary=(
                    "The local model was not reachable, so the note was recorded but not "
                    "acted on. The remake used only the settings chosen explicitly."
                ),
                model=None,
            )

        try:
            with context.broker.acquire(f"ollama:{client.model}", NOTE_LLM_VRAM_MB):
                answer, interpretation = interpret_note(
                    client,
                    note,
                    options=options,
                    duration_sec=original.duration_sec,
                    # What this reviewer has already taught. Shown so they do
                    # not have to say it again to be understood, and so the
                    # model does not read a note as contradicting a rule it is
                    # merely silent about.
                    standing=standing_guidance(accepted),
                )
        except InsufficientVramError as exc:
            # The note is optional enrichment — it may only fill gaps the reviewer
            # left. Letting a busy GPU abort the whole stage would destroy the work
            # that *was* fully specified, and on a job with one attempt that is
            # permanent. The broker is built with no wait, so this fires whenever
            # another model happens to be resident.
            log.info("remake.note_no_vram", error=str(exc))
            return options, NoteInterpretation(
                understood=False,
                summary=(
                    "There was not enough free VRAM to read the note, so it was recorded "
                    "but not acted on. The remake used only the settings chosen explicitly."
                ),
                model=None,
            )
        applied = apply_interpretation(options, answer)

        # Anything the note asked for that no control can express. Recorded on
        # the clip rather than dropped: a reviewer who asks for a watermark to
        # be removed and gets back a clip with the watermark on it cannot tell a
        # refusal from a misunderstanding from a bug.
        for ask in (answer.unsupported if answer else None) or []:
            self._refusals.append(_UNSUPPORTED_WORDING[ask])
        # A conflict is not a refusal: the remake was made, it just resolved an
        # argument between the note and the form. The reviewer still needs to
        # see which side won.
        self._notes.extend(applied.conflicts)

        return applied.options, NoteInterpretation(
            understood=interpretation.understood,
            # Rebuilt from what was actually applied. The model's own wording
            # describes its answer, and the topics gate discards part of that
            # answer afterwards.
            summary=describe(answer, applied),
            model=interpretation.model,
        )

    def _accepted_preferences(self, original: Clip) -> list[Preference]:
        """What has already been learned about this kind of clip."""
        if self._preferences is None:
            return []
        try:
            return self._preferences.accepted_for_source(original.source_id)
        except Exception as exc:  # noqa: BLE001 - a remake must not fail over this
            log.warning("remake.preferences_unreadable", error=str(exc))
            return []

    def _learn(
        self,
        context: StageContext,
        original: Clip,
        resolved: RemakeOptions,
        used: list[Preference],
    ) -> None:
        """Ask what generalises, and record it as a proposal.

        Only after a remake that actually carried feedback: a correction made
        entirely with the controls, with nothing written, teaches nothing a
        default could not already express. And nothing here is applied — a
        proposal sits until a human accepts it, because a wrong standing rule
        shapes every later clip and the reviewer has no reason to suspect it.
        """
        if self._preferences is None:
            return
        try:
            self._preferences.mark_applied(used)
        except Exception as exc:  # noqa: BLE001
            log.warning("remake.preference_count_failed", error=str(exc))

        note = (resolved.notes or "").strip()
        if not note or not resolved.interpret_notes:
            return

        client = self._ollama()
        if client is None or not client.is_available():
            return

        saved = self._clips.get(original.id)
        applied = saved.remake if saved else None
        if applied is None:
            return

        source = self._sources.get(original.source_id) if original.source_id else None
        try:
            known = self._preferences.for_source(original.source_id)
            with context.broker.acquire(f"ollama:{client.model}", NOTE_LLM_VRAM_MB):
                proposed = propose(
                    client,
                    note=note,
                    applied=applied,
                    clip=original,
                    source_title=source.title if source else None,
                    known=known,
                    uid=context.job.uid,
                )
            fresh = dedupe(proposed, known)
            self._preferences.save_all(fresh)
            if fresh:
                log.info(
                    "remake.learned",
                    count=len(fresh),
                    lessons=[p.lesson[:80] for p in fresh],
                )
        except (InsufficientVramError, Exception) as exc:  # noqa: BLE001
            log.info("remake.learning_skipped", error=str(exc))

    def _cut(self, original: Clip, options: RemakeOptions) -> _Cut:
        """Where in the source to take the picture from, after any nudge.

        Nudges are relative to **the clip being watched**, not to the candidate
        that started the lineage. A remake of a remake must therefore start from
        the window its parent actually used, which is why `_publish` records
        `startSec`/`endSec` on every clip it makes.

        Without that, a second nudge silently discarded the first: every
        generation resolved back through `candidateId` — which is copied onto
        each new clip — to the same original window. Worse in the voice-only
        path, where the picture is copied rather than re-cut: the mix is capped
        at the computed window, so a clip that had been *lengthened* was
        silently truncated back, losing footage the reviewer had approved.

        The candidate remains the fallback, and the only source of truth for a
        clip that RENDER made.
        """
        previous = original.remake
        if previous is not None:
            base_start, base_end = previous.start_sec, previous.end_sec
        else:
            candidate = self._candidates.get(original.candidate_id)
            if candidate is None:
                raise RemakeStageError(
                    "this clip's candidate is gone, and it is the only record of where in the "
                    "source the clip was cut from. A remake cannot re-cut without it."
                )
            base_start, base_end = candidate.start_sec, candidate.end_sec

        start = max(0.0, base_start + float(options.start_delta_sec or 0.0))
        end = base_end + float(options.end_delta_sec or 0.0)
        if end - start < 1.0:
            raise RemakeStageError(
                f"those nudges leave a clip of {end - start:.1f}s. "
                "Start and end have moved past each other."
            )
        return _Cut(start_sec=start, end_sec=end)

    # ── The voice ────────────────────────────────────────────────────────────

    def _narrate(
        self,
        context: StageContext,
        voice: VoiceOptions | None,
        original: Clip,
        cut: _Cut,
        scratch: Path,
    ) -> tuple[AppliedVoice | None, str]:
        """Synthesise the narration, translating first when asked.

        Returns the provenance and the spoken text — the latter separately
        because the caption rebuild needs the words, and they are not
        necessarily the words that were requested.
        """
        if voice is None:
            return None, ""

        if self._speech is None:
            raise RemakeStageError(
                "this worker has no speech synthesiser configured. "
                "Install it with: uv sync --project apps/worker --extra speech"
            )

        script = (voice.script or "").strip()
        translated = False
        from_transcript = False
        if not script:
            script, from_transcript = self._spoken_words(original, cut)

        # Is this worth saying at all? Every check here exists because a clip
        # shipped without it: a hallucinated "thanks for watching" over a goal,
        # a feedback note read aloud, two words of commentary stretched into a
        # narration. See clipforge.analysis.feedback.
        verdict = speakability(script, self._facts(original, cut), from_transcript=from_transcript)
        self._notes.extend(verdict.warnings)
        if not verdict.ok:
            raise RemakeStageError(verdict.refusal or "there is nothing worth narrating here")

        if voice.translate and script:
            client = self._ollama()
            if client is None or not client.is_available():
                raise RemakeStageError(
                    "a translation was asked for but the local model is not reachable. "
                    "Speaking the original words with another language's voice produces "
                    "something no listener wants, so this stops rather than guessing."
                )
            try:
                with context.broker.acquire(f"ollama:{client.model}", NOTE_LLM_VRAM_MB):
                    source_text = script
                    script = translate(client, script, target_language=voice.language)
                    # Did it actually translate? Handed unpunctuated ASR text, a
                    # 4B will restore the punctuation and hand back the same
                    # language — a plausible reading of the input, and one that
                    # produced a clip recorded as English that spoke French.
                    if not translation_landed(source_text, script, target_language=voice.language):
                        raise RemakeStageError(
                            f"the translation into {voice.language} came back as the same "
                            "text it was given, which happens when the transcript is too "
                            "rough to translate. Nothing was narrated. Write the line you "
                            "want spoken, or keep the clip's own audio."
                        )
            except InsufficientVramError as exc:
                # Unlike the note, this one is load-bearing: speaking the original
                # words with another language's voice is not a degraded result, it
                # is a wrong one. Refuse, and say what to do about it.
                raise RemakeStageError(
                    f"there was not enough free VRAM to translate the script ({exc}). "
                    "Wait for the running job to finish, or write the script by hand."
                ) from exc
            translated = True

        destination = scratch / "narration.wav"
        try:
            utterance = self._speech.speak(
                script,
                voice=voice.voice,
                language=voice.language,
                speed=float(voice.speed or 1.0),
                destination=destination,
            )
        except SpeechError as exc:
            raise RemakeStageError(str(exc)) from exc

        return (
            AppliedVoice(
                mode=voice.mode,
                voice=utterance.voice,
                language=utterance.language,
                engine=utterance.engine,
                translated=translated,
                spoken_text=utterance.text[:4000],
                speech_duration_sec=round(utterance.duration_sec, 3),
            ),
            utterance.text,
        )

    def _facts(self, original: Clip, cut: _Cut) -> ClipFacts:
        """What is true about this clip, for the feedback layer to reason about."""
        transcript = self._transcript(original)
        words = (
            [
                word
                for segment in transcript.segments
                for word in (segment.words or [])
                if word.end_sec > cut.start_sec and word.start_sec < cut.end_sec
            ]
            if transcript is not None
            else []
        )
        scored = [w.probability for w in words if w.probability is not None]
        previous = original.remake.voice if original.remake else None
        source = self._sources.get(original.source_id) if original.source_id else None
        return ClipFacts(
            duration_sec=float(original.duration_sec or cut.duration_sec),
            spoken_language=(
                previous.language if previous else (transcript.language if transcript else None)
            ),
            source_language=transcript.language if transcript else None,
            word_count=len(words),
            mean_confidence=(sum(scored) / len(scored)) if scored else None,
            source_available=bool(
                source and source.local_path and Path(source.local_path).is_file()
            ),
            can_speak=self._speech is not None,
            can_rebuild_captions=self._transcriber is not None,
            is_derived=bool(original.derived_from_clip_id),
        )

    def _spoken_words(self, original: Clip, cut: _Cut) -> tuple[str, bool]:
        """What this clip says, and whether the words came from the recogniser.

        A clip that has already been re-voiced says whatever its narration says,
        and `AppliedVoice.spokenText` is the exact text a synthesiser was given
        — so for a derived clip that is the truth, and it is also clean,
        punctuated prose rather than ASR output.

        Reading the source transcript instead was how a remake of a Spanish clip
        went back to the original French: the lineage resets to the footage on
        every generation, so translating "the clip" translated something the
        reviewer had already replaced.
        """
        previous = original.remake.voice if original.remake else None
        if previous is not None and previous.spoken_text:
            return previous.spoken_text, False

        transcript = self._transcript(original)
        if transcript is None:
            return "", True
        words = [
            word.text
            for segment in transcript.segments
            for word in (segment.words or [])
            if word.end_sec > cut.start_sec and word.start_sec < cut.end_sec
        ]
        return " ".join(w.strip() for w in words if w.strip()), True

    # ── The picture ──────────────────────────────────────────────────────────

    def _picture(
        self,
        context: StageContext,
        options: RemakeOptions,
        original: Clip,
        cut: _Cut,
        profile: RenderProfile,
        scratch: Path,
        voice: AppliedVoice | None,
        spoken_text: str,
    ) -> tuple[Path, list[PanKeyframe]]:
        """Produce the video, re-cutting from source only when it is needed.

        The cheap path is deliberately first and deliberately common: a
        voice-only remake leaves the picture exactly as it was reviewed, so
        there is nothing to render and the existing file is handed on.
        """
        rebuilding_captions = (
            voice is not None
            and options.voice is not None
            and options.voice.captions is not VoiceCaptions.KEEP
        )
        reframing = options.framing is not None
        retrimming = bool(options.start_delta_sec or options.end_delta_sec)

        if not (reframing or retrimming or rebuilding_captions):
            existing = Path(original.local_path)
            if not existing.is_file():
                raise RemakeStageError(
                    f"the clip's file is gone from this machine ({existing.name}), and this "
                    "remake does not re-cut from the source, so there is nothing to work from."
                )
            return existing, []

        source = self._source_media(original)
        media = probe(source, ffprobe_bin=self._settings.ffprobe_bin)

        keyframes = self._keyframes(options, source, cut, media)
        subtitles = self._subtitles(
            options, original, cut, profile, scratch, voice, spoken_text, context
        )

        destination = scratch / "picture.mp4"
        try:
            graph = build_video_chain(
                media=media,
                profile=profile,
                framing=options.framing,
                keyframes=keyframes,
                subtitles_expr=_subtitles_filter(subtitles),
            )
        except FramingError as exc:
            raise RemakeStageError(str(exc)) from exc

        try:
            render_clip(
                RenderRequest(
                    source=source,
                    destination=destination,
                    start_sec=cut.start_sec,
                    end_sec=cut.end_sec,
                    profile=profile,
                    # Both already resolved into `video_filter` above: the
                    # framing and the caption burn have to compose into one
                    # graph, so they are built together rather than reassembled
                    # here from parts.
                    subtitles=None,
                    encoder=self._settings.video_encoder,
                    video_filter=graph,
                ),
                media,
                ffmpeg_bin=self._settings.ffmpeg_bin,
            )
        except RenderError as exc:
            raise RemakeStageError(f"the reframed clip would not render: {exc}") from exc

        return destination, keyframes

    def _keyframes(
        self, options: RemakeOptions, source: Path, cut: _Cut, media: MediaInfo
    ) -> list[PanKeyframe]:
        """The window's path, from the reviewer or from the footage."""
        framing = options.framing
        if framing is None or framing.mode is not FramingMode.TRACK:
            return resolve_keyframes(framing)

        try:
            result = plan_track(
                source,
                start_sec=cut.start_sec,
                end_sec=cut.end_sec,
                # The crop's width depends on the source's shape, and the
                # tracker has to score the window the render will actually use.
                source_aspect=(
                    media.width / media.height if media.width and media.height else 16 / 9
                ),
                smoothing_sec=float(framing.smoothing_sec or 2.0),
                max_pan_pct_per_sec=float(framing.max_pan_pct_per_sec or 12.0),
                zoom=float(framing.zoom or 1.0),
                ffmpeg_bin=self._settings.ffmpeg_bin,
            )
        except TrackError as exc:
            raise RemakeStageError(f"the footage could not be analysed: {exc}") from exc

        if not result.is_confident:
            # Still rendered, not refused. A tracked crop over footage with
            # little movement is a centred crop, which is exactly what the clip
            # already had — so the honest outcome is a clip plus a record of how
            # little the tracker found, rather than a failure the reviewer
            # cannot act on.
            log.info(
                "remake.track_unconfident",
                coverage=round(result.coverage, 2),
                samples=result.samples,
            )
        return resolve_keyframes(framing, tracked=result.keyframes)

    def _subtitles(
        self,
        options: RemakeOptions,
        original: Clip,
        cut: _Cut,
        profile: RenderProfile,
        scratch: Path,
        voice: AppliedVoice | None,
        spoken_text: str,
        context: StageContext,
    ) -> Path | None:
        """The caption file for the re-rendered picture, if it has captions.

        Three cases, and the interesting one is the third:

        * No new voice — captions come from the source's transcript, as RENDER
          would have built them.
        * REMOVE — no captions at all.
        * REBUILD — the narration is transcribed *back*, so the captions match
          what the synthesiser actually said. A synthesiser does not read a
          sentence at the speed the script implies, and captions cut from the
          script drift within a few seconds.
        """
        wanted = options.voice.captions if options.voice is not None else None

        if wanted is VoiceCaptions.REMOVE:
            return None
        if voice is None or wanted is VoiceCaptions.KEEP:
            transcript = self._transcript(original)
            return self._write_ass(transcript, cut, profile, scratch, rebased=True)

        # REBUILD.
        narration = scratch / "narration.wav"
        if not narration.is_file():
            log.warning("remake.no_narration_to_align", clip=original.id)
            return None

        aligned = self._align(context, narration, spoken_text)
        if aligned is None:
            return None
        return self._write_ass(
            aligned, _Cut(0.0, cut.duration_sec), profile, scratch, rebased=False
        )

    def _align(self, context: StageContext, narration: Path, spoken_text: str) -> Transcript | None:
        """Recover word timings from the narration we just generated.

        Reuses the existing Whisper loader rather than adding an aligner: the
        model is already here, already brokered, and transcribing thirty seconds
        of clean synthetic speech is a few seconds of work with none of the
        accuracy problems that make forced alignment its own dependency.
        """
        transcriber = self._build_transcriber(context)
        if transcriber is None:
            # REBUILD quietly becomes REMOVE here, and saying so matters: the
            # reviewer asked for captions and is about to get a clip without any.
            # Cutting them from the script instead would be worse — a synthesiser
            # does not read a sentence at the speed the script implies, so those
            # drift within seconds and read as a bug rather than an omission.
            log.warning(
                "remake.captions_not_rebuilt",
                reason="this worker has no transcriber (the gpu extra is not installed)",
                effect="the clip is rendered without captions",
            )
            self._notes.append("captions could not be rebuilt: this worker has no transcriber")
            return None
        try:
            result = transcriber.transcribe(narration, source_id="remake-narration")
        except Exception as exc:  # noqa: BLE001 - alignment is best-effort
            log.warning(
                "remake.captions_not_rebuilt",
                reason=f"aligning the narration failed: {exc}",
                effect="the clip is rendered without captions",
                spoken_chars=len(spoken_text),
            )
            self._notes.append(f"captions could not be rebuilt: {exc}")
            return None
        return result.transcript

    def _write_ass(
        self,
        transcript: Transcript | None,
        cut: _Cut,
        profile: RenderProfile,
        scratch: Path,
        *,
        rebased: bool,
    ) -> Path | None:
        if transcript is None:
            return None
        words = [word for segment in transcript.segments for word in (segment.words or [])]
        cues = group_into_cues(
            words,
            style=profile.captions,
            clip_start_sec=cut.start_sec if rebased else 0.0,
            clip_end_sec=cut.end_sec if rebased else cut.duration_sec,
        )
        if not cues:
            return None
        path = scratch / "captions.ass"
        path.write_text(build_ass(cues, style=profile.captions), encoding="utf-8", newline="\n")
        return path

    # ── The mix ──────────────────────────────────────────────────────────────

    def _mix(
        self,
        options: VoiceOptions | None,
        picture: Path,
        voice: AppliedVoice | None,
        original: Clip,
        cut: _Cut,
        scratch: Path,
    ) -> Path:
        """Put the narration onto the picture, or hand the picture straight on."""
        if voice is None or options is None:
            return picture

        narration = scratch / "narration.wav"
        media = probe(picture, ffprobe_bin=self._settings.ffprobe_bin)
        # The picture's OWN length, not the window that was planned. On the
        # cheap path the picture is the existing clip file, which may be longer
        # than the computed window — and `-t` would then cut the copied video
        # stream, throwing away footage nobody asked to lose.
        picture_sec = media.duration_sec or cut.duration_sec
        plan = plan_narration(
            mode=options.mode,
            clip_duration_sec=picture_sec,
            speech_duration_sec=float(voice.speech_duration_sec or 0.0),
            gain_db=options.gain_db,
            duck_db=options.duck_db,
            has_original_audio=media.has_audio,
        )
        if not plan.fits:
            # Recorded rather than corrected: the picture is never retimed to
            # fit the audio. See clipforge.media.narration.
            log.warning(
                "remake.narration_overruns",
                by_sec=round(plan.overruns_by_sec, 2),
                clip_sec=round(picture_sec, 2),
            )

        destination = scratch / "mixed.mp4"
        argv = build_narration_args(
            plan,
            clip_path=str(picture),
            speech_path=str(narration),
            output_path=str(destination),
            # The picture is already encoded — either by this stage's reframe or
            # by the RENDER that produced the original — so copying is both
            # correct and free.
            reencode_video=False,
            ffmpeg=self._settings.ffmpeg_bin,
        )
        done = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
        if done.returncode != 0:
            raise RemakeStageError(f"the narration would not mix: {done.stderr.strip()[:400]}")
        if original.duration_sec and plan.mode is SpeechMode.BED and not media.has_audio:
            log.info("remake.bed_without_audio", clip=original.id)
        return destination

    # ── Saving ───────────────────────────────────────────────────────────────

    def _publish(
        self,
        context: StageContext,
        *,
        clip_id: str,
        original: Clip,
        final: Path,
        profile: RenderProfile,
        options: RemakeOptions,
        interpretation: NoteInterpretation | None,
        keyframes: list[PanKeyframe],
        voice: AppliedVoice | None,
        cut: _Cut,
        scratch: Path,
    ) -> StageOutcome:
        media = probe(final, ffprobe_bin=self._settings.ffprobe_bin)
        ref = self._blobs.put(
            f"clips/{context.job.uid}/{clip_id}.mp4", final, content_type="video/mp4"
        )

        try:
            images = extract_poster(
                final,
                duration_sec=media.duration_sec,
                work_dir=scratch,
                ffmpeg_bin=self._settings.ffmpeg_bin,
            )
        except PosterError as exc:
            # A missing poster costs a thumbnail in the review queue, not a
            # clip. Failing the whole remake over it would throw away the render
            # that just succeeded.
            log.warning("remake.poster_failed", error=str(exc))
            images = None

        now = datetime.now(UTC)
        mode = options.framing.mode if options.framing is not None else FramingMode.AS_RENDERED
        remade = Clip(
            id=clip_id,
            uid=context.job.uid,
            candidate_id=original.candidate_id,
            source_id=original.source_id,
            job_id=context.job.id,
            derived_from_clip_id=original.id,
            # One lineage, however many corrections. `lineageId` is inherited
            # rather than recomputed, so the chain stays flat: a remake of a
            # remake of a remake all point at the clip RENDER made, and the
            # review queue can group on one equality rather than walking a list.
            lineage_id=original.lineage_id or original.id,
            version=(original.version or 1) + 1,
            location=(
                ClipLocation.REMOTE
                if (ref.storage_path or ref.playback_url)
                else ClipLocation.LOCAL
            ),
            local_path=str(ref.local_path),
            playback_url=ref.playback_url,
            storage_path=ref.storage_path,
            playback_expires_at=ref.expires_at,
            duration_sec=round(media.duration_sec, 3),
            width_px=media.width,
            height_px=media.height,
            size_bytes=ref.size_bytes,
            render_profile=profile.identifier,
            title=original.title,
            description=original.description,
            # PENDING, always. The clip that was approved is the one this was
            # made from; a correction is a different edit and deserves to be
            # watched before it goes anywhere.
            review=ReviewState.PENDING,
            # The footage is the same footage, so its rights carry over. The
            # voice does not change that and must not be allowed to look as
            # though it did.
            rights=original.rights,
            music=original.music,
            remake=AppliedRemake(
                notes=options.notes,
                interpretation=interpretation,
                framing_mode=mode,
                keyframes=keyframes,
                voice=voice,
                refusals=self._refusals[:8],
                warnings=self._notes[:8],
                start_sec=round(cut.start_sec, 3),
                end_sec=round(cut.end_sec, 3),
            ),
            created_at=now,
        )

        # Same guard as RENDER: a poster whose dimensions could not be read
        # fails ClipPreview validation, and losing a finished remake over a
        # thumbnail is the wrong trade. See clipforge.media.poster._dimensions.
        if images is not None and not (images.width_px > 0 and images.height_px > 0):
            log.warning("remake.preview_unusable", clip_id=clip_id, detail="no poster saved")
            images = None

        preview = (
            ClipPreview(
                clip_id=clip_id,
                poster_base64=images.poster_base64,
                filmstrip_base64=images.filmstrip_base64,
                width_px=images.width_px,
                height_px=images.height_px,
                byte_size=images.byte_size,
                created_at=now,
            )
            if images is not None
            else None
        )
        self._clips.save(remade, preview=preview)

        detail = _describe(mode, voice, keyframes)
        if self._notes:
            detail = f"{detail} ({'; '.join(self._notes)})"
        log.info(
            "remake.done",
            clip_id=clip_id,
            derived_from=original.id,
            framing=mode.value,
            keyframes=len(keyframes),
            voice=voice.voice if voice else None,
            size_mb=round(ref.size_bytes / 1_048_576, 2),
        )
        return StageOutcome(detail=detail, metadata={"clipId": clip_id})

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _source_media(self, original: Clip) -> Path:
        source = self._sources.get(original.source_id) if original.source_id else None
        if source is None or not source.local_path or not Path(source.local_path).is_file():
            raise RemakeStageError(
                "changing the framing means re-cutting from the original video, and it is "
                "no longer on this machine — the workspace collector reclaims sources. "
                "Re-ingest the source and try again, or remake only the voice, which "
                "works on the rendered clip and does not need it."
            )
        return Path(source.local_path)

    def _transcript(self, original: Clip) -> Transcript | None:
        source = self._sources.get(original.source_id) if original.source_id else None
        if source is None or not source.content_hash:
            return None
        return self._archive.load(
            source.content_hash,
            model_version(self._settings.whisper_model, self._settings.whisper_compute_type),
        )

    def _build_transcriber(self, context: StageContext) -> Aligner | None:
        """Whisper, holding the worker's own broker lease.

        Built per use from `context.broker` rather than injected at
        construction, for the same reason TranscribeStage does it: the broker is
        the single authority on what is resident in 6 GB of VRAM, and a stage
        that brought its own would be a second authority that could disagree
        with the first.
        """
        if self._transcriber is not None:
            return self._transcriber
        try:
            from clipforge.models.whisper import WhisperTranscriber
        except ImportError:
            # No GPU extra on this worker. Reframing still works; rebuilt
            # captions do not, and the caller keeps the script's own timings.
            return None
        settings = context.settings
        return WhisperTranscriber(
            model=settings.whisper_model,
            compute_type=settings.whisper_compute_type,
            device=settings.whisper_device,
            broker=context.broker,
        )

    def _ollama(self) -> OllamaClient | None:
        if self._client_factory is not None:
            return self._client_factory()
        return OllamaClient(
            host=self._settings.ollama_host,
            model=self._settings.ollama_model,
            num_ctx=self._settings.ollama_num_ctx,
        )


def _prefill(
    options: RemakeOptions, accepted: list[Preference]
) -> tuple[RemakeOptions, list[Preference]]:
    """Fill in what the reviewer left open, from what they have already taught.

    A preference is a default, and a default that overrides a choice is not a
    default — so this only touches settings this request said nothing about.
    The reviewer's silence is the opening; their note and their form both close
    it.

    Returns the preferences that actually changed something, so the ones that
    never fire can be told apart from the ones carrying the work.
    """
    if not accepted:
        return options, []

    framing_set = options.framing is not None
    voice_set = options.voice is not None
    defaults = apply_to_options(accepted, framing_set=framing_set, voice_set=voice_set)
    if defaults.framing_mode is None and not defaults.language:
        return options, []

    updated = options.model_copy(deep=True)
    used: list[Preference] = []
    if defaults.framing_mode is not None and not framing_set:
        updated.framing = Framing(mode=defaults.framing_mode)
        used.extend(p for p in accepted if p.defaults and p.defaults.framing_mode is not None)
    if defaults.language and not voice_set:
        updated.voice = VoiceOptions(
            mode=SpeechMode.REPLACE,
            voice=DEFAULT_VOICES.get(kokoro_language(defaults.language), "af_heart"),
            language=defaults.language,
            translate=True,
            captions=VoiceCaptions.REBUILD,
        )
        used.extend(p for p in accepted if p.defaults and p.defaults.language)
    return updated, used


def _profile_name(identifier: str | None) -> str:
    """`name:version` back to `name`, for reloading the profile a clip used."""
    if not identifier:
        return "default"
    return identifier.split(":", 1)[0]


def _subtitles_filter(path: Path | None) -> str | None:
    if path is None:
        return None
    text = str(path.resolve()).replace("\\", "/").replace(":", "\\:")
    return f"subtitles='{text}'"


def _describe(mode: FramingMode, voice: AppliedVoice | None, keyframes: list[PanKeyframe]) -> str:
    parts: list[str] = []
    if mode is FramingMode.TRACK:
        parts.append(f"tracked across {len(keyframes)} points")
    elif mode is FramingMode.PAN:
        parts.append(f"panned across {len(keyframes)} points")
    elif mode is FramingMode.FIT:
        parts.append("whole frame fitted")
    if voice is not None:
        spoken = f"{voice.voice} in {voice.language}"
        parts.append(f"re-voiced ({spoken}{', translated' if voice.translated else ''})")
    return ", ".join(parts) if parts else "re-cut"
