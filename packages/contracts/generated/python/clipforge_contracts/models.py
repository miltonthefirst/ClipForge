# ClipForge contracts - GENERATED FILE, DO NOT EDIT.
#
# Source of truth: packages/contracts/schemas/clipforge.json
# Regenerate with:
#   uv run --project apps/worker python packages/contracts/scripts/generate_python.py
#
# Editing this file by hand is pointless: CI regenerates it and fails on any
# difference. Change the schema instead.

from __future__ import annotations

from datetime import date as date_aliased
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, RootModel


class JobStatus(StrEnum):
    """
    Lifecycle of a job. Transitions are defined in docs/PLAN.md 3.3 and enforced by clipforge.scheduler.lease.
    """

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class JobType(StrEnum):
    """
    ECHO is a no-op job of three artificial stages used to exercise the scheduler without touching media. CLIP is the real pipeline. PUBLISH is a separate, single-stage job created after a human approves a clip — publishing cannot be a stage of CLIP because it happens on the far side of a human decision that may take days. MUSIC is the same shape for the same reason: scoring a finished clip is a choice someone makes while watching it, and it produces a new clip rather than altering the one they watched. UPLOAD is how a reviewer on a phone asks for a clip that only exists on the worker's disk: the phone cannot reach the worker, so the request travels as a job like everything else. REMAKE is the correction channel: a reviewer watching a finished clip says what is wrong with it — the framing lost the ball, the voice has to change — and gets a new clip rather than an edited one, for the same reason MUSIC does.
    """

    ECHO = "ECHO"
    CLIP = "CLIP"
    PUBLISH = "PUBLISH"
    MUSIC = "MUSIC"
    UPLOAD = "UPLOAD"
    REMAKE = "REMAKE"


class StageStatus(StrEnum):
    """
    A stage that is DONE is never re-executed on retry. That is what makes a crash during ANALYZE cost seconds rather than the twenty minutes of DOWNLOAD and TRANSCRIBE that preceded it.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class StageName(StrEnum):
    """
    Ordered pipeline steps. ECHO_* belong to the ECHO job type only. UPLOAD is the single stage of an UPLOAD job: it copies a clip that already exists on the worker into the bucket so a phone can play it. REMAKE is the single stage of a REMAKE job: it re-cuts a clip from its original source with the reviewer's corrections applied.
    """

    ECHO_ONE = "ECHO_ONE"
    ECHO_TWO = "ECHO_TWO"
    ECHO_THREE = "ECHO_THREE"
    DOWNLOAD = "DOWNLOAD"
    TRANSCRIBE = "TRANSCRIBE"
    ANALYZE = "ANALYZE"
    RENDER = "RENDER"
    PUBLISH = "PUBLISH"
    MUSIC = "MUSIC"
    UPLOAD = "UPLOAD"
    REMAKE = "REMAKE"


class Lane(StrEnum):
    """
    Which scheduler lane a stage runs in. The GPU lane is depth 1 because Whisper and the LLM cannot be co-resident in 6 GB of VRAM (docs/PLAN.md 2.1).
    """

    GPU = "GPU"
    CPU = "CPU"


class StageError(BaseModel):
    """
    Structured failure detail. Captured rather than raised so a failed stage can be inspected from the PWA without reading worker logs.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    type: str
    """
    Exception class name.
    """
    message: str
    code: str | None = None
    """
    A stable, machine-readable cause the PWA can map to a written explanation — see IngestErrorCode for the ingest stage's vocabulary. Null when a failure has no classified cause, in which case the UI falls back to `message`. Typed as a string rather than an enum so each stage can own its own vocabulary without every stage's codes leaking into every error.
    """
    traceback: str | None = None
    retryable: bool | None = True
    """
    False marks a failure that will never succeed on retry (bad input, a missing tool, an age-restricted video), so the scheduler fails the job immediately instead of burning its remaining attempts.
    """


class Stage(BaseModel):
    """
    One idempotent, checkpointed step of a job.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    name: StageName
    lane: Lane
    status: StageStatus
    started_at: AwareDatetime | None = Field(None, alias="startedAt")
    ended_at: AwareDatetime | None = Field(None, alias="endedAt")
    duration_ms: int | None = Field(None, alias="durationMs", ge=0)
    peak_vram_mb: int | None = Field(None, alias="peakVramMb", ge=0)
    """
    Peak VRAM observed through NVML while this stage held the ModelBroker lease. Null for CPU stages.
    """
    attempts: int | None = Field(0, ge=0)
    checkpoint: dict[str, Any] | None = None
    """
    Opaque, stage-owned resume state. The scheduler persists and returns it verbatim and never interprets it.
    """
    progress: str | None = Field(None, max_length=120)
    """
    One sentence on what this stage is doing right now — 'Fetching the track', 'Analysing the beat grid', 'Mixing'. Null until the stage says something.

    It exists because a long stage was indistinguishable from a hung one: a MUSIC job that ran for thirty minutes produced exactly two events, CLAIMED and STAGE_STARTED, and nothing after them until it finished. A sentence rather than an object with a percentage, because no stage here can honestly compute one — the work being waited on belongs to a remote server or a model, not to a loop with a countable denominator — and a bar stuck at 80 percent for nine minutes reads as broken in a way that a line of prose which keeps changing does not. It sits beside the checkpoint rather than inside it because a checkpoint is opaque to the scheduler by contract, and this is the one thing about a running stage that the scheduler and the PWA both have to read (docs/adr/0007-checkpointed-stage-pipeline.md). It is carried out by the lease heartbeat, which already rewrites the whole job document every 30 seconds, so it costs no Firestore write of its own — job-progress writes being throttled is a requirement rather than an optimisation (docs/adr/0004-dedicated-firebase-project.md).

    Capped at 120 characters, which is longer than any honest description of a step and short enough that whoever sets it has to write a sentence rather than paste a tool's output line. Callers truncate; the cap is a guard, not a formatter.
    """
    error: StageError | None = None


class MusicMode(StrEnum):
    """
    What the added track does to the clip's own audio. BED keeps the original and sits the music under it, ducking automatically so speech stays intelligible — the usual choice for a talking clip. REPLACE removes the original audio entirely and the track becomes the whole soundtrack, which is what a montage or a silent-action clip wants.
    """

    BED = "BED"
    REPLACE = "REPLACE"


class MusicCaptions(StrEnum):
    """
    Captions are burned into the clip's pixels by the RENDER stage, so they cannot be peeled off a finished file — removing them means re-rendering the segment from the original source without the subtitle filter. KEEP is therefore cheap and always available; REMOVE needs the source media to still be on the worker, and fails clearly when it has been garbage-collected. Offered as a choice rather than inferred from the mode because 'music over captions' is a real style, and guessing wrong either way is worse than asking.
    """

    KEEP = "KEEP"
    REMOVE = "REMOVE"


class MusicOptions(BaseModel):
    """
    What to add to a clip, and how. Carried on the MUSIC job that produces the scored version.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    source: str = Field(..., min_length=1)
    """
    A YouTube URL, or a path to an audio file on the worker. Only the audio is ever fetched from a URL — the video of a music source is of no use here and would cost bandwidth for nothing.
    """
    mode: MusicMode
    captions: MusicCaptions
    gain_db: float | None = Field(None, alias="gainDb", ge=-40.0, le=12.0)
    """
    Trim on the music, relative to the level the stage picks. Null means 'use the stage's judgement', which targets a bed roughly 18 LUFS below speech and a replacement at the clip's own loudness target.
    """
    align_to_beat: bool | None = Field(True, alias="alignToBeat")
    """
    Whether to start the music excerpt exactly on a beat, so its pulse lands with the clip's first frame. The music moves to meet the clip, never the other way round: re-cutting the video to fall on a beat would mean the published clip differed from the one that was reviewed, and would force a re-encode to achieve something the listener hears identically either way. Ignored when the track has no tempo clear enough to measure.
    """


class PreferenceScope(StrEnum):
    """
    How widely a learned preference applies. SOURCE is this channel or series only — the right default, because most corrections are about the kind of footage rather than about video in general, and a rule learned from football should not reframe a talking head. EVERYTHING is for a preference that genuinely holds across all of them, which is rarer than it feels while writing one.
    """

    SOURCE = "SOURCE"
    EVERYTHING = "EVERYTHING"


class PreferenceStatus(StrEnum):
    """
    PROPOSED until a human says otherwise, and nothing is applied while it sits there. ACCEPTED means it shapes later remakes; REJECTED means it never comes back. Rejected preferences are kept rather than deleted precisely so the same suggestion cannot be made again on the next correction — that is the difference between a system that learns and one that nags.
    """

    PROPOSED = "PROPOSED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class UnsupportedAsk(StrEnum):
    """
    Something a reviewer asked for that ClipForge cannot do. Recorded rather than ignored, because the alternative is what happened in practice: a reviewer asked for a watermark to be removed, got back a clip with the watermark still on it and no explanation, and had no way to tell 'refused' from 'misunderstood' from 'quietly broken'. It also doubles as the list of what to build next, written by the person who wanted it.

    REMOVE_WATERMARK and REMOVE_OVERLAY_TEXT are kept as members and are no longer refusals: both are now answered by ObscureOptions, and the note reader routes them to the OBSCURE topic. They stay in the enum because stored readings contain them, and because detection can still come back empty — at which point 'nothing static was found to hide' is the honest refusal and this is what it is recorded as.
    """

    REMOVE_WATERMARK = "REMOVE_WATERMARK"
    REMOVE_OVERLAY_TEXT = "REMOVE_OVERLAY_TEXT"
    CHANGE_MUSIC = "CHANGE_MUSIC"
    ZOOM_ON_SUBJECT = "ZOOM_ON_SUBJECT"
    SLOW_MOTION = "SLOW_MOTION"
    REORDER_OR_CUT_MIDDLE = "REORDER_OR_CUT_MIDDLE"
    COLOUR_OR_GRADE = "COLOUR_OR_GRADE"
    SOMETHING_ELSE = "SOMETHING_ELSE"


