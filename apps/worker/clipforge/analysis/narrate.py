"""Writing the line a clip speaks, given what it says and what it shows.

## What this replaces

"Translate the transcript" — which is correct exactly as often as the transcript
is, and on sports commentary that is not often. The three failures that produced
this module all came from the same source and the same match:

* Whisper heard *"très mal à beaude glim cette frappe pure latérale gauche
  municois"*. Translated faithfully, that became *"very bad beauty glim this
  pure left lateral munitions shot"*, which a synthesiser then read aloud.
* On another cut it heard the commentary perfectly — *"Pavlovitch, bonne passe,
  on a franchi un premier rideau"* — and the translation came back as the same
  French, which an English voice pronounced phonetically.
* Both clips were then captioned from a re-transcription of that audio, so the
  burnt-in text said *"7,000 flaps go. She is magnificent one and"*.

Every component behaved correctly. The chain had no step that could ask whether
the words meant anything.

## The shape of the fix

One call, handed **both** the transcript and a description of what is on screen
(`clipforge.media.vision`), asked to produce the spoken line directly rather than
a translation of anything.

That ordering matters more than it looks. A translator's job is fidelity, so
given nonsense it produces faithful nonsense — it has no licence to do otherwise.
A writer given the same nonsense plus a photograph of a football pitch has both
the licence and the evidence to write the sentence the commentator was obviously
saying. `LlmNarration.transcriptUsable` is where that licence is granted, and it
comes first in the schema so the judgement is made before the script that
depends on it.

## The second failure: a line that is only the picture

Two clips reached Review narrated with *"This is a dramatic anime moment with
characters showing concern, determination, distress, and shock against a dark
background"* and *"Then the scene cuts. We see humans lying there."* Both are
accurate. Both are the description of the frames, above in the same prompt,
read back to somebody who is already looking at them, and the operator's verdict
was that they make the video not worth watching.

The prompt caused it — "write from the pictures instead" is an instruction to
paraphrase the one paragraph of prose in front of the model — but a prompt alone
does not fix it, for the reason the language gate exists: a model is not a
witness to its own output. So `echoes_the_picture` counts, the way `reads_as`
counts, and a line that trips it is sent back once with what it did wrong. What
comes back the second time is used when it is clean; when it is not, the
reviewer is told the narration is dull rather than left to discover it.

## What still refuses

Grounding is not a guarantee, so `clipforge.analysis.feedback` keeps every gate
it had, and this module adds one: the script must actually be in the language
that was asked for. That is checked by counting function words rather than by
asking the model, because a model that has just produced French will happily
report that it produced English.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from clipforge_contracts import LlmNarration

from clipforge.media.vision import VisualContext
from clipforge.models.ollama import OllamaClient, OllamaError
from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = [
    "NARRATE_SYSTEM_PROMPT",
    "Narration",
    "build_narration_prompt",
    "echoes_the_picture",
    "reads_as",
    "too_thin",
    "word_floor",
    "write_narration",
]

# Roughly how many words a synthesiser gets through in a second of clip at a
# natural pace. Used to give the model a budget rather than a vague "keep it
# short": narration that overruns the picture is the single most common way a
# re-voiced clip goes wrong, and it is invisible until someone watches the end.
_WORDS_PER_SECOND = 2.4

# And how little is too little. A sixteen-second clip came back with nineteen
# words in it: eight seconds of voice and eight of nothing, which reads as an
# afterthought rather than as restraint. The floor is deliberately well under
# the ceiling — there is no penalty for a line that ends early, and a real one
# for a line that runs past the picture.
_BUDGET_FLOOR = 0.6

# And how far under that floor is a different problem rather than a short line.
# Deliberately generous: a narration that ends a couple of seconds early is
# fine, and the failure this catches is the one that is over before the clip
# has started.
_FAR_UNDER = 0.7

# The retry's own seed. `OllamaClient.generate_structured` defaults to seed 0,
# so asking again with the same prompt returns the same words: two remakes of
# one clip, days apart, produced byte-identical narration on both the first
# attempt and the correction. Sampling needs somewhere else to start from, and
# a constant keeps the pair reproducible: the same clip always gets the same
# two attempts.
_RETRY_SEED = 7

# The most common function words in each language a voice exists for, as one
# string each so the table stays readable.
_COMMON: dict[str, str] = {
    "en": "the a an and is are was were of to in on it that this with for as at he "
    "she they we you but not from has have had his her their there here",
    "fr": "le la les un une des et est sont de du au aux en dans sur il elle ils "
    "elles qui que ne pas pour avec ce cette son sa ses par plus on mais tout "
    "bien peut va fait encore tres sans leur",
    "es": "el la los las un una de del y es son en con por para que no se su sus lo "
    "al como pero este esta muy mas ya pero cuando donde",
    "it": "il lo la i gli le un una di del e con per che non si sua suo come ma "
    "questo questa piu da al sono nel nella anche",
    "pt": "o a os as um uma de do da e em com por para que nao se seu sua como mas "
    "este esta mais ja ao dos das pelo pela",
    "de": "der die das ein eine und ist sind von zu in mit auf fur dass nicht sich er "
    "sie es aber auch noch bei aus dem den einen",
}


def _discriminative() -> dict[str, frozenset[str]]:
    """Keep only the words that belong to ONE of these languages.

    Overlap is what broke the first version. "a" is an article in English and a
    verb in Portuguese; "on" is English and also the commonest French pronoun;
    "de", "la" and "que" are shared by four of the six. A short sports line has
    only a handful of function words in it, so two or three ambiguous hits are
    enough to tie — and a tie on a French sentence asked for in English let the
    French through, which is the exact failure this gate exists to catch.
    """
    seen: dict[str, int] = {}
    for words in _COMMON.values():
        for word in set(words.split()):
            seen[word] = seen.get(word, 0) + 1
    return {
        language: frozenset(word for word in set(words.split()) if seen[word] == 1)
        for language, words in _COMMON.items()
    }


_FUNCTION_WORDS = _discriminative()


@dataclass(frozen=True)
class Narration:
    """The line to speak, and an honest account of where it came from."""

    script: str
    # False when the model judged the transcript too mangled to carry meaning
    # and wrote from the pictures instead. Surfaced to the reviewer, because a
    # clip narrated from its visuals is a different kind of thing from one
    # narrated from its commentary and they deserve to know which they have.
    from_transcript: bool
    # What the model said about that judgement, for the log and nothing else.
    reasoning: str
    grounded: bool  # was there a visual context to work from at all
    # True when what came back was still mostly the description of the frames
    # after being sent back once. The clip is made either way — a dull narration
    # is worth more than a failed job — but the reviewer is told, because this
    # is invisible in a transcript that reads as perfectly good English.
    echoed: bool = False


def write_narration(
    client: OllamaClient,
    *,
    transcript: str,
    target_language: str,
    duration_sec: float,
    visual: VisualContext | None,
    source_title: str | None = None,
) -> Narration | None:
    """Produce the spoken line. Returns None when the model cannot be reached.

    None rather than raising, because the caller has a decision to make that
    this function cannot: a remake that was only ever about the voice must fail,
    and one that was mostly about the framing might reasonably go on without a
    new narration.
    """
    prompt = build_narration_prompt(
        transcript=transcript,
        target_language=target_language,
        duration_sec=duration_sec,
        visual=visual,
        source_title=source_title,
    )

    # A little sampling. Writing one sentence has more than one right answer,
    # and at temperature 0 a small model locks onto the transcript's own
    # wording even when it has just said it is unusable.
    first = _ask(client, prompt, temperature=0.3, grounded=visual is not None)
    if first is None:
        return None

    floor = word_floor(duration_sec)
    echo = echoes_the_picture(first.script, visual)
    thin = None if echo else too_thin(first.script, floor)
    if echo is None and thin is None:
        return first

    # Sent back once, told exactly what it did. Warmer than the first ask
    # because the point is a different line, and a model handed back its own
    # prompt at the same temperature tends to return its own answer.
    reason = echo or thin or ""
    log.info("narrate.rejected", reason=reason, words=len(first.script.split()))
    correction = _CORRECTION if echo else _TOO_SHORT
    second = _ask(
        client,
        f"{prompt}\n\n{correction.format(reason=reason)}",
        temperature=0.6,
        seed=_RETRY_SEED,
        grounded=visual is not None,
    )
    second_echo = echoes_the_picture(second.script, visual) if second is not None else "none"
    if second is not None and second_echo is None and too_thin(second.script, floor) is None:
        return second

    if echo is None:
        # Only ever short. Keep whichever of the two says more, unless saying
        # more meant reciting the picture: asking for a longer line is an
        # invitation to pad it out with description, and a clip shipped
        # saying "Look at their faces" is what this costs when it is not
        # checked. A thin line is a disappointment; a recital is the defect.
        if (
            second is not None
            and second_echo is None
            and len(second.script.split()) > len(first.script.split())
        ):
            return second
        return first

    # Both describe the picture. The first is kept rather than the second: it
    # was written from a clean prompt, and a second attempt that failed the
    # same way is not evidence of anything better. The flag is what matters.
    log.info("narrate.echoed_twice")
    return replace(first, echoed=True)


def _ask(
    client: OllamaClient,
    prompt: str,
    *,
    temperature: float,
    grounded: bool,
    seed: int = 0,
) -> Narration | None:
    """One call, cleaned up. None when the model cannot be reached or said nothing."""
    try:
        answer = client.generate_structured(
            schema_model=LlmNarration,
            system=NARRATE_SYSTEM_PROMPT,
            prompt=prompt,
            temperature=temperature,
            seed=seed,
        )
    except OllamaError as exc:
        log.warning("narrate.unavailable", error=str(exc))
        return None

    script = " ".join((answer.script or "").split())
    if not script:
        return None

    log.info(
        "narrate.written",
        usable=answer.transcript_usable,
        grounded=grounded,
        words=len(script.split()),
    )
    return Narration(
        script=script,
        from_transcript=bool(answer.transcript_usable),
        reasoning=(answer.reasoning or "").strip(),
        grounded=grounded,
    )


NARRATE_SYSTEM_PROMPT = """\
You write the voice-over for one short video clip.

