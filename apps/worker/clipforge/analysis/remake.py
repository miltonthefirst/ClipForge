"""Reading a reviewer's note, and translating a script.

Both jobs go to the model that is already here — `qwen3.5:4b`, already pulled,
already brokered, already the thing ANALYZE talks to. Neither justifies a second
model class: turning "it keeps losing the ball" into a framing mode is a
classification over a fixed vocabulary, and translating forty words of
commentary is well inside what a 4B does competently.

## Why the note is interpreted at all

Because the alternative is a form. The reviewer is on a phone, watching a clip
that is wrong, and the thing they can express fastest and most precisely is a
sentence about what is wrong with it. Making them first translate that sentence
into *framing mode: TRACK, maxPanPctPerSec: 12* is the interface failing to do
its job.

## The three rules that keep it honest

* **It never overrides what was stated.** A field the reviewer set explicitly is
  not up for reinterpretation; the model fills gaps and nothing else. This is
  enforced in `apply_interpretation`, not asked for in the prompt, because a
  prompt is a request and this needs to be a guarantee.
* **It only touches what the note was about.** The model declares the note's
  topics before it answers anything, and a setting belonging to a topic it did
  not declare is discarded. This is worth more than it looks: asked to read "can
  we have this in Spanish" with every field required, a 4B cheerfully also
  returns a framing mode and a crop anchor, and a remake that silently reframes
  a clip nobody complained about is worse than one that does nothing.
* **What it decided is recorded.** `NoteInterpretation` goes onto the clip with
  the model's own one-sentence reading. A remake that comes out wrong is nearly
  always either a misread note or a correctly-read note badly executed, and
  without the reading written down those are indistinguishable afterwards.

Measured against the real model in `tests/gpu/test_remake_notes.py`;
the shape of `LlmRemakeNote` is the way it is because the two simpler shapes
were tried first and each failed in its own direction.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from clipforge_contracts import (
    CropAnchor,
    Framing,
    FramingMode,
    LlmRemakeNote,
    NoteAudio,
    NoteCrop,
    NoteFraming,
    NoteInterpretation,
    NoteObscure,
    NoteTopic,
    ObscureOptions,
    RemakeOptions,
    SpeechMode,
    UnsupportedAsk,
    VoiceCaptions,
    VoiceOptions,
)
from pydantic import BaseModel, Field

from clipforge.media.speech import DEFAULT_VOICES, SpeechError, kokoro_language
from clipforge.models.ollama import OllamaClient, OllamaError
from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = [
    "NOTE_PROMPT_VERSION",
    "NOTE_SYSTEM_PROMPT",
    "Applied",
    "Translation",
    "apply_interpretation",
    "build_note_prompt",
    "describe",
    "interpret_note",
    "translate",
]

NOTE_PROMPT_VERSION = "v1"

NOTE_SYSTEM_PROMPT = """\
You turn a reviewer's complaint about a short video clip into settings.

You are not editing the video and you are not writing copy. You read one \
sentence of feedback and decide which of a fixed set of controls it is talking \
about.

Say NOT_MENTIONED, NONE or 0 for every control the note does not raise. Most notes mention one \
thing. A note about framing says nothing about language, and inventing a value \
for it would change something the reviewer did not ask to change — which is \
worse than leaving it alone, because they will not know you did it.

Set the control itself, not just a description of it. If the note is about \
where the picture looks, `framingMode` must have a value; if it names a side, \
`crop` must have one too. The `summary` is written last and only describes \
settings you have already made — a summary that claims a change no field \
records is the one failure that matters here.