class ObscureMethod(StrEnum):
    """
    How a region of the picture is hidden.

    Four, because one is wrong for the two cases that matter. DELOGO reconstructs the area from the pixels around it and is far and away the best answer for a small broadcast bug — at channel-logo size it reads as though the logo was never there. It is also the worst answer for anything large: it has nothing to reconstruct from, so a wide region becomes a smear that draws more attention than the thing it hid. BLUR and PIXELATE stay honest at any size, which is what a burnt-in caption needs. BOX is a flat rectangle, for when honest is the point.
    """

    BLUR = "BLUR"
    PIXELATE = "PIXELATE"
    DELOGO = "DELOGO"
    BOX = "BOX"


class ObscureFound(StrEnum):
    """
    Where a region came from. Worth recording because the three fail differently: AUTO can be in the wrong place, MANUAL cannot but costs the reviewer a drag, and REMEMBERED is a decision made once about a channel and applied ever after — which is the one that needs to be visible when it goes wrong, because nobody asked for it on this clip.
    """

    AUTO = "AUTO"
    MANUAL = "MANUAL"
    REMEMBERED = "REMEMBERED"


class ObscureRegion(BaseModel):
    """
    One rectangle of the SOURCE frame to hide, in percentages of its width and height.

    Percentages rather than pixels, and of the source rather than the output, for one reason each. Percentages survive a source that turns out to be 1280 wide when the box was drawn on a 1920 poster. Source coordinates are the only frame a logo is actually fixed in: the output is cropped, panned and scaled, so a box in output coordinates would have to move with the window, and a tracked window would drag the blur across the picture.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    x_pct: float = Field(..., alias="xPct", ge=0.0, le=100.0)
    """
    Left edge, as a percentage of source width.
    """
    y_pct: float = Field(..., alias="yPct", ge=0.0, le=100.0)
    """
    Top edge, as a percentage of source height.
    """
    w_pct: float = Field(..., alias="wPct", ge=0.2, le=100.0)
    h_pct: float = Field(..., alias="hPct", ge=0.2, le=100.0)
    method: ObscureMethod | None = None
    """
    Null lets the worker choose by size, which is the better default: DELOGO for a region small enough to reconstruct, BLUR for anything bigger.
    """
    strength: float | None = Field(None, ge=0.0, le=1.0)
    """
    How hard to hide it, 0 to 1. Scales blur radius and pixel size; ignored by DELOGO, which either reconstructs the area or does not. Null means the default, which is strong enough that the shape underneath is not readable.
    """
    from_sec: float | None = Field(None, alias="fromSec", ge=0.0)
    """
    Seconds from the START OF THE CLIP, not of the source, because that is the timebase the render's filters see. Null means from the beginning. For a bug that only appears during play.
    """
    to_sec: float | None = Field(None, alias="toSec", ge=0.0)
    label: str | None = Field(None, max_length=80)
    """
    What this is, in a couple of words — 'channel bug, top left'. Shown next to the box in the history, so a remembered region is identifiable a month later.
    """
    found: ObscureFound | None = None
    confidence: float | None = Field(None, ge=0.0, le=1.0)
    """
    For an AUTO region: how static and how distinct it was. Recorded rather than thresholded away, because a low-confidence find that turns out to be right is the evidence for loosening the threshold, and one that is wrong is the evidence for the reviewer to drag the box instead.
    """


class ObscureOptions(BaseModel):
    """
    The request to hide things: find them, or hide these, or both.

    `auto` and `regions` compose rather than exclude. A reviewer who has drawn one box and also wants the scoreboard found gets both; detection skips anything that overlaps a box already listed, so asking for both never produces two filters over the same pixels.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    auto: bool | None = False
    """
    Look for static logos and burnt-in text in the footage and hide what is found. Costs a short decode pass over the cut and no model.
    """
    regions: list[ObscureRegion] | None = Field([], max_length=6, validate_default=True)
    """
    Rectangles to hide whatever detection thinks. Always applied.

    Six, and the number is the security rule's rather than this schema's: each region is a filter pass, and a rule that validates them positionally has a thousand-expression budget that eight of them exceeded. Detection returns at most four.
    """
    method: ObscureMethod | None = None
    """
    Override the method for regions that do not name one, found or listed.
    """
    strength: float | None = Field(None, ge=0.0, le=1.0)


class NoteObscure(StrEnum):
    """
    Whether a note asks for something in the picture to be hidden, and where it says that thing is.

    One enum rather than a boolean plus a location, because the location is only meaningful when the answer is yes and a small model given two fields answers them independently — producing 'no, and it is in the top left'. ANYWHERE is the common answer: reviewers write 'blur the canal+' and expect the system to know where the canal+ is, which is exactly what detection is for. A corner narrows the search and is worth having when they do say.
    """

    NOT_MENTIONED = "NOT_MENTIONED"
    ANYWHERE = "ANYWHERE"
    TOP_LEFT = "TOP_LEFT"
    TOP_RIGHT = "TOP_RIGHT"
    BOTTOM_LEFT = "BOTTOM_LEFT"
    BOTTOM_RIGHT = "BOTTOM_RIGHT"
    TOP = "TOP"
    BOTTOM = "BOTTOM"


class NoteFraming(StrEnum):
    """
    A framing decision read out of a note, plus the value that means the note did not make one. NOT_MENTIONED is a member rather than the field being nullable, for a reason measured rather than assumed — see LlmRemakeNote.

    PAN is deliberately absent, though FramingMode has it. A pan is defined by its keyframes and this schema gives the model no way to supply any, so a model answering PAN could only ever produce a framing with an empty keyframe list — which the render path refuses outright, turning a readable note into a failed job. TRACK is the executable form of the same intent: follow the action, with the points worked out from the footage.
    """

    NOT_MENTIONED = "NOT_MENTIONED"
    AS_RENDERED = "AS_RENDERED"
    FIT = "FIT"
    TRACK = "TRACK"


class NoteCrop(StrEnum):
    """
    A side read out of a note, plus the value that means the note did not name one.
    """

    NOT_MENTIONED = "NOT_MENTIONED"
    CENTRE = "centre"
    LEFT = "left"
    RIGHT = "right"


class NoteAudio(StrEnum):
    """
    What a note asked for the original audio. REPLACE removes it, KEEP_UNDER keeps it ducked beneath a narration, NOT_MENTIONED means the note said nothing about sound at all — which is most notes.
    """

    NOT_MENTIONED = "NOT_MENTIONED"
    REPLACE = "REPLACE"
    KEEP_UNDER = "KEEP_UNDER"


class NoteTopic(StrEnum):
    """
    An aspect of a clip a note can be about. Answering this is a much easier question than filling in settings, and it is what bounds the rest of the reading: a field outside the declared topics is ignored, so a model that volunteers a crop for a note about language changes nothing.
    """

    FRAMING = "FRAMING"
    LANGUAGE = "LANGUAGE"
    AUDIO = "AUDIO"
    TIMING = "TIMING"
    OBSCURE = "OBSCURE"


class OnScreenTextItem(RootModel[str]):
    root: str = Field(..., max_length=80)


class LlmVisualContext(BaseModel):
    """
    What a vision model sees in a handful of frames from one clip.

    This exists because the pipeline was narrating footage it had never looked at. A speech recogniser handed French football commentary returns "très mal à beaude glim cette frappe pure latérale gauche municois", a translator faithfully renders that as "very bad beauty glim this pure left lateral munitions shot", and a synthesiser reads it aloud over a goal. Every step did its job. Nothing in the chain could tell that the words were nonsense, because nothing in the chain had seen a football.

    It is **description, never inference**. What is in the frame, what the scoreboard says, what colours the teams wear. Not who is about to score.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    subject: str = Field(..., max_length=200)
    """
    What kind of footage this is, in a phrase. 'A football match, one side in red, the other in yellow.'
    """
    happens: str = Field(..., max_length=700)
    """
    What actually occurs across the frames, in order. The part a narration can be built on.
    """
    on_screen_text: list[OnScreenTextItem] = Field(
        ..., alias="onScreenText", max_length=8
    )
    """
    Text readable in the frames — a scoreline, a clock, team abbreviations, hoardings. Worth having separately because it is the one part of a frame that is unambiguous, and because it is often the thing a caption got wrong.
    """


class LlmNarration(BaseModel):
    """
    A spoken line for one clip, in the language that was asked for.

    Replaces "translate the transcript", which is correct exactly as often as the transcript is. Handed both the transcript AND what is visible, the model can tell which words survived the recogniser and which did not, and can write around the ones that did not.

    `transcriptUsable` comes first and is the whole point of the shape: a model that has committed to an answer about the transcript's quality writes a different script than one that has not been asked. It is the same device `LlmPreferenceProposal` uses, for the same reason — make the decision before the thing that depends on it.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    transcript_usable: bool = Field(..., alias="transcriptUsable")
    """
    Does the transcript carry meaning a listener would want, or has the recogniser mangled it past rescue? Answered before the script exists.
    """
    reasoning: str = Field(..., max_length=300)
    """
    One sentence on that judgement. Not stored — it exists to make the model state a position it then has to write consistently with.
    """
    script: str = Field(..., max_length=1200)
    """
    The words to speak, in the target language and nothing else. No speaker labels, no stage directions, no quotation marks around the whole thing: a synthesiser reads whatever is here, literally.
    """


class Tag(RootModel[str]):
    root: str = Field(..., max_length=40)