You are given a machine transcription of the clip's own audio and, usually, a \
description of what is actually visible in it. The transcription is often wrong. \
It is produced by a speech recogniser with no idea what it is listening to, and \
on sport, music and fast speech it mangles names, invents words and produces \
fluent nonsense. Treat it as a witness, not as a record.

**First decide whether the transcription is usable.** It is usable when it reads \
as real sentences that mean something consistent with the pictures. It is NOT \
usable when it is word salad, when the names are obviously garbled, or when it \
describes something the pictures contradict. Say which, and say why in one \
sentence, before you write anything.

Recognisable words are not usable ones. "how selfish my brother the monster say \
domain about to hit five punches" has half a dozen words you can make out in it \
and is still not a sentence anybody said. A few phrases you recognise inside a \
wreck is what an unusable transcription looks like from the inside.

Then write the line.

- If the transcription is usable, carry its **content** across into the target \
language, correcting names and terms using what is visible. A commentator saying \
a player's name badly transcribed is still that player. Never copy the \
transcription's own broken wording across: whatever you write, write it as \
sentences, with a subject, a verb and punctuation, that a person could read \
aloud without stumbling. If the words you were given will not make one, they \
were not usable and you should have said so.
- If it is not usable, work from the pictures instead — but write ABOUT what is \
happening, not a description OF it.

