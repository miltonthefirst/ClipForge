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

## What still refuses

Grounding is not a guarantee, so `clipforge.analysis.feedback` keeps every gate
it had, and this module adds one: the script must actually be in the language
that was asked for. That is checked by counting function words rather than by
asking the model, because a model that has just produced French will happily
report that it produced English.
"""

from __future__ import annotations

from dataclasses import dataclass

from clipforge_contracts import LlmNarration

from clipforge.media.vision import VisualContext
from clipforge.models.ollama import OllamaClient, OllamaError
from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = [
    "NARRATE_SYSTEM_PROMPT",
    "Narration",
    "build_narration_prompt",
    "reads_as",
    "write_narration",
]

# Roughly how many words a synthesiser gets through in a second of clip at a
# natural pace. Used to give the model a budget rather than a vague "keep it
# short": narration that overruns the picture is the single most common way a
# re-voiced clip goes wrong, and it is invisible until someone watches the end.
_WORDS_PER_SECOND = 2.4

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
    try:
        answer = client.generate_structured(
            schema_model=LlmNarration,
            system=NARRATE_SYSTEM_PROMPT,
            prompt=build_narration_prompt(
                transcript=transcript,
                target_language=target_language,
                duration_sec=duration_sec,
                visual=visual,
                source_title=source_title,
            ),
            # A little sampling. Writing one sentence has more than one right
            # answer, and at temperature 0 a small model locks onto the
            # transcript's own wording even when it has just said it is unusable.
            temperature=0.3,
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
        grounded=visual is not None,
        words=len(script.split()),
    )
    return Narration(
        script=script,
        from_transcript=bool(answer.transcript_usable),
        reasoning=(answer.reasoning or "").strip(),
        grounded=visual is not None,
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

Then write the line.

- If the transcription is usable, carry its content across into the target \
language, correcting names and terms using what is visible. A commentator saying \
a player's name badly transcribed is still that player.
- If it is not usable, write from the pictures instead. Describe what is \
happening as a commentator would, and say only what the pictures support.

Rules that hold either way:

- Write ONLY in the target language. Not a word of the original.
- Never invent a scoreline, a minute, a competition or a name that is not in the \
transcription or visible on screen. If you do not know who scored, say what \
happened without naming anyone. Do not expand an abbreviation on the score bug \
into a club name you are guessing at: "BOD" is "BOD".
- **Never assert an outcome.** Do not say a goal was scored, a save was made, a \
card was shown or a point was won unless the transcription says so or it is \
written on screen. A few stills are evidence that something was attempted, never \
that it succeeded: the frames stop, the move does not.
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
        f"Write at most {budget} words, in {target_language}.",
    ]
    return "\n".join(lines)


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