class LlmClipMetadata(BaseModel):
    """
    A title, a description and tags for one finished clip.

    The fields they replace were never written to be read. `Clip.title` was `Candidate.hook`, which the analysis prompt defines as "the actual opening line, quoted from the transcript" — so a real clip went out titled `"on a franchi un premier rideau kane peut enroulé du plat"`, lowercase, in French, on a clip that had been re-voiced into English. `Clip.description` was `Candidate.reason`, which is one sentence on *why the clip was selected*: "This segment captures the high-tension moment where Kane breaks through the defense, creating an immediate visual hook for football fans." That is a note from an analyst to a pipeline, and it was being shown to viewers.

    Written for reach rather than for accuracy about the selection: the first few words of a title and the first line of a description are what a feed shows, and they decide whether anything else is read.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    title: str = Field(..., max_length=100)
    """
    The hook, front-loaded, in the target language. What happened and who it happened to, in the first few words, because that is all a feed shows before it truncates.
    """
    description: str = Field(..., max_length=900)
    """
    First line carries the hook again — it is the only line most viewers see. Then a sentence or two of context, then hashtags on their own line.
    """
    tags: list[Tag] = Field(..., max_length=15, min_length=5)
    """
    Search terms, lowercase, broad ones and specific ones together. `minItems` rather than a request in the prompt, because an empty array satisfies an array schema trivially and a small model takes the cheapest answer that validates — the lesson LlmPreferenceProposal paid for.
    """


class LlmRemakeNote(BaseModel):
    """
    The schema-constrained reading of a reviewer's note. Handed to Ollama as a format constraint, like LlmClipResponse, so the model cannot answer with prose.

    Three details of this shape are load-bearing, and all three were measured against qwen3.5:4b rather than reasoned about.

    **`topics` comes first and bounds everything after it.** Fields belonging to a topic the model did not declare are discarded by the caller. This exists because the two obvious shapes both fail: with nullable optional fields a 4B writes a summary saying it chose TRACK and then emits null for the mode, and with every field required it fills all of them, inventing a crop and a language for a note about timing. Neither a prompt asking for restraint nor one asking for completeness fixes the other. Declaring scope first is a question the model answers reliably, and it makes the scope a property of the protocol rather than a hope.

    **Every decision is still required, with 'the note did not say' as a value rather than a null,** so that within a declared topic there is no lazy path that satisfies the schema while deciding nothing.

    **`summary` is last.** Constrained decoding emits properties in declaration order, so a model asked to explain itself first explains a decision it has not made yet and then fails to make it.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    topics: list[NoteTopic] = Field(..., max_length=4)
    """
    Which aspects of the clip this note actually raises. Usually one. An empty array is the correct answer for a note that is a remark rather than an instruction, and the remake then proceeds on whatever was set explicitly.
    """
    framing_mode: NoteFraming = Field(..., alias="framingMode")
    crop: NoteCrop
    language: str = Field(..., max_length=16)
    """
    A BCP-47 tag when the note asks for another language, and the literal string NONE when it does not. A required string rather than a nullable one, for the reason given above.
    """
    audio: NoteAudio
    start_delta_sec: float = Field(..., alias="startDeltaSec", ge=-30.0, le=30.0)
    """
    Seconds to move the cut's start. 0 when the note does not say the clip begins at the wrong moment. Negative starts earlier.
    """
    end_delta_sec: float = Field(..., alias="endDeltaSec", ge=-30.0, le=30.0)
    """
    Seconds to move the cut's end. 0 when the note does not mention it. Positive runs longer.
    """
    obscure: NoteObscure
    unsupported: list[UnsupportedAsk] | None = Field(None, max_length=8)
    """
    Anything the note asked for that none of the controls above can express. Usually empty. Listing something here does not stop the remake — the rest of the note is still acted on — it records that one part of the request was understood and cannot be met, which is the difference between a refusal and a silent failure.
    """
    summary: str = Field(..., max_length=600)
    """
    One sentence restating the instruction and naming what was changed. Written last, after the settings it describes.

    It describes this ANSWER, not necessarily the outcome: settings belonging to a topic that was not declared are discarded by the caller afterwards, so a summary can name a change that does not survive. `NoteInterpretation.summary` is rewritten from what was actually applied before it reaches the clip — an early version recorded the model's own wording and told reviewers it had set a crop it had not.
    """


class CropAnchor(StrEnum):
    """
    Where a fixed 9:16 window sits in a landscape source. The render profile's own `crop` setting, promoted to the wire so a reviewer can overrule it for one clip without editing a profile that every other clip shares.
    """

    CENTRE = "centre"
    LEFT = "left"
    RIGHT = "right"


class FramingMode(StrEnum):
    """
    How the 9:16 window is decided. AS_RENDERED keeps the profile's fixed crop, which is what every clip got before this existed. FIT crops nothing at all — the whole landscape frame is scaled into the canvas and the dead space is filled — so a subject that moves can never leave the picture; the cost is a smaller picture. PAN moves a full-height window along the source over time, between points the reviewer set. TRACK does the same thing but works the points out from the footage, by following where the motion is. FIT and TRACK exist because a fixed crop keeps about a third of a broadcast frame's width and holds still, which is the wrong answer for any sport where the thing worth watching moves.
    """

    AS_RENDERED = "AS_RENDERED"
    FIT = "FIT"
    PAN = "PAN"
    TRACK = "TRACK"


class FitFill(StrEnum):
    """
    What fills the canvas above and below the picture in FIT mode. BLUR is a scaled, heavily blurred copy of the frame itself, which reads as deliberate and keeps the eye on the centre band. SOLID is a flat colour, which is cheaper to encode and looks better when the footage has a hard horizon the blur would smear.
    """

    BLUR = "BLUR"
    SOLID = "SOLID"


class PanKeyframe(BaseModel):
    """
    Where the window sits at one instant. The crop centre is interpolated between consecutive keyframes and held flat outside the first and last, so two points are enough to describe a pan and one is enough to describe a fixed off-centre crop.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    at_sec: float = Field(..., alias="atSec", ge=0.0)
    """
    Seconds from the start of the clip, not of the source. A reviewer sets these while watching the clip, and the clip is the only timeline they can see.
    """
    x_pct: float = Field(..., alias="xPct", ge=0.0, le=100.0)
    """
    Centre of the window as a percentage of source width. 50 is the middle. Clamped at render time so the window cannot hang off the edge of the frame, which means a keyframe of 0 or 100 is a legal way of saying 'as far left/right as this can go' rather than an error.
    """


class Framing(BaseModel):
    """
    The reframe half of a remake. Every mode other than AS_RENDERED re-cuts from the original source, because the rendered clip has already had the discarded pixels thrown away — so these need the source media to still be on the worker, and fail clearly when the workspace collector has taken it.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    mode: FramingMode
    crop: CropAnchor | None = None
    """
    AS_RENDERED only: a fixed anchor overriding the profile's. Null keeps the profile's own.
    """
    keyframes: list[PanKeyframe] | None = Field([], validate_default=True)
    """
    PAN only, and required there: an empty list in PAN mode is a request with no instruction in it. Ignored in every other mode — TRACK computes its own and records them in the same shape, so a tracked clip can be remade as a PAN with the tracker's work as the starting point.
    """
    fill: FitFill | None = None
    """
    FIT only. Null means BLUR.
    """
    zoom: float | None = Field(1, ge=1.0, le=2.0)
    """
    How tight the window is. 1 means 'as wide as this mode allows' — the whole frame in FIT, full source height in PAN and TRACK — and larger values close in, trading away the margin that keeps a moving subject in shot. The ceiling of 2 is not arbitrary: past it a 1080-line source no longer has the pixels to fill a 1080-wide canvas, and the clip visibly softens.
    """
    offset_y_pct: float | None = Field(0, alias="offsetYPct", ge=-50.0, le=50.0)
    """
    FIT only: moves the picture band up or down the canvas, as a percentage of canvas height. Negative is up. Useful when captions want the lower third and the blurred fill above is doing nothing.
    """
    smoothing_sec: float | None = Field(2, alias="smoothingSec", ge=0.0, le=10.0)
    """
    TRACK only: the window of footage the tracker averages over before it moves. Low values follow the action closely and jitter; high values glide and lag. Two seconds is the compromise that survives a camera cut without lurching, and a camera that is itself already following play needs very little on top.
    """
    max_pan_pct_per_sec: float | None = Field(
        12, alias="maxPanPctPerSec", ge=0.0, le=100.0
    )
    """
    TRACK only: a ceiling on how fast the window may travel, in percent of source width per second. This is what stops the crop snapping across the pitch when the motion centroid jumps to a different part of the frame — the picture lags the ball for a moment, which looks far better than a whip-pan that arrives before anything happens.
    """


class SpeechMode(StrEnum):
    """
    What a generated voice does to the clip's own audio. REPLACE removes the original entirely, which is the point when the original is commentary you cannot use. BED keeps the original underneath, ducked, which suits footage whose crowd noise and ball contact are half of why the clip works.
    """

    REPLACE = "REPLACE"
    BED = "BED"


class VoiceCaptions(StrEnum):
    """
    Captions are burned into pixels, so a new voice makes the existing ones wrong — they show words nobody is saying any more. REBUILD transcribes the generated narration and re-cuts captions from what was actually said rather than from the script, which is the only version that stays in sync when the synthesiser stresses a sentence differently from the way it was written. KEEP is for a bed under unchanged speech. REMOVE leaves the picture clean.
    """

    REBUILD = "REBUILD"
    KEEP = "KEEP"
    REMOVE = "REMOVE"