**The viewer is already watching the picture.** Telling them what is on the \
screen is the single fastest way to make them leave: they can see it, you are \
just slower than their eyes. What they cannot see is what it means, what is at \
stake, who these people are to each other, or what the question is. That is your \
half of the job, and the only reason the clip has a voice on it at all.

- **Open with the most interesting thing you have**, inside the first six words. \
Not a label, not a setting, not "this is". If all you have is a question, ask it.
- **Never read the pictures back.** "Characters look shocked against a dark \
background" is the description you were handed, said aloud to someone looking \
straight at it.
- **Never mention the footage.** No "this clip", no "this video", no "we see", \
no "the scene cuts", no "the camera". Talk about the thing, never about the \
recording of the thing.
- **End on something.** A question, or the one point worth taking away. Not a \
fade into another description.

None of this is a licence to invent. Stakes you cannot see are not yours to \
claim, and a sentence you are unsure of is better left out than dressed up.

**Fill the clip.** You are given a range of words, and the bottom of it is as \
real as the top: a line that is over in two seconds leaves the rest of the video \
with nothing being said over it, which is the same dull result by another route. \
Ask a question and answer it, or follow the thought one step further. Say more \
about what you know, never more about the picture.

Rules that hold either way:

- Write ONLY in the target language. Not a word of the original.
- Never invent a scoreline, a minute, a competition or a name that is not in the \
transcription or visible on screen. If you do not know who scored, say what \
happened without naming anyone. Do not expand an abbreviation on the score bug \
into a club name you are guessing at: "BOD" is "BOD".
- **Never assert an outcome.** Do not say a thing succeeded — in sport, that a \
goal was scored, a save was made, a card was shown or a point was won — unless \
the transcription says so or it is written on screen. A few stills are evidence \
that something was attempted, never that it succeeded: the frames stop, the move \
does not.
- **The examples in these instructions are about football because that is where \
this went wrong first.** They are not a vocabulary to borrow. A clip that is not \
sport has no scoreline, no competition and no minute, and reaching for one is \
inventing a fact.
- **Use the transcription's content wherever it reads as real words**, even when \
you have judged it unusable overall. These two things are not in tension: a \
transcript can be half nonsense and half a perfectly clear phrase, and throwing \
away the clear half leaves you describing a still photograph. The names and \
actions it gets right are the only access you have to what was actually said.
- Where the transcription and the pictures disagree about a detail, leave the \
detail out. Neither is reliable enough to overrule the other, and a line that \
omits it is right either way.
- It is spoken aloud by a synthesiser, so write it as it should sound: no \
speaker labels, no stage directions, no brackets, no quotation marks around the \
whole thing. Expand digits and abbreviations into words — a synthesiser reads \
"3-1" and "FC" badly.
- Stay inside the word budget. A narration that runs past the end of the picture \
is cut off mid-sentence."""


def build_narration_prompt(
    *,
    transcript: str,
    target_language: str,
    duration_sec: float,
    visual: VisualContext | None,
    source_title: str | None = None,
) -> str:
    """The user half.

    The pictures come **before** the transcript, deliberately. A model reads its
    prompt in order and anchors on what it sees first, and the whole purpose of
    this module is that the pictures are the more trustworthy of the two.
    """
    budget = max(8, int(duration_sec * _WORDS_PER_SECOND))
    floor = word_floor(duration_sec)
    lines: list[str] = [f"Target language: {target_language}", f"Clip length: {duration_sec:.0f}s"]
    if source_title:
        lines.append(f"It was cut from a video called: {source_title}")

    if visual is not None:
        lines += ["", visual.as_prompt()]
    else:
        lines += [
            "",
            "No description of the pictures is available for this clip, so the "
            "transcription is all you have. Be correspondingly careful: if it "
            "does not read as real sentences, say so and write something short "
            "and general rather than repeating it.",
        ]

    lines += [
        "",
        "Machine transcription of the clip's audio (may be wrong):",
        transcript.strip() or "(the recogniser returned nothing)",
        "",
        f"Write {floor} to {budget} words in {target_language} — roughly "
        f"{floor / _WORDS_PER_SECOND:.0f} to {budget / _WORDS_PER_SECOND:.0f} seconds of "
        f"speech, over a clip {duration_sec:.0f} seconds long. Fewer than {floor} words "
        f"is too little.",
    ]
    return "\n".join(lines)


# Talking about the recording rather than about what is in it. Every one of
# these was written by the model at some point, and none of them belongs in a
# voice-over: a viewer holding a phone does not need to be told they are
# watching a video.
_ABOUT_THE_FOOTAGE: tuple[str, ...] = (
    "this is a",
    "this is the",
    "we see",
    "we can see",
    "you can see",
    "you see",
    "here we",
    "the video",
    "this video",
    "this clip",
    "the clip",
    "the footage",
    "the frames",
    "the first frame",
    "the camera",
    "the screen shows",
    "the scene cuts",
    "the scene shifts",
    "it cuts to",
    "cuts to black",
    "watch as",
    "in this moment",
    # Pointing at the picture without naming it. "Look at their faces" is the
    # description again, in the imperative. Bare "look at that" is left alone:
    # it is idiomatic commentary about an action rather than a frame.
    "look at their",
    "look at his face",
    "look at her face",
    "look at these",
    "look at those",
    "notice the",
    "notice how",
)

# Below this there is not enough of a line to judge, and a short one is not the
# failure anyway: "Nobody walks away from this" shares every content word it has
# with the picture and is a perfectly good hook.
_RECITAL_MIN_WORDS = 8

# How much of a line has to come from the description before it IS the
# description. Set high on purpose: narration that shares vocabulary with what
# is on screen is normal and correct, and the failure is the line that shares
# nearly all of it.
_RECITAL_SHARE = 0.7


def _content_words(text: str) -> set[str]:
    """The words that carry the meaning: no function words, nothing tiny."""
    stop = frozenset(_COMMON["en"].split())
    words = (word.strip(".,!?;:\"'()[]{}<>\u00ab\u00bb\u2014-").lower() for word in text.split())
    return {word for word in words if len(word) > 2 and word not in stop}


def echoes_the_picture(script: str, visual: VisualContext | None) -> str | None:
    """Is this narration just the description of the frames, read aloud?

    The reason it failed, or None when the line says something of its own.

    Counted rather than asked, for the same reason `reads_as` is counted: a
    model that has just paraphrased the paragraph above its own answer reports
    that it wrote an original line. Two arithmetic tests, both cheap:

    * it talks about the footage, which no voice-over should ever do;
    * or nearly every content word in it came from the description it was
      handed, which is what "read the pictures back" looks like from outside.

    Deliberately blunt about false positives. Tripping this costs one more
    model call and a differently-worded line; missing it costs a clip nobody
    watches to the end.
    """
    text = " ".join(script.split()).lower()
    for phrase in _ABOUT_THE_FOOTAGE:
        if phrase in text:
            return f"it talks about the footage itself ({phrase!r})"

    if visual is None:
        return None
    mine = _content_words(text)
    if len(mine) < _RECITAL_MIN_WORDS:
        return None
    theirs = _content_words(visual.as_prompt())
    if not theirs:
        return None
    if len(mine & theirs) / len(mine) >= _RECITAL_SHARE:
        return "it is the description of the pictures, read back"
    return None


_CORRECTION = """\
That attempt was rejected: {reason}