Some notes ask for things none of these controls can do: removing a watermark \
or burnt-in text, changing the music, zooming onto one person, slow motion, \
cutting the middle out, recolouring. When that happens, name it in \
`unsupported` and leave the controls alone. Do NOT bend the request into the \
nearest control that exists — "remove the watermark in the top right" is not a \
framing change, and answering it with one produces a clip that is differently \
wrong and still has the watermark."""

_FRAMING_GUIDE = """\
Framing controls which part of a wide video ends up in a tall clip:
- AS_RENDERED: a fixed window that does not move. Use with `crop` when the note \
says the interesting thing is consistently on one side ("the goal is on the \
left", "the speaker sits to the right").
- FIT: the whole wide frame, shrunk to fit, with the gaps filled. Use when the \
note says things are being cut off, or that they want to see the whole pitch, \
field or stage. Nothing can be lost in this mode.
- TRACK: the window follows whatever is moving. Use when the note says the clip \
loses track of something that moves — a ball, a player, a car. This is also the \
answer when a note asks for the window to move without saying where to, because \
you cannot supply the positions a hand-set pan needs."""


def build_note_prompt(
    note: str,
    *,
    already_set: list[str],
    clip_language: str | None,
    duration_sec: float | None,
    standing: list[str] | None = None,
) -> str:
    """The user half of the note prompt.

    `already_set` is listed so the model does not spend its answer on fields
    that will be discarded anyway — the discarding happens regardless, in
    `apply_interpretation`, but a model told what is already decided produces a
    more useful `summary` for the fields that remain.
    """
    lines = [
        "The reviewer wrote this about a clip they just watched:",
        "",
        note.strip(),
        "",
        _FRAMING_GUIDE,
        "",
        "First answer `topics`: which of FRAMING, LANGUAGE, AUDIO, TIMING and "
        "OBSCURE this "
        "note actually raises. Usually one. Anything you say about a topic you "
        "did not list is discarded, so listing a topic the note did not raise "
        "changes something nobody asked to change.",
        "",
        "Also available:",
        "- language: a BCP-47 tag if the note asks for another language, "
        "otherwise the literal string NONE.",
        "- audio: REPLACE if the note wants the original audio gone, KEEP_UNDER "
        "if it wants it kept quietly beneath a narration, NOT_MENTIONED if it "
        "says nothing about sound.",
        "- startDeltaSec / endDeltaSec: seconds to move the cut's start or end, "
        "and 0 if the note does not say it begins or ends at the wrong moment. "
        "Negative starts earlier; positive ends later.",
        "- obscure: whether something burnt into the picture should be hidden "
        "— a channel logo or bug, a broadcaster's watermark, a score bar, a "
        "clock, burnt-in text. Answer ANYWHERE when the note asks for something "
        "to be hidden but does not say where on screen it is, which is the usual "
        "case: the system finds it by looking at the footage. Answer a corner "
        "(TOP_LEFT, TOP_RIGHT, BOTTOM_LEFT, BOTTOM_RIGHT) or a band (TOP, "
        "BOTTOM) only if the note says where. NOT_MENTIONED if the note does not "
        "ask for anything to be hidden.",
        "",
        "Every field must have a value. Use NOT_MENTIONED, NONE or 0 to say the "
        "note did not raise something — those are answers, not blanks.",
        "",
        "Last, `unsupported`: anything the note asks for that NONE of the controls "
        "above can express — different music, a zoom onto one person, slow "
        "motion, cutting the middle out, a colour change. Usually empty. A logo "
        "or watermark to remove is NOT unsupported any more; that is what "
        "`obscure` is for. Listing something here does not stop the remake; it "
        "records the part that will not be met so the reviewer is told rather "
        "than left to notice.",
    ]
    if clip_language:
        lines.append(f"\nThe clip is currently in {clip_language}.")
    if duration_sec:
        lines.append(f"It is {duration_sec:.0f} seconds long.")
    if standing:
        # What this reviewer has taught before. Context, not instruction: a note
        # that contradicts one of these is the reviewer changing their mind, and
        # the note wins.
        lines.append(
            "\nThings this reviewer has already established about clips like this:\n"
            + "\n".join(f"- {item}" for item in standing[:10])
        )
    if already_set:
        lines.append(
            "\nThe reviewer has already set these explicitly, so anything you say "
            f"about them is ignored: {', '.join(already_set)}."
        )
    lines.append(
        "\nWrite `summary` as one sentence: what the reviewer wants, and what you changed."
    )
    return "\n".join(lines)


def interpret_note(
    client: OllamaClient,
    note: str,
    *,
    options: RemakeOptions,
    clip_language: str | None = None,
    duration_sec: float | None = None,
    standing: list[str] | None = None,
) -> tuple[LlmRemakeNote | None, NoteInterpretation]:
    """Put the note to the model. Never raises.

    A note that cannot be read is not a reason to refuse a remake: the reviewer
    may well have set everything that matters explicitly and written the note
    for the record. So a model failure comes back as `understood=False` with the
    reason in the summary, and the caller proceeds on what it already had.
    """
    stated = _stated_fields(options)
    try:
        answer = client.generate_structured(
            schema_model=LlmRemakeNote,
            system=NOTE_SYSTEM_PROMPT,
            prompt=build_note_prompt(
                note,
                already_set=stated,
                clip_language=clip_language,
                duration_sec=duration_sec,
                standing=standing,
            ),
            # A little sampling, unlike selection. Reading a sentence has one
            # right answer far more often than judging a clip does, and at
            # temperature 0 a 4B occasionally locks onto a wrong reading it
            # cannot be nudged out of.
            temperature=0.2,
        )
    except OllamaError as exc:
        log.warning("remake.note_unreadable", error=str(exc))
        # Truncated, because this is the handler that must not fail. `summary`
        # is capped at 600 characters by the contract, and the likeliest error
        # to arrive here is a repair failure carrying a whole pydantic
        # ValidationError — comfortably over the cap. Building an oversized
        # NoteInterpretation would raise *inside* the except block and take down
        # a remake whose real work was fully specified, on a job with one attempt.
        reason = str(exc)
        if len(reason) > 320:
            reason = reason[:317] + "..."
        return None, NoteInterpretation(
            understood=False,
            summary=f"The note could not be read by the local model ({reason}). "
            "The remake used only the settings that were chosen explicitly.",
            model=client.model,
        )

    log.info("remake.note_read", summary=answer.summary[:120])
    return answer, NoteInterpretation(
        understood=True, summary=answer.summary[:600], model=client.model
    )


@dataclass(frozen=True)
class Applied:
    """The resolved request, and an honest account of how it got that way."""

    options: RemakeOptions
    # One phrase per change the note caused, in the reviewer's terms. This is
    # what the recorded summary is built from, rather than the model's own
    # description of its answer: settings outside the declared topics are
    # discarded after the model has already written about them, so its wording
    # claimed a crop that was never applied on two of the first five remakes.
    changes: list[str]
    # Where the note and the form disagreed, and which won.
    conflicts: list[str]
    # A corner or band the note named, for detection to search in; None when it
    # asked for something to be hidden without saying where, which is usual.
    obscure_where: str | None = None
    # Asks the model filed as impossible that this build can in fact do. The
    # caller must not refuse these — see `apply_interpretation`.
    absorbed: list[UnsupportedAsk] = field(default_factory=list)


def apply_interpretation(options: RemakeOptions, answer: LlmRemakeNote | None) -> Applied:
    """Fold the model's reading into the request.

    The rule used to be "never override anything the reviewer stated", which
    sounds right and was wrong in practice, because a form cannot tell a choice
    from a default. A note reading *"change commentary voice to English"* was
    overruled by a language dropdown nobody had touched, and the clip came back
    in Spanish.

    So the rule is now narrower and better matched to how the two inputs are
    produced: **the note wins where it names something explicitly**, the form
    fills in everything the note is silent about, and any disagreement is
    recorded and shown rather than resolved quietly. The note is written last,
    while watching the clip, and is the most specific thing the reviewer said.
    """
    if answer is None:
        return Applied(options=options, changes=[], conflicts=[])

    updated = options.model_copy(deep=True)
    topics = set(answer.topics or ())
    changes: list[str] = []
    conflicts: list[str] = []

    # Everything below is gated on the topic the model declared. Without this
    # the reading of "put it in Spanish" also arrives carrying a framing mode
    # and a crop anchor, and the remake quietly reframes a clip whose framing
    # nobody complained about.
    mode = _framing_mode(answer.framing_mode) if NoteTopic.FRAMING in topics else None
    crop = (
        answer.crop.value
        if NoteTopic.FRAMING in topics
        and answer.crop is not NoteCrop.NOT_MENTIONED
        and _names_a_side(options.notes)
        else None
    )

    # ── Framing ──────────────────────────────────────────────────────────────
    if updated.framing is None and (mode is not None or crop is not None):
        resolved_mode = mode or FramingMode.AS_RENDERED
        # An anchor only means anything to a window that holds still. Carrying
        # one onto TRACK or FIT records a setting the renderer ignores, which
        # then shows up in the clip's history as a decision nobody made.
        keep_crop = crop if resolved_mode is FramingMode.AS_RENDERED else None
        updated.framing = Framing(
            mode=resolved_mode,
            crop=CropAnchor(keep_crop) if keep_crop is not None else None,
        )
        changes.append(
            f"set the framing to {updated.framing.mode.value}"
            + (f" on the {keep_crop}" if keep_crop else "")
        )
    elif (
        updated.framing is not None
        and updated.framing.crop is None
        and crop is not None
        and updated.framing.mode is FramingMode.AS_RENDERED
    ):
        # A mode was stated but no anchor. An anchor is a detail of that mode,
        # so filling it in completes the reviewer's instruction rather than
        # contradicting it.
        updated.framing.crop = CropAnchor(crop)
        changes.append(f"anchored the window to the {crop}")

    # ── Voice ────────────────────────────────────────────────────────────────
    language = _language(answer.language) if NoteTopic.LANGUAGE in topics else None
    if language is not None:
        try:
            resolved = kokoro_language(language)
        except SpeechError:
            # An unsupported language is not a failure of the remake: the rest
            # of the note may be perfectly actionable, and refusing the whole
            # request over a voice nobody can speak helps no one.
            log.info("remake.note_language_unsupported", language=language)
        else:
            if updated.voice is None:
                updated.voice = VoiceOptions(
                    mode=(
                        SpeechMode.BED
                        if NoteTopic.AUDIO in topics and answer.audio is NoteAudio.KEEP_UNDER
                        else SpeechMode.REPLACE
                    ),
                    voice=DEFAULT_VOICES.get(resolved, "af_heart"),
                    language=language,
                    translate=True,
                    captions=VoiceCaptions.REBUILD,
                )
                changes.append(f"set the language to {language}")
            elif _different_language(updated.voice.language, language):
                # The note named a language and the form named another. The
                # note wins: it is the more specific and more recent statement,
                # and a form cannot distinguish a deliberate choice from an
                # untouched default. Recorded either way, because being
                # overruled silently is how the reviewer lost an argument they
                # did not know they were having.
                conflicts.append(
                    f"the note asked for {language} and the form said "
                    f"{updated.voice.language}; the note was used"
                )
                updated.voice.language = language
                updated.voice.voice = DEFAULT_VOICES.get(resolved, updated.voice.voice)
                changes.append(f"set the language to {language}")

    # ── Hiding a fixed mark ──────────────────────────────────────────────────
    # Two ways in, and both are read. The first is the OBSCURE topic, which is
    # how a current model says it. The second is `unsupported`, which is where
    # every model said it until this build existed and where a model that has
    # not noticed the new field will keep saying it — the reviewer's three real
    # notes all came back as REMOVE_WATERMARK. Absorbing that rather than
    # refusing it is what makes the feature work on a note written the old way.
    unsupported = set(answer.unsupported or ())
    misfiled = unsupported & {UnsupportedAsk.REMOVE_WATERMARK, UnsupportedAsk.REMOVE_OVERLAY_TEXT}
    by_topic = NoteTopic.OBSCURE in topics and answer.obscure is not NoteObscure.NOT_MENTIONED

    obscure_where: str | None = None
    absorbed: list[UnsupportedAsk] = []
    if by_topic or misfiled:
        absorbed = sorted(misfiled, key=lambda ask: ask.value)
        if by_topic and answer.obscure is not NoteObscure.ANYWHERE:
            obscure_where = answer.obscure.value
        if updated.obscure is None:
            updated.obscure = ObscureOptions(auto=True)
        elif not updated.obscure.auto and not updated.obscure.regions:
            # The form sent an object with nothing turned on, which is what an
            # untouched control looks like. The note is the more specific
            # statement, so it turns detection on rather than being outvoted by
            # a checkbox nobody ticked.
            updated.obscure.auto = True
        else:
            # Detection was already asked for, or boxes were already drawn.
            # Nothing to change, and nothing to claim in the summary.
            by_topic = False
        if by_topic or misfiled:
            where = f" in the {obscure_where.lower().replace('_', ' ')}" if obscure_where else ""
            changes.append(f"looked for a fixed mark to hide{where}")

    # ── Trims ────────────────────────────────────────────────────────────────
    # `0` is this schema's "not mentioned", so a falsy answer never overwrites.
    if NoteTopic.TIMING in topics:
        if not updated.start_delta_sec and answer.start_delta_sec:
            updated.start_delta_sec = answer.start_delta_sec
            changes.append(f"moved the start by {answer.start_delta_sec:+g}s")
        if not updated.end_delta_sec and answer.end_delta_sec:
            updated.end_delta_sec = answer.end_delta_sec
            changes.append(f"moved the end by {answer.end_delta_sec:+g}s")

    return Applied(
        options=updated,
        changes=changes,
        conflicts=conflicts,
        obscure_where=obscure_where,
        absorbed=absorbed,
    )


def describe(answer: LlmRemakeNote | None, applied: Applied) -> str:
    """The sentence recorded on the clip, built from what actually happened.

    The model writes a summary of its own answer, and the caller then discards
    whatever fell outside the declared topics — so its wording is a description
    of an intention, not of an outcome. Recording it verbatim told two
    reviewers their clip had been anchored "to the right" when no crop was ever
    set. The model's reading is still worth keeping; it is just no longer
    allowed to be the record of what was done.
    """
    read = (answer.summary or "").strip() if answer is not None else ""
    if applied.changes:
        did = "; ".join(applied.changes)
        return f"Read as: {read} Applied: {did}."[:600] if read else f"Applied: {did}."[:600]
    if read:
        return f"Read as: {read} Nothing in it changed a setting you had not already chosen."[:600]
    return "Nothing in the note changed a setting."


_SIDE_WORDS = ("left", "right", "centre", "center", "middle", "side")


def _names_a_side(note: str | None) -> bool:
    """Did the reviewer actually mention a side of the frame?

    The model volunteers `crop: right` habitually \u2014 it appeared on notes about
    following the ball and about a watermark, and on two of the first five real
    remakes it reached the recorded summary as "and crop to right" when no crop
    had been applied at all. An anchor is a specific instruction about where to
    point the window, so requiring the note to contain the word costs nothing
    and removes a whole class of invented settings.
    """
    if not note:
        return False
    lowered = note.lower()
    return any(word in lowered for word in _SIDE_WORDS)


def _different_language(a: str, b: str) -> bool:
    """Do two tags name different languages?

    Compared on the primary subtag: `en-us` and `en-GB` are the same argument
    about accents, not about language, and overruling a reviewer's chosen
    accent because the note said "English" would be the same mistake in reverse.
    """
    return a.strip().lower().split("-")[0] != b.strip().lower().split("-")[0]


def _framing_mode(value: NoteFraming) -> FramingMode | None:
    """The note's framing mode, or None when it did not name one.

    The two enums stay separate types on purpose: `NoteFraming` is what a model
    may say and carries NOT_MENTIONED, `FramingMode` is what may be rendered and
    does not. Collapsing them would put a value into the render path that the
    render path has no meaning for.
    """
    if value is NoteFraming.NOT_MENTIONED:
        return None
    return FramingMode(value.value)


def _language(value: str) -> str | None:
    """The note's language, or None for the sentinel.

    Matched against the empty string as well as NONE, case-insensitively: the
    schema asks for the literal `NONE`, and a model answering `none` or `""`
    means the same thing and must not be treated as a language tag.
    """
    cleaned = (value or "").strip()
    if not cleaned or cleaned.upper() == "NONE":
        return None
    return cleaned


def _stated_fields(options: RemakeOptions) -> list[str]:
    stated: list[str] = []
    if options.framing is not None:
        stated.append("framing")
    if options.voice is not None:
        stated.append("language and voice")
    if options.start_delta_sec:
        stated.append("the start of the cut")
    if options.end_delta_sec:
        stated.append("the end of the cut")
    if options.obscure is not None and (options.obscure.auto or options.obscure.regions):
        stated.append("what to hide in the picture")
    return stated


# ── Translation ──────────────────────────────────────────────────────────────


class Translation(BaseModel):
    """The constrained shape of a translation response.

    A model asked to translate will otherwise preface the answer — "Sure, here
    is the Spanish:" — and that preface would be spoken aloud by the
    synthesiser. Constraining the shape is cheaper and more reliable than
    stripping prefaces afterwards.
    """

    text: str = Field(description="The translation, and nothing else.")


TRANSLATE_SYSTEM_PROMPT = """\
You translate the spoken words of a short video clip.

The result is going to be read aloud by a speech synthesiser, so write it the \
way it will be spoken: no notes, no alternatives, no bracketed explanations, no \
quotation marks around the whole thing. Expand digits and abbreviations into \
words, because a synthesiser reads "3-1" and "FC" badly.

Keep it close to the same length as the original. A translation that runs long \
overruns the video it has to fit."""


def translate(client: OllamaClient, text: str, *, target_language: str) -> str:
    """Translate a clip's words into the language it will be spoken in.

    Raises `OllamaError` on failure rather than degrading: unlike a note, this
    is load-bearing. Speaking the original text with the target language's
    phonetics produces something no listener wants, so a failed translation
    should stop the remake rather than quietly narrate gibberish.
    """
    cleaned = " ".join(text.split())
    if not cleaned:
        return ""

    answer = client.generate_structured(
        schema_model=Translation,
        system=TRANSLATE_SYSTEM_PROMPT,
        prompt=(f"Translate into {target_language}. Reply with the translation only.\n\n{cleaned}"),
        temperature=0.2,
    )
    result = " ".join(answer.text.split())
    log.info(
        "remake.translated",
        language=target_language,
        source_chars=len(cleaned),
        result_chars=len(result),
    )
    return result