class VoiceOptions(BaseModel):
    """
    A new narration for a clip: what to say, in which language, in whose voice. Worth being plain about the limit of this, because it is easy to reach for the wrong reason: re-voicing changes the soundtrack and nothing else. On third-party footage the picture is still the picture, and it is the picture a rights holder's matching runs against. This helps with a claim on commentary or music, and it opens a clip to an audience that does not speak the original language. It does not make third-party footage safe to publish — nothing here decides that, and the operator still does.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    mode: SpeechMode
    voice: str = Field(..., min_length=1)
    """
    The synthesiser's own name for a voice. Opaque here on purpose: this contract should not have to be reissued every time an engine ships a new one.
    """
    language: str = Field(..., max_length=16, min_length=2)
    """
    The language to speak in, as a BCP-47 tag. Not necessarily the language the clip is in — that difference is the whole point of `translate`.
    """
    translate: bool | None = True
    """
    Whether to translate the clip's words before speaking them. Only meaningful when `language` differs from the source's; when it does and this is false, the synthesiser is being asked to read one language with another's phonetics, which produces something no listener wants.
    """
    script: str | None = Field(None, max_length=4000)
    """
    What to say, written by hand. Null means 'say what the clip says', which is the ordinary case. A script overrides both the transcript and any translation of it — the reviewer has stated the words, so nothing else gets to.
    """
    speed: float | None = Field(1, ge=0.5, le=2.0)
    """
    Playback rate of the synthesised speech. It moves independently of the picture: narration that runs long is not allowed to stretch the video, so the stage reports an overrun rather than silently retiming footage the reviewer already approved.
    """
    gain_db: float | None = Field(None, alias="gainDb", ge=-40.0, le=12.0)
    """
    Trim on the narration. Null means the stage's own judgement, which targets the clip's loudness normalisation rather than a fixed level.
    """
    duck_db: float | None = Field(None, alias="duckDb", ge=-40.0, le=0.0)
    """
    BED only: how far the original audio is pushed down under the narration. Null means -12 dB, which keeps crowd noise present without competing with a voice.
    """
    captions: VoiceCaptions | None = None
    """
    What happens to the burned-in captions, which the new voice has otherwise made wrong.
    """


class RemakeOptions(BaseModel):
    """
    A reviewer's corrections to a finished clip. Every field is optional because a remake is usually one complaint, not a rebuild: 'the framing lost the ball' and 'this needs to be in Spanish' are separate errands and should not have to be sent together.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    notes: str | None = Field(None, max_length=2000)
    """
    What is wrong with the clip, in the reviewer's own words. Always recorded on the new clip, whether or not anything is inferred from it — a remake whose result is still wrong is much easier to reason about when what was asked for is written next to what was done.
    """
    interpret_notes: bool | None = Field(True, alias="interpretNotes")
    """
    Whether to put the notes to the local model and let it fill in the options the reviewer left unset. Bounded deliberately: it may choose a framing mode, a crop anchor, a language and a voice, and it may not touch anything the reviewer stated explicitly. What it decided is recorded on the clip, so a note that was misread is visible as a misreading rather than as an unexplained result.
    """
    framing: Framing | None = None
    """
    Null means 'leave the framing alone', which is not the same as AS_RENDERED: an explicit AS_RENDERED with a `crop` set is a reframe to a different fixed anchor.
    """
    voice: VoiceOptions | None = None
    """
    Null keeps the clip's own audio.
    """
    start_delta_sec: float | None = Field(0, alias="startDeltaSec", ge=-30.0, le=30.0)
    """
    Nudge the cut's start, in seconds, relative to where the candidate put it. Negative starts earlier. Boundary snapping gets the sentence right and still lands a beat late for an action clip, where the interesting thing happens before anyone says anything about it.
    """
    end_delta_sec: float | None = Field(0, alias="endDeltaSec", ge=-30.0, le=30.0)
    """
    Nudge the cut's end. Positive runs longer.
    """
    obscure: ObscureOptions | None = None
    """
    Hide a fixed part of the picture — a channel bug, a scoreboard, a burnt-in caption. Null asks for nothing; an object with `auto` false and no regions is also nothing, and is what an untouched control sends.
    """
    profile: str | None = None
    """
    Render with a different named profile — caption size, bitrate, the rest of the look. Null keeps the one the clip was made with, which is what makes a reframe comparable to the version it replaces.
    """


class NoteInterpretation(BaseModel):
    """
    What the local model made of the reviewer's note, recorded whether or not it was any use. A remake that came out wrong is nearly always one of two failures — the note was misread, or it was read correctly and the machinery did the wrong thing — and without this they are indistinguishable.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    understood: bool
    """
    False when the model could not turn the note into anything actionable. The remake still runs on whatever the reviewer set explicitly, rather than failing: a note nobody could parse is not a reason to refuse work that was otherwise fully specified.
    """
    summary: str = Field(..., max_length=600)
    """
    The instruction as the model understood it, in one sentence, and the settings it changed.
    """
    model: str | None = None


class AppliedVoice(BaseModel):
    """
    The narration that was actually produced. Records the spoken text rather than the script, because those differ whenever a translation happened, and the spoken text is the one a caption has to match and a viewer will hear.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    mode: SpeechMode
    voice: str
    language: str
    engine: str
    """
    Which synthesiser, and which version of it. Two voices of the same name from different engines do not sound alike.
    """
    translated: bool | None = False
    spoken_text: str | None = Field(None, alias="spokenText", max_length=4000)
    speech_duration_sec: float | None = Field(None, alias="speechDurationSec", ge=0.0)
    """
    How long the narration runs. Compared against the clip's own duration by the UI: narration that overruns the picture is the most common way a translated clip goes wrong, and it is invisible until someone watches the end.
    """


class Refusal(RootModel[str]):
    root: str = Field(..., max_length=300)


class Warning(RootModel[str]):
    root: str = Field(..., max_length=300)


class AppliedRemake(BaseModel):
    """
    What was asked for, and what was done. Carried on the clip the remake produced, beside `derivedFromClipId`, so the pair reads as a correction and its result rather than as two unrelated clips.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    notes: str | None = Field(None, max_length=2000)
    """
    The reviewer's words, verbatim.
    """
    interpretation: NoteInterpretation | None = None
    framing_mode: FramingMode = Field(..., alias="framingMode")
    keyframes: list[PanKeyframe] | None = Field([], validate_default=True)
    """
    The window's path through the source, as rendered — the reviewer's own points in PAN, and the tracker's findings in TRACK. Kept because it is the only way to see what a tracked remake actually decided to follow, and because a PAN remake can be seeded from it and corrected by hand when it followed the wrong thing.
    """
    voice: AppliedVoice | None = None
    refusals: list[Refusal] | None = Field([], max_length=8, validate_default=True)
    """
    Parts of the request that were understood and NOT carried out, each phrased for the person who asked. A remake that silently does four of the five things asked of it is indistinguishable from one that is broken, and the reviewer's next move — ask again, ask differently, give up — depends entirely on which it was.
    """
    warnings: list[Warning] | None = Field([], max_length=8, validate_default=True)
    """
    Things that were done but are likely to disappoint: a narration built from a transcript the recogniser was unsure of, a translation that barely changed the text, a clip with almost no speech in it. Surfaced next to the result because every one of these has produced a clip that looked finished and was unusable.
    """
    obscured: list[ObscureRegion] | None = Field(
        [], max_length=6, validate_default=True
    )
    """
    The regions actually hidden, with where each came from. This is the record that makes a wrong box fixable: a reviewer who can see that detection put the rectangle two percent too high can drag it and remake, rather than describing the error in prose to a model that will guess again.
    """
    start_sec: float = Field(..., alias="startSec", ge=0.0)
    """
    The window actually cut from the source, after any nudge. Absolute source seconds, matching Candidate.
    """
    end_sec: float = Field(..., alias="endSec", ge=0.0)


class JobEventKind(StrEnum):
    CREATED = "CREATED"
    CLAIMED = "CLAIMED"
    STAGE_STARTED = "STAGE_STARTED"
    STAGE_COMPLETED = "STAGE_COMPLETED"
    STAGE_FAILED = "STAGE_FAILED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    REQUEUED = "REQUEUED"


class JobEvent(BaseModel):
    """
    Append-only transition log at jobs/{jobId}/events/{eventId}. Never mutated, which is what makes it trustworthy when debugging a job that failed hours ago.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    id: str = Field(..., min_length=1)
    job_id: str = Field(..., alias="jobId", min_length=1)
    kind: JobEventKind
    at: AwareDatetime
    seq: int = Field(..., ge=0)
    """
    Order within a single transition. One transition can emit several events at the identical instant — a reap emits LEASE_EXPIRED and REQUEUED together — and ordering by timestamp alone would then fall back to document id, which is random. The log is ordered by (at, seq).
    """
    stage: StageName | None = None
    worker_id: str | None = Field(None, alias="workerId")
    detail: str | None = None
    attempts: int | None = Field(None, ge=0)


class IngestErrorCode(StrEnum):
    """
    Why an ingest failed, in terms a user can act on. Phase 3 requires each of these to map to a distinct user-facing message rather than a stack trace: 'this video is age-restricted' is actionable, 'DownloadError' is not. Only RATE_LIMITED and NETWORK are worth retrying.
    """

    UNSUPPORTED_URL = "UNSUPPORTED_URL"
    NOT_FOUND = "NOT_FOUND"
    PRIVATE = "PRIVATE"
    GEO_BLOCKED = "GEO_BLOCKED"
    AGE_RESTRICTED = "AGE_RESTRICTED"
    LIVE_STREAM = "LIVE_STREAM"
    TOO_LONG = "TOO_LONG"
    NO_SUITABLE_FORMAT = "NO_SUITABLE_FORMAT"
    RATE_LIMITED = "RATE_LIMITED"
    NETWORK = "NETWORK"
    DISK_FULL = "DISK_FULL"
    UNKNOWN = "UNKNOWN"


class SourceProvider(StrEnum):
    """
    local exists so the whole pipeline can be exercised from a committed fixture with no network, which is what keeps the integration tier runnable in CI.
    """

    YOUTUBE = "youtube"
    LOCAL = "local"


class Source(BaseModel):
    """
    One ingested long-form video at sources/{sourceId}. The media itself never leaves the worker (docs/PLAN.md decision D3).
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    id: str = Field(..., min_length=1)
    uid: str = Field(..., min_length=1)
    provider: SourceProvider
    external_id: str | None = Field(None, alias="externalId")
    """
    YouTube video id, or null for a local file.
    """
    url: str | None = None
    title: str | None = None
    channel: str | None = None
    duration_sec: float | None = Field(None, alias="durationSec", ge=0.0)
    content_hash: str | None = Field(None, alias="contentHash")
    """
    Hash of the downloaded media. Transcripts are cached against this, so re-ingesting the same video costs no GPU time.
    """
    local_path: str | None = Field(None, alias="localPath")
    """
    Worker-local absolute path. Advisory only for the PWA, which can never read it.
    """
    size_bytes: int | None = Field(None, alias="sizeBytes", ge=0)
    obscure: ObscureOptions | None = None
    """
    Hidden on every clip cut from this source, including the first. Set by accepting a learned preference, or from the remake form's 'always do this for this channel'.

    This is the difference between a feature and a chore. A channel bug is a property of the channel: found once on one clip, it is in the same place on every clip that channel will ever produce, and a reviewer who has to ask for it each time is doing the system's bookkeeping. Applied at RENDER, so a clip arrives for review already clean rather than arriving wrong and needing a correction.
    """
    pinned: bool | None = False
    """
    Exempt from workspace garbage collection. Sources are large and the disk is finite, so GC is not optional — pinning is the escape hatch for one you are still working with.
    """
    last_accessed_at: AwareDatetime | None = Field(None, alias="lastAccessedAt")
    """
    Drives least-recently-used eviction. Touched whenever a stage reads the file, not when the document is read.
    """
    created_at: AwareDatetime = Field(..., alias="createdAt")


class TranscriptWord(BaseModel):
    """
    Word-level timing. Load-bearing for boundary snapping (D4) and caption generation, which is why faster-whisper is configured with word timestamps enabled.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    text: str
    start_sec: float = Field(..., alias="startSec", ge=0.0)
    end_sec: float = Field(..., alias="endSec", ge=0.0)
    probability: float | None = Field(None, ge=0.0, le=1.0)