The viewer is already looking at the picture, so describing it is the one thing \
the line must not do. Write a different one that says something they cannot see \
— why it matters, what is at stake, what the question is — and open with that. \
Do not mention the video, the clip, the scene, the frames or the camera. Stay \
inside the same range of words: a shorter line is not the fix."""


def word_floor(duration_sec: float) -> int:
    """The fewest words worth speaking over a clip this long."""
    return max(6, int(max(8, int(duration_sec * _WORDS_PER_SECOND)) * _BUDGET_FLOOR))


def too_thin(script: str, floor: int) -> str | None:
    """Is there so little here that most of the clip has nothing said over it?

    The first two remakes written to the rewritten prompt came back with eight
    words on a thirty-seven second clip and thirteen on a sixteen-second one.
    Both read well. Both are two or three seconds of voice and then silence,
    which is the same dull video the rewrite was for.

    The bar is well under the floor the prompt asks for, because a line that
    ends a little early is fine and this is only meant to catch the one that
    barely starts.
    """
    words = len(script.split())
    if words >= floor * _FAR_UNDER:
        return None
    return f"it is {words} words where the clip has room for {floor}"


_TOO_SHORT = """\
That attempt was rejected: {reason}.

It reads well, and it is over long before the picture is. Write a longer line \
that covers the clip — ask a question and answer it, or take the thought one \
step further — and stay inside the range of words you were given. More about \
what you know, not more about what is on the screen, and nothing invented to \
fill the space."""


def reads_as(text: str, language: str) -> bool:
    """Is this text plausibly in that language?

    Counts function words, which is a crude identifier and a very reliable one
    at sentence length. It exists because the model is not a witness to its own
    output: handed French and asked for English, a 4B returns the French and
    reports success, and `translation_landed` only catches that when the text is
    returned *unchanged*. A tidied-up, re-punctuated French sentence passes a
    similarity check and fails a listener.

    True for any language this cannot identify, and for text too short to judge.
    The gate exists to catch a confident wrong answer, not to be a language
    detector, and refusing a clip because the vocabulary is unusual would be a
    worse failure than the one it prevents.
    """
    primary = language.strip().lower().split("-")[0]
    expected = _FUNCTION_WORDS.get(primary)
    if expected is None:
        return True

    words = [word.strip(".,!?;:\"'()[]«»—-").lower() for word in text.split()]
    words = [word for word in words if word]
    if len(words) < 6:
        return True

    hits = {
        name: sum(1 for word in words if word in members)
        for name, members in _FUNCTION_WORDS.items()
    }
    best = max(hits.values())
    if best == 0:
        # Nothing recognised in any language. Unusual vocabulary, not a wrong
        # language — and refusing here would fail a clip nobody could diagnose.
        return True
    # Ties go to the target. After the overlap is removed a tie means genuinely
    # mixed evidence, and refusing on mixed evidence would fail clips whose
    # narration is correct but full of names.
    return hits[primary] == best