class TranscriptSegment(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    index: int = Field(..., ge=0)
    text: str
    start_sec: float = Field(..., alias="startSec", ge=0.0)
    end_sec: float = Field(..., alias="endSec", ge=0.0)
    words: list[TranscriptWord] | None = Field([], validate_default=True)


class Transcript(BaseModel):
    """
    Stored at sources/{sourceId}/transcripts/{modelVersion}, keyed by model so re-transcribing with a better model does not destroy the old one.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    source_id: str = Field(..., alias="sourceId", min_length=1)
    model_version: str = Field(..., alias="modelVersion", min_length=1)
    """
    For example faster-whisper:large-v3-turbo:int8_float16
    """
    language: str | None = None
    duration_sec: float | None = Field(None, alias="durationSec", ge=0.0)
    segments: list[TranscriptSegment]
    created_at: AwareDatetime = Field(..., alias="createdAt")


class SubScores(BaseModel):
    """
    The rubric from docs/PLAN.md. The LLM returns these; Python computes the weighted total (decision D5), so the weighting can be changed and every historical candidate rescored without re-running inference.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    hook: int = Field(..., ge=0, le=25)
    curiosity: int = Field(..., ge=0, le=20)
    standalone: int = Field(..., ge=0, le=20)
    emotion: int = Field(..., ge=0, le=15)
    pacing: int = Field(..., ge=0, le=10)
    shareability: int = Field(..., ge=0, le=10)


class Candidate(BaseModel):
    """
    An LLM-proposed clip window at candidates/{candidateId}, after deterministic boundary snapping.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    id: str = Field(..., min_length=1)
    uid: str = Field(..., min_length=1)
    source_id: str = Field(..., alias="sourceId", min_length=1)
    job_id: str | None = Field(None, alias="jobId")
    start_sec: float = Field(..., alias="startSec", ge=0.0)
    """
    Snapped to a silence boundary and a sentence end, not the raw LLM value.
    """
    end_sec: float = Field(..., alias="endSec", ge=0.0)
    proposed_start_sec: float | None = Field(None, alias="proposedStartSec", ge=0.0)
    """
    The model's raw suggestion, retained so snapping can be evaluated against it.
    """
    proposed_end_sec: float | None = Field(None, alias="proposedEndSec", ge=0.0)
    sub_scores: SubScores = Field(..., alias="subScores")
    total: int = Field(..., ge=0, le=100)
    """
    Computed in Python from subScores. Never supplied by the model.
    """
    hook: str | None = None
    reason: str | None = None
    transcript_excerpt: str | None = Field(None, alias="transcriptExcerpt")
    model_version: str | None = Field(None, alias="modelVersion")
    prompt_version: str | None = Field(None, alias="promptVersion")
    created_at: AwareDatetime = Field(..., alias="createdAt")


class ReviewState(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ChannelConnection(StrEnum):
    """
    Whether the worker can currently publish to this channel. NOT_CONFIGURED means no OAuth client has been supplied; NEEDS_AUTH means one has but nobody has authorised it, or the refresh token expired — which it does every 7 days while the consent screen is in Testing mode.
    """

    NOT_CONFIGURED = "NOT_CONFIGURED"
    NEEDS_AUTH = "NEEDS_AUTH"
    CONNECTED = "CONNECTED"
    ERROR = "ERROR"


class UserRole(StrEnum):
    """
    ADMIN can approve other users and change roles. MEMBER can use the app for their own data and nothing else. There is no third level because there is no third thing to protect.
    """

    ADMIN = "ADMIN"
    MEMBER = "MEMBER"


class UserStatus(StrEnum):
    """
    Registration is open; access is not. A new account lands in PENDING and can read nothing until an admin approves it, so an unapproved sign-up is an inert row rather than a foothold. REJECTED and DISABLED are kept apart deliberately: one was never let in, the other was and had it taken away, and an audit that cannot tell them apart is not much of an audit.
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DISABLED = "DISABLED"


class UserProfile(BaseModel):
    """
    A person with an account, at users/{uid}. The document id IS the Firebase Auth uid, which is what lets security rules resolve a caller's status with one get() and no join.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    uid: str = Field(..., min_length=1)
    email: str = Field(..., min_length=1)
    display_name: str | None = Field(None, alias="displayName")
    photo_url: str | None = Field(None, alias="photoUrl")
    role: UserRole
    status: UserStatus
    created_at: AwareDatetime = Field(..., alias="createdAt")
    decided_at: AwareDatetime | None = Field(None, alias="decidedAt")
    """
    When an admin last approved, rejected or disabled this account.
    """
    decided_by: str | None = Field(None, alias="decidedBy")
    """
    The uid of the admin who made that decision. Recorded so 'who let this person in?' is answerable later.
    """


class ClipLocation(StrEnum):
    """
    Where the authoritative copy of a rendered clip lives. LOCAL is the free-tier default: Cloud Storage for Firebase requires the Blaze plan, so on Spark there is no bucket at all and clips stay on the worker. REMOTE means a copy exists that any browser can fetch. See docs/adr/0009-spark-tier-local-artefacts.md.
    """

    LOCAL = "LOCAL"
    REMOTE = "REMOTE"


class ClipPreview(BaseModel):
    """
    What a phone can actually see when the clip file is not reachable. Stored at clips/{clipId}/preview/poster as base64 — a subcollection document, so the review-queue query does not drag image bytes on every read. Sized to stay well inside Firestore's 1 MiB document limit; at ~40-60 KB the 1 GiB free tier holds roughly 20,000 of these.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    clip_id: str = Field(..., alias="clipId", min_length=1)
    poster_base64: str = Field(..., alias="posterBase64", min_length=1)
    """
    A single representative frame, JPEG, base64-encoded without a data: prefix.
    """
    filmstrip_base64: str | None = Field(None, alias="filmstripBase64")
    """
    Four frames tiled into one JPEG, so a reviewer gets a sense of motion rather than a single still.
    """
    width_px: int = Field(..., alias="widthPx", ge=1)
    height_px: int = Field(..., alias="heightPx", ge=1)
    byte_size: int | None = Field(None, alias="byteSize", ge=0)
    created_at: AwareDatetime = Field(..., alias="createdAt")


class TranscriptRef(BaseModel):
    """
    What Firestore keeps about a transcript, at sources/{sourceId}/transcripts/{modelVersion}. The word-level segments themselves live on the worker: a 60-minute word-level transcript approaches Firestore's 1 MiB document limit, and the PWA never needs one whole — Candidate.transcriptExcerpt carries the part a reviewer reads.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    source_id: str = Field(..., alias="sourceId", min_length=1)
    model_version: str = Field(..., alias="modelVersion", min_length=1)
    local_path: str = Field(..., alias="localPath", min_length=1)
    """
    Worker-local path to the full Transcript document, as JSON.
    """
    vad_path: str | None = Field(None, alias="vadPath")
    """
    Worker-local path to the voice-activity map produced alongside the transcript: the speech spans, whose gaps are the silences. Phase 5 snaps clip boundaries to those gaps, so it is worth persisting rather than recomputing. Its contents are deliberately NOT modelled here — it never crosses to the PWA, and this document is the wire protocol.
    """
    language: str | None = None
    duration_sec: float | None = Field(None, alias="durationSec", ge=0.0)
    segment_count: int | None = Field(None, alias="segmentCount", ge=0)
    word_count: int | None = Field(None, alias="wordCount", ge=0)
    created_at: AwareDatetime = Field(..., alias="createdAt")


class PublicationState(StrEnum):
    """
    Lifecycle of one attempt to publish one clip to one platform. PENDING is written BEFORE the upload begins, which is what makes a retry reconcile against the platform instead of double-posting.
    """

    PENDING = "PENDING"
    UPLOADING = "UPLOADING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PublishPlatform(StrEnum):
    """
    Where a clip was published. TikTok and Instagram are out of scope until v0.3+ — both need app review with materially harder approval paths.
    """

    YOUTUBE = "YOUTUBE"


class PublishPrivacy(StrEnum):
    """
    Defaults to unlisted. Publishing something to the world by accident is not recoverable in the way an unlisted upload is.
    """

    PRIVATE = "private"
    UNLISTED = "unlisted"
    PUBLIC = "public"


class Publication(BaseModel):
    """
    One publish attempt, at clips/{clipId}/publications/{pubId}. Doubles as the audit log: it records what title, description and privacy actually went out, to which channel and when, so 'what did we post' is answerable for any published clip without reading worker logs.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    id: str = Field(..., min_length=1)
    clip_id: str = Field(..., alias="clipId", min_length=1)
    uid: str = Field(..., min_length=1)
    platform: PublishPlatform
    state: PublicationState
    external_id: str | None = Field(None, alias="externalId")
    """
    The platform's id for the uploaded video. Null until the upload completes.
    """
    external_url: str | None = Field(None, alias="externalUrl")
    channel_id: str | None = Field(None, alias="channelId")
    """
    Which channel this went to. Recorded on the attempt because a clip published today and re-published elsewhere next month must not look like it went to the same place.
    """
    privacy: PublishPrivacy | None = None
    title: str | None = None
    description: str | None = None
    tags: list[str] | None = []
    category_id: str | None = Field(None, alias="categoryId")
    """
    Recorded because it is part of what went out. The resolved value, not the request's — this record is the answer to 'what did we actually send', and a field that only sometimes reflects the upload answers nothing.
    """
    attempts: int | None = Field(0, ge=0)
    quota_units: int | None = Field(None, alias="quotaUnits", ge=0)
    """
    What this attempt cost. A YouTube upload is 1,600 of a 10,000 daily allowance — about six a day — so the budget is tracked rather than discovered on the seventh failure.
    """
    error: StageError | None = None
    publish_at: AwareDatetime | None = Field(None, alias="publishAt")
    """
    Scheduled publish time. The worker holds the clip until then.
    """
    created_at: AwareDatetime = Field(..., alias="createdAt")
    published_at: AwareDatetime | None = Field(None, alias="publishedAt")


class RetentionPoint(BaseModel):
    """
    One point on the audience-retention curve. `elapsedRatio` is the position through the video, 0 to 1. `audienceWatchRatio` is the fraction of viewers still watching there, and is NOT capped at 1: a segment people scrub back to reports above 1, which is a real signal and must not be clamped away.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    elapsed_ratio: float = Field(..., alias="elapsedRatio", ge=0.0, le=1.0)
    audience_watch_ratio: float = Field(..., alias="audienceWatchRatio", ge=0.0)


class MetricSnapshot(BaseModel):
    """
    One day of realised performance for one publication, at metrics/{publicationId}_{date}.

    A top-level collection rather than a subcollection of the publication, because every question Phase 9 asks is asked ACROSS clips — the correlation between predicted score and retention is not a per-clip query — and a collection group index to answer them would buy nothing that the denormalised clipId and publicationId here do not.

    The join key is `externalId`, and deliberately nothing more. A publication the operator created by hand carries one just as an API upload does, so Phase 15's manual deliveries join here without a second path (see the synthesis-track notes in docs/PLAN.md).
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    id: str = Field(..., min_length=1)
    uid: str = Field(..., min_length=1)
    clip_id: str = Field(..., alias="clipId", min_length=1)
    publication_id: str = Field(..., alias="publicationId", min_length=1)
    platform: PublishPlatform
    external_id: str = Field(..., alias="externalId", min_length=1)
    """
    The platform's video id. How the publication got it is not recorded here and must not become load-bearing.
    """
    channel_id: str | None = Field(None, alias="channelId")
    date: date_aliased
    """
    The metrics day in the channel's reporting timezone, YYYY-MM-DD. Not the fetch day: a poller that ran twice or missed a day must produce the same document either way, which is what makes the gap check in exit criterion 1 meaningful.
    """
    days_since_publish: int | None = Field(0, alias="daysSincePublish", ge=0)
    views: int | None = Field(0, ge=0)
    likes: int | None = Field(0, ge=0)
    comments: int | None = Field(0, ge=0)
    shares: int | None = Field(0, ge=0)
    subscribers_gained: int | None = Field(0, alias="subscribersGained", ge=0)
    estimated_minutes_watched: float | None = Field(
        0, alias="estimatedMinutesWatched", ge=0.0
    )
    average_view_duration_sec: float | None = Field(
        None, alias="averageViewDurationSec", ge=0.0
    )
    average_view_percentage: float | None = Field(
        None, alias="averageViewPercentage", ge=0.0
    )
    """
    Percentage of the video watched on average, 0-100. Null when the platform withheld it, which it does below its privacy threshold.
    """
    retention: list[RetentionPoint] | None = Field([], validate_default=True)
    """
    Empty is a normal state, not a failure: YouTube withholds the curve until a video clears a privacy threshold of a few hundred views. A clip with no curve is excluded from curve-based aggregation rather than counted as a flat zero.
    """
    partial: bool | None = False
    """
    True when this day is inside the platform's revision window — YouTube restates the last two to three days. A partial snapshot is overwritten on the next poll; a settled one is never rewritten, so the calibration reads a stable history.
    """
    fetched_at: AwareDatetime = Field(..., alias="fetchedAt")


class ScoreWeights(BaseModel):
    """
    How much each rubric dimension contributes to the total. Decision D5 keeps the total in Python precisely so this can change and every historical candidate be re-ranked with no inference. The defaults reproduce the plain rubric sum exactly.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    hook: float = Field(..., ge=0.0, le=1.0)
    curiosity: float = Field(..., ge=0.0, le=1.0)
    standalone: float = Field(..., ge=0.0, le=1.0)
    emotion: float = Field(..., ge=0.0, le=1.0)
    pacing: float = Field(..., ge=0.0, le=1.0)
    shareability: float = Field(..., ge=0.0, le=1.0)


class CohortKind(StrEnum):
    """
    The dimensions performance is broken down by. Each is knowable from documents ClipForge already writes — nothing here needs a new field on a clip.
    """

    HOOK_TYPE = "HOOK_TYPE"
    DURATION_BUCKET = "DURATION_BUCKET"
    SCORE_BAND = "SCORE_BAND"
    TOPIC = "TOPIC"
    POSTING_HOUR = "POSTING_HOUR"
    CAPTION_STYLE = "CAPTION_STYLE"
    RENDER_PROFILE = "RENDER_PROFILE"


class CohortStat(BaseModel):
    """
    One bucket of one breakdown. `n` is first and is the field to read first: with tens of clips most buckets are too small to mean anything, and a mean over two videos is a number, not a finding.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    kind: CohortKind
    bucket: str = Field(..., min_length=1)
    n: int = Field(..., ge=0)
    mean_predicted_score: float | None = Field(None, alias="meanPredictedScore")
    mean_views: float | None = Field(None, alias="meanViews")
    mean_view_percentage: float | None = Field(None, alias="meanViewPercentage")
    mean_retention_at_half: float | None = Field(None, alias="meanRetentionAtHalf")
    """
    Audience still watching at the midpoint. Chosen as the headline retention number because it is comparable across clips of different lengths, which raw seconds-watched is not.
    """


class CalibrationMethod(StrEnum):
    """
    Spearman is the default: it is rank-based, so it survives the outlier that one clip going mildly viral produces in a sample of tens, where Pearson would report that outlier as the whole finding.
    """

    SPEARMAN = "SPEARMAN"
    PEARSON = "PEARSON"


class CalibrationCorrelation(BaseModel):
    """
    The correlation between the predicted score and one realised outcome. A coefficient near zero is a result, and reporting it as one is the point of the phase.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    outcome: str = Field(..., min_length=1)
    method: CalibrationMethod
    n: int = Field(..., ge=0)
    coefficient: float = Field(..., ge=-1.0, le=1.0)
    ci_low: float | None = Field(None, alias="ciLow")
    ci_high: float | None = Field(None, alias="ciHigh")
    interpretation: str | None = None
    """
    Plain words, generated from n and the interval — not from the coefficient alone. At n below the threshold this says the sample cannot support a conclusion, whatever the coefficient happens to be.
    """


class CalibrationReport(BaseModel):
    """
    What the scores turned out to predict, at calibrations/{reportId}.

    Written, never applied. `fittedWeights` is a proposal the operator adopts by putting it in configuration, in the same spirit as a Preference: a report that silently re-weighted the rubric would destroy the attribution that `modelVersion` and `promptVersion` exist to preserve.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    id: str = Field(..., min_length=1)
    uid: str = Field(..., min_length=1)
    generated_at: AwareDatetime = Field(..., alias="generatedAt")
    n: int = Field(..., ge=0)
    """
    Publications with enough settled metrics to be included. The denominator for every claim in the report.
    """
    window_days: int | None = Field(28, alias="windowDays", ge=1)
    correlations: list[CalibrationCorrelation]
    cohorts: list[CohortStat]
    baseline_weights: ScoreWeights = Field(..., alias="baselineWeights")
    fitted_weights: ScoreWeights | None = Field(None, alias="fittedWeights")
    """
    Null when the sample is too small to fit responsibly, which at the volumes this project produces is the expected answer for a long time.
    """
    underpowered: bool | None = True
    """
    True when n is below the threshold for any conclusion. Defaults to true so a report that fails to set it errs towards claiming nothing.
    """
    notes: list[str] | None = []


class WorkerStatus(StrEnum):
    ONLINE = "ONLINE"
    BUSY = "BUSY"
    OFFLINE = "OFFLINE"


class WorkerCapabilities(BaseModel):
    """
    Advertised so the PWA can explain why a job is not progressing, rather than leaving it silently queued.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    whisper: bool
    llm: bool
    render: bool
    publish: bool


class GpuInfo(BaseModel):
    """
    vramFreeMb is measured through NVML rather than tracked internally, because a foreign process (an interactive Ollama session, say) can hold VRAM the pipeline needs. Observed in Phase 0.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    name: str
    vram_total_mb: int = Field(..., alias="vramTotalMb", ge=0)
    vram_free_mb: int = Field(..., alias="vramFreeMb", ge=0)


class WorkerHeartbeat(BaseModel):
    """
    Liveness and capability advertisement at workers/{workerId}.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    worker_id: str = Field(..., alias="workerId", min_length=1)
    uid: str = Field(..., min_length=1)
    status: WorkerStatus
    capabilities: WorkerCapabilities
    gpu: GpuInfo | None = None
    version: str
    hostname: str | None = None
    active_job_ids: list[str] | None = Field([], alias="activeJobIds")
    last_seen_at: AwareDatetime = Field(..., alias="lastSeenAt")
    started_at: AwareDatetime | None = Field(None, alias="startedAt")


class AgentDesired(StrEnum):
    """
    What the people want the worker on a machine to be doing. The only thing about an agent the PWA is allowed to say, and it is a wish rather than a fact: the agent decides when it has come true.
    """

    RUNNING = "RUNNING"
    STOPPED = "STOPPED"


class AgentState(StrEnum):
    """
    What the agent has actually managed to do about the wish. FOREIGN means a worker is running on that machine which the agent did not start — from a terminal or the desktop app — which it reports rather than duplicates. FAILED means the worker will not stay up and the agent has stopped retrying; it needs a person, not another restart.
    """

    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    FOREIGN = "FOREIGN"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


class AgentReport(BaseModel):
    """
    One machine's supervisor, at agents/{agentId}. The document exists so that starting the worker does not require being at the machine: the PWA writes `desired` from anywhere, including a phone, and the agent — the only party that can spawn a process — writes everything else. Its `lastSeenAt` answers a question no worker heartbeat can, because a worker that is not running cannot say so: an agent beating with state STOPPED means the PC is on and waiting, while an agent that has gone quiet means the PC is off.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    agent_id: str = Field(..., alias="agentId", min_length=1)
    hostname: str | None = None
    version: str
    desired: AgentDesired
    requested_by: str | None = Field(None, alias="requestedBy")
    """
    The uid that last asked for a change, so a worker that started on its own is distinguishable from one somebody started.
    """
    requested_at: AwareDatetime | None = Field(None, alias="requestedAt")
    state: AgentState
    detail: str | None = None
    """
    One sentence a person can act on, written by the agent. A failure that only exists as an exit code is a failure nobody can diagnose from a phone.
    """
    worker_pid: int | None = Field(None, alias="workerPid")
    worker_started_at: AwareDatetime | None = Field(None, alias="workerStartedAt")
    last_exit_code: int | None = Field(None, alias="lastExitCode")
    restarts: int | None = Field(0, ge=0)
    """
    How many times the agent has restarted a worker that died while it was wanted. Reset when a start is asked for, so it counts one bad run rather than the machine's whole history.
    """
    log: list[str] | None = []
    """
    The worker's last few output lines, so a start that fails says why on the phone that asked for it. Bounded: a Firestore document is capped at 1 MiB and this one is written every heartbeat.
    """
    use_emulators: bool | None = Field(False, alias="useEmulators")
    project_id: str | None = Field(None, alias="projectId")
    started_at: AwareDatetime | None = Field(None, alias="startedAt")
    last_seen_at: AwareDatetime = Field(..., alias="lastSeenAt")


class LlmClipProposal(BaseModel):
    """
    One window as the model returns it. Deliberately carries no total and no precise boundaries: the model supplies judgement, Python supplies arithmetic (D5) and boundary precision (D4).
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    start_sec: float = Field(..., alias="startSec", ge=0.0)
    end_sec: float = Field(..., alias="endSec", ge=0.0)
    sub_scores: SubScores = Field(..., alias="subScores")
    hook: str
    reason: str


class LlmClipResponse(BaseModel):
    """
    The schema-constrained response from the local model for one transcript window. Passed to Ollama as a format constraint so malformed output is rejected by the runtime rather than parsed defensively here.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    clips: list[LlmClipProposal]


class AppliedMusic(BaseModel):
    """
    What was actually done to a scored clip, recorded on the clip itself. Provenance rather than configuration: it answers 'what is this version, and where did the track come from' months later, when the job that made it is long gone.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    mode: MusicMode
    captions: MusicCaptions
    source: str
    """
    The URL or path the track came from, verbatim.
    """
    track_title: str | None = Field(None, alias="trackTitle")
    """
    What the source called itself, when it said. Worth keeping for the description and for answering a claim.
    """
    tempo_bpm: float | None = Field(None, alias="tempoBpm")
    """
    The tempo the analysis settled on. Null when the track had no beat clear enough to measure, in which case alignment was skipped rather than guessed.
    """
    music_start_sec: float | None = Field(None, alias="musicStartSec")
    """
    Where in the track the excerpt begins. Rarely 0: a track's first bars are usually its least interesting, so the stage picks a section by energy and starts it on a downbeat.
    """


class RemakeDefaults(BaseModel):
    """
    The machine-readable half of a preference: settings to pre-fill on a future remake. Every field is optional, and a preference may have none at all — plenty of what a reviewer teaches is a judgement the controls cannot hold ("this channel's wide shots are unusable cropped") and is worth carrying as a sentence into the prompt even when it fills in no box.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    framing_mode: FramingMode | None = Field(None, alias="framingMode")
    crop: CropAnchor | None = None
    language: str | None = Field(None, max_length=16)
    speech_mode: SpeechMode | None = Field(None, alias="speechMode")
    obscure: ObscureOptions | None = None
    """
    Regions to hide on future clips. The one kind of preference whose value is mostly in its coordinates rather than in its sentence: a channel's bug does not move, so the box found once is the box forever, and accepting it is what turns 'blur the canal+' from an instruction into a property of the channel.
    """


class Preference(BaseModel):
    """
    Something the system noticed it should remember, at preferences/{preferenceId}.

    The point is to stop asking. A reviewer who writes "follow the ball" on every football clip is teaching the same thing every time, and a system that cannot hold it makes them type it forever. So after a remake that applied feedback, the local model is asked what — if anything — generalises, and the answer is stored here as a proposal.

    **Proposed, never applied.** A preference does nothing until a human accepts it. A wrong one is more expensive than a missed one, because it silently shapes every later clip and the reviewer has no reason to suspect it; a missed one costs one more sentence in a note. The model is also shown what has already been accepted AND what has been rejected, so it neither repeats itself nor re-proposes something that was already turned down.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    id: str = Field(..., min_length=1)
    uid: str = Field(..., min_length=1)
    scope: PreferenceScope
    source_id: str | None = Field(None, alias="sourceId")
    """
    Which source this applies to when the scope is SOURCE. Null for an EVERYTHING preference, which is stored unattached so it is retrieved for every clip.
    """
    category: NoteTopic
    lesson: str = Field(..., max_length=400, min_length=1)
    """
    The standing instruction, written for a remake of a clip nobody has seen yet. It must make sense without the clip that prompted it: "this channel's wide shots lose the ball unless the window follows it", not "the framing was wrong on that one".
    """
    defaults: RemakeDefaults | None = None
    status: PreferenceStatus
    from_clip_id: str | None = Field(None, alias="fromClipId")
    from_note: str | None = Field(None, alias="fromNote", max_length=2000)
    """
    The feedback that taught it, verbatim. Kept so a preference can be judged against what was actually said rather than against the model's paraphrase of it.
    """
    times_applied: int | None = Field(0, alias="timesApplied", ge=0)
    """
    How many remakes this preference has shaped since it was accepted. The number that says whether it is earning its place: one that never fires is noise, and one that fires constantly is a default the pipeline should probably adopt outright.
    """
    created_at: AwareDatetime = Field(..., alias="createdAt")
    decided_at: AwareDatetime | None = Field(None, alias="decidedAt")
    decided_by: str | None = Field(None, alias="decidedBy")


class Preference1(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    lesson: str = Field(..., max_length=400)
    category: NoteTopic
    scope: PreferenceScope
    framing_mode: NoteFraming | None = Field(None, alias="framingMode")
    language: str | None = Field(None, max_length=16)


class LlmPreferenceProposal(BaseModel):
    """
    The schema-constrained answer to "what, if anything, should be remembered from this correction?". Handed to Ollama as a format constraint like the other LLM shapes here.

    `recurring` and `reasoning` come first, and that ordering is the whole design. Constrained decoding emits properties in declaration order, and an empty array satisfies an array schema trivially — so a model asked only for a list returns `[]` every single time, which it did on all four measured cases including the one that plainly taught two things. Being made to answer a yes/no question and justify it *before* the list is reached turns the same model into one that answers. It is the same lesson `LlmRemakeNote` records: where a schema offers a lazy path, a small model takes it.

    An empty list is still a correct and common answer — most corrections are about one clip's own moment — and a wrong standing rule costs far more than a missed one.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    recurring: bool
    """
    Would a DIFFERENT clip from this same source be made better if the system already knew something from this correction? Answered first, before any list exists to be left empty.
    """
    reasoning: str = Field(..., max_length=300)
    """
    One sentence on why. Not stored — it exists to make the model state a position it then has to be consistent with, which is worth more than the tokens it costs.
    """
    preferences: list[Preference1] = Field(..., max_length=3)


class PublishDefaults(BaseModel):
    """
    What a publish uses when the clip does not say otherwise. Editable from the app because none of it is secret — unlike the credentials, which never leave the worker.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    privacy: PublishPrivacy
    category_id: str | None = Field("22", alias="categoryId")
    """
    YouTube category id. 22 is People & Blogs, which is the safe default for talking-head clips.
    """
    tags: list[str] | None = []
    title_suffix: str | None = Field(None, alias="titleSuffix")
    """
    Appended to every title, for a channel handle or series marker. Truncation still applies: YouTube rejects titles over 100 characters.
    """
    description_template: str | None = Field(None, alias="descriptionTemplate")
    """
    Appended to every description. Where a standing credit, licence note or link block belongs.
    """


class Channel(BaseModel):
    """
    A publishing destination, at channels/{channelId}. Modelled as a collection from the outset even though the UI manages one: adding a second channel is then a document and a second `youtube-auth`, not a schema migration and a rewrite of every publication record. Deliberately holds NO credentials — the client secret and refresh token live on the worker (docs/adr/0010-worker-held-publishing-credentials.md).
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    id: str = Field(..., min_length=1)
    uid: str = Field(..., min_length=1)
    platform: PublishPlatform
    label: str = Field(..., min_length=1)
    """
    What the operator calls it. Free text, because 'the cooking one' is more useful at 6am than a channel id.
    """
    is_default: bool | None = Field(False, alias="isDefault")
    external_channel_id: str | None = Field(None, alias="externalChannelId")
    external_channel_title: str | None = Field(None, alias="externalChannelTitle")
    """
    Read back from YouTube after authorising, so the app can show which account was actually connected rather than which one was intended.
    """
    connection: ChannelConnection
    connection_message: str | None = Field(None, alias="connectionMessage")
    """
    Why the connection is not CONNECTED, in words the operator can act on.
    """
    authorised_at: AwareDatetime | None = Field(None, alias="authorisedAt")
    checked_at: AwareDatetime | None = Field(None, alias="checkedAt")
    defaults: PublishDefaults
    quota_day: str | None = Field(None, alias="quotaDay")
    quota_used_units: int | None = Field(None, alias="quotaUsedUnits", ge=0)
    uploads_remaining_today: int | None = Field(
        None, alias="uploadsRemainingToday", ge=0
    )
    created_at: AwareDatetime = Field(..., alias="createdAt")
    updated_at: AwareDatetime | None = Field(None, alias="updatedAt")


class PublishOptions(BaseModel):
    """
    What the operator chose for one specific upload, set when the publish is requested. Absent fields fall back to the channel's defaults, so a clip published without opening any of this still behaves sensibly.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    channel_id: str | None = Field(None, alias="channelId")
    title: str | None = Field(None, max_length=100)
    """
    Replaces the clip's title for this upload only. Capped at YouTube's own limit here rather than truncated silently on the way out, so what the operator typed is what the audit record shows.
    """
    description: str | None = Field(None, max_length=4900)
    """
    Replaces the clip's description for this upload only.
    """
    privacy: PublishPrivacy | None = None
    category_id: str | None = Field(None, alias="categoryId")
    tags: list[str] | None = None
    """
    Null and empty are different answers here, and both are ones an operator can mean. Null is 'I did not touch the tags', which falls through to the channel's; an empty list is 'no tags on this one', which does not. Every other field in this block is nullable for the same reason, and an array whose absent value was [] could not express the second.
    """


class Clip(BaseModel):
    """
    A rendered artefact at clips/{clipId}. The FILE itself never enters Firestore. On the free tier it stays on the worker and is reachable through the worker's local file server; when Blaze is available the same document also carries a playbackUrl. Both states are first-class here on purpose, so enabling Blaze populates a field rather than migrating a model.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    id: str = Field(..., min_length=1)
    uid: str = Field(..., min_length=1)
    candidate_id: str = Field(..., alias="candidateId", min_length=1)
    source_id: str | None = Field(None, alias="sourceId")
    job_id: str | None = Field(None, alias="jobId")
    location: ClipLocation
    local_path: str = Field(..., alias="localPath", min_length=1)
    """
    Absolute path on the worker. Always set, even once a remote copy exists — the worker still needs it to publish (decision D7).
    """
    playback_url: str | None = Field(None, alias="playbackUrl")
    """
    A URL any browser can fetch. Null on the free tier. Populated by the Cloud Storage adapter, and equally by a tunnel — this field is 'a URL', not 'a Firebase thing'.
    """
    storage_path: str | None = Field(None, alias="storagePath")
    """
    Object path within the bucket, when one exists. Kept alongside playbackUrl because deletion and rules key off the path, not the URL.
    """
    playback_expires_at: AwareDatetime | None = Field(None, alias="playbackExpiresAt")
    """
    When the bucket copy stops being playable. Written at upload as upload time plus the retention window, and honoured by the UI without asking the bucket: the object is removed by a Cloud Storage lifecycle rule, which reports to nobody, so a clip whose expiry has passed is treated as local-only rather than discovered to be missing when someone presses play. Null when there is no bucket copy.
    """
    thumbnail_path: str | None = Field(None, alias="thumbnailPath")
    duration_sec: float | None = Field(None, alias="durationSec", ge=0.0)
    width_px: int | None = Field(None, alias="widthPx", ge=1)
    height_px: int | None = Field(None, alias="heightPx", ge=1)
    size_bytes: int | None = Field(None, alias="sizeBytes", ge=0)
    render_profile: str | None = Field(None, alias="renderProfile")
    title: str | None = Field(None, max_length=200)
    """
    What this clip is called, written to be read. Until LlmClipMetadata existed this was `Candidate.hook` — a line quoted out of the transcript — so clips went out titled with lowercase French ASR fragments.
    """
    description: str | None = Field(None, max_length=2000)
    """
    The text that goes out with the clip. Previously `Candidate.reason`, which is one sentence on why the window was SELECTED — an analyst's note to a pipeline, shown to viewers.
    """
    tags: list[Tag] | None = Field([], max_length=15, validate_default=True)
    """
    Search terms for this clip, written with its title and description. Carried here rather than only on the channel because they are about THIS clip — a channel-wide list is the same on a goal and on a press conference.
    """
    review: ReviewState
    reviewed_at: AwareDatetime | None = Field(None, alias="reviewedAt")
    lineage_id: str | None = Field(None, alias="lineageId", min_length=1)
    """
    The id of the clip this one descends from, at the root of the chain — the clip RENDER originally made. Every version shares it, so a clip and every correction of it are one row in the review queue instead of five.

    That mattered immediately. The queue lists what is PENDING, a remake is always PENDING, and its parent usually still is too, so a single football clip corrected three times filled four slots and the reviewer had to work out which was newest. Null on a clip written before this field existed; readers treat that as the clip being its own root.
    """
    version: int | None = Field(1, ge=1)
    """
    Which attempt this is within its lineage. 1 is the clip RENDER made; a remake is its parent's version plus one. Ordering by this rather than by `createdAt` is deliberate: two remakes of the same parent are siblings, not a sequence, and the number says so.
    """
    derived_from_clip_id: str | None = Field(None, alias="derivedFromClipId")
    """
    The clip this one was made from, when it is a scored version of another. The original is never altered — a MUSIC job produces a new clip — so this is what relates the two, and what lets the review queue say 'music version of' rather than showing two unexplained near-duplicates.
    """
    music: AppliedMusic | None = None
    """
    What was added to this clip, when something was. Null on an ordinary render.
    """
    remake: AppliedRemake | None = None
    """
    The correction that produced this clip, when it is one. Null on an ordinary render.
    """
    review_note: str | None = Field(None, alias="reviewNote", max_length=2000)
    """
    What the reviewer thought, in their own words. Distinct from `description`, which is copy that may be published: this is never uploaded anywhere and exists to answer 'why did I reject this?' three weeks later. Phase 9 calibrates the rubric against realised performance; a human's stated reason is the other half of that evidence and is worth capturing while it is fresh.
    """
    created_at: AwareDatetime = Field(..., alias="createdAt")


class Job(BaseModel):
    """
    One pipeline run over one source. Stored at jobs/{jobId}.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    id: str = Field(..., min_length=1)
    uid: str = Field(..., min_length=1)
    """
    Owning user. Every security rule keys off this field.
    """
    type: JobType
    status: JobStatus
    source_id: str | None = Field(None, alias="sourceId")
    """
    Set by the DOWNLOAD stage once ingestion resolves the submission to a source. Null until then.
    """
    submission: str | None = None
    """
    What the user actually submitted: a YouTube URL or a local file path. Kept verbatim and separate from sourceId, because a job must be re-runnable from the original input even if its source document was garbage-collected.
    """
    clip_id: str | None = Field(None, alias="clipId")
    """
    The clip a PUBLISH or MUSIC job acts on. Null for every other job type. Security rules read this to check the clip has been approved before allowing the job to be created at all.
    """
    music_options: MusicOptions | None = Field(None, alias="musicOptions")
    """
    What a MUSIC job should add, and how. Null for every other job type.
    """
    remake_options: RemakeOptions | None = Field(None, alias="remakeOptions")
    """
    What a REMAKE job should correct. Null for every other job type.
    """
    publish_options: PublishOptions | None = Field(None, alias="publishOptions")
    """
    Set by the client on a PUBLISH job. Null for every other job type, and null here means 'use the channel defaults'.
    """
    not_before: AwareDatetime | None = Field(None, alias="notBefore")
    """
    The job is not claimable until this instant. Null means claimable immediately. This is how a publish-at time is honoured: scheduling lives in the one predicate every claim path already consults, rather than in a second scheduler that could disagree with the first.
    """
    stages: list[Stage] = Field(..., min_length=1)
    """
    Ordered. Executed front to back; DONE stages are skipped on retry.
    """
    worker_id: str | None = Field(None, alias="workerId")
    lease_expires_at: AwareDatetime | None = Field(None, alias="leaseExpiresAt")
    """
    Set on claim, extended by heartbeat. A RUNNING job whose lease is in the past is reclaimable by the reaper.
    """
    attempts: int = Field(..., ge=0)
    max_attempts: int = Field(..., alias="maxAttempts", ge=1)
    error: StageError | None = None
    """
    The failure that terminated the job, copied from the failing stage.
    """
    created_at: AwareDatetime = Field(..., alias="createdAt")
    updated_at: AwareDatetime = Field(..., alias="updatedAt")
    started_at: AwareDatetime | None = Field(None, alias="startedAt")
    ended_at: AwareDatetime | None = Field(None, alias="endedAt")


class ClipForgeContracts(BaseModel):
    """
    The complete ClipForge wire protocol. The PWA and the worker are two independent implementations of the types defined here; both are generated from this file, so neither can drift from it. See docs/adr/0005-single-source-contracts.md. Every timestamp is an ISO 8601 date-time string, NOT a Firestore Timestamp: the store adapters convert at the boundary, which keeps this document language-neutral and lets the unit tier compare documents as plain JSON with no emulator running.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    job: Job | None = None
    job_event: JobEvent | None = Field(None, alias="jobEvent")
    stage: Stage | None = None
    source: Source | None = None
    ingest_error_code: IngestErrorCode | None = Field(None, alias="ingestErrorCode")
    transcript: Transcript | None = None
    transcript_ref: TranscriptRef | None = Field(None, alias="transcriptRef")
    candidate: Candidate | None = None
    clip: Clip | None = None
    clip_preview: ClipPreview | None = Field(None, alias="clipPreview")
    publication: Publication | None = None
    metric_snapshot: MetricSnapshot | None = Field(None, alias="metricSnapshot")
    calibration_report: CalibrationReport | None = Field(
        None, alias="calibrationReport"
    )
    channel: Channel | None = None
    user_profile: UserProfile | None = Field(None, alias="userProfile")
    worker_heartbeat: WorkerHeartbeat | None = Field(None, alias="workerHeartbeat")
    agent_report: AgentReport | None = Field(None, alias="agentReport")
    preference: Preference | None = None
    llm_preference_proposal: LlmPreferenceProposal | None = Field(
        None, alias="llmPreferenceProposal"
    )
    remake_options: RemakeOptions | None = Field(None, alias="remakeOptions")
    applied_remake: AppliedRemake | None = Field(None, alias="appliedRemake")
    llm_remake_note: LlmRemakeNote | None = Field(None, alias="llmRemakeNote")
    llm_clip_response: LlmClipResponse | None = Field(None, alias="llmClipResponse")
