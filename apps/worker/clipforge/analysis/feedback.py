"""Deciding whether a remake's *inputs* are good enough to act on.

The layer between "what the reviewer asked for" and "what the renderer is told
to do". `clipforge.analysis.remake` reads the note; this decides whether the
result is worth producing, and says so when it is not.

It exists because of five real remakes, every one of which completed
successfully and none of which was usable:

1. A clip whose audio is music. Whisper hallucinated `"Here is a nice musical
   instrumental for you. [Instrumental music plays here] Thank you."` — a known
   failure mode on speechless audio — and a voice read it aloud, stage direction
   included.
2. A note reading *"change commentary voice to English"* produced Spanish,
   because the form's language had been left at its default and the rule at the
   time treated an untouched control as a deliberate choice.
3. Garbled French commentary — `"très mal à beaude glim cette frappe pure
   latérale gauche municois"` — translated faithfully into `"Very bad beauty
   glim this pure left lateral munitions shot"`.
4. A remake asking for English that *spoke French*. Given unpunctuated ASR text
   and asked to translate, a 4B model restores punctuation and returns the same
   language. Nothing checked, so the clip was recorded as translated.
5. Feedback typed into the script box — `"Cut the Canal plus caption or
   watermark in the upper right corner"` — narrated by a synthesiser.

The common shape is not a model being stupid. It is a pipeline with **no
predicate anywhere between a bad input and a finished artefact**: every stage
did exactly what it was told, and the thing that was missing was anyone asking
whether the input was worth speaking. So the checks here are deliberately
deterministic and outside the model. A model cannot be the thing that decides
whether to trust a model's output.

Two of these produce a refusal (the remake is still made, minus the part that
cannot be done honestly) and the rest produce a warning recorded next to the
result. Nothing here silently drops a request.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = [
    "ClipFacts",
    "Speakability",
    "is_probably_an_instruction",
    "is_probably_hallucinated",
    "speakability",
    "translation_landed",
]

# Whisper's boilerplate on speechless audio. It emits these with high confidence
# from music, crowd noise and silence — they are in its training data as
# subtitle furniture, not as anything anyone said. Matched as normalised
# substrings because the exact wording varies ("Thanks for watching!",
# "Thank you for watching.").
_HALLUCINATION_MARKERS = (
    "thanks for watching",
    "thank you for watching",
    "subscribe to",
    "subtitles by",
    "amara.org",
    "transcription by",
    "musical instrumental",
    "instrumental music",
    "music plays",
    "outro music",
    "applause",
    "foreign",
    "you you you",
)

# Bracketed stage directions: "[Instrumental music plays here]", "(applause)",
# "♪♪". Never speech, always read aloud verbatim by a synthesiser.
_STAGE_DIRECTION = re.compile(r"[\[\(][^\]\)]{0,60}[\]\)]|[♪♫]+")

# Phrasing that means the reviewer is talking TO the system rather than giving
# it words to say. The script box sits under a heading about narration and was
# still used for feedback, which is a fair thing for a person to do: the two
# boxes look alike and only one of them is obviously "for the machine".
_INSTRUCTION_OPENERS = (
    "cut ",
    "crop ",
    "remove ",
    "delete ",
    "change ",
    "make ",
    "please ",
    "can you",
    "could you",
    "try to",
    "let's ",
    "lets ",
    "i want",
    "i need",
    "add ",
    "fix ",
    "use ",
    "keep the",
    "follow the",
    "zoom ",
)
_INSTRUCTION_NOUNS = (
    "watermark",
    "caption",
    "overlay",
    "logo",
    "corner",
    "framing",
    "crop",
    "clip",
    "video",
    "voice",
    "subtitle",
)

# Below this, a "translation" is the same text with tidier punctuation.
_TRANSLATION_SIMILARITY_CEILING = 0.75

# A narration needs something to say. Six words is about two seconds of speech,
# under which the clip is better left with its own audio than given a voice
# reading a fragment.
_MIN_SPEAKABLE_WORDS = 6

# Whisper's own confidence, averaged over the window. Below this the words are
# a guess, and translating a guess and then speaking it produces the
# confident-sounding nonsense that prompted this module.
_LOW_CONFIDENCE = 0.6


@dataclass(frozen=True)
class ClipFacts:
    """What is true about the clip a reviewer is complaining about.

    Gathered by the stage before anything is decided, so the feedback layer
    reasons about the actual clip rather than about the request in the abstract.
    """

    duration_sec: float
    # The language the clip's audio is in *now*. For a derived clip that is
    # whatever its parent was re-voiced into, which is not the source's language
    # — the distinction cost a remake that went back to the French source when
    # it should have started from the Spanish its parent spoke.
    spoken_language: str | None = None
    source_language: str | None = None
    word_count: int = 0
    mean_confidence: float | None = None
    source_available: bool = False
    can_speak: bool = False
    can_rebuild_captions: bool = False
    is_derived: bool = False

    @property
    def has_speech(self) -> bool:
        return self.word_count >= _MIN_SPEAKABLE_WORDS


@dataclass
class Speakability:
    """Whether a script should be handed to a synthesiser at all."""

    ok: bool
    refusal: str | None = None
    warnings: list[str] = field(default_factory=list)


def _normalise(text: str) -> str:
    stripped = unicodedata.normalize("NFKD", text.lower())
    return " ".join(re.sub(r"[^\w\s]", " ", stripped).split())


def is_probably_hallucinated(text: str) -> bool:
    """Does this look like Whisper's boilerplate rather than something said?

    Deliberately blunt. The cost of refusing a real sentence is that the
    reviewer writes a script by hand; the cost of accepting a hallucination is a
    published clip in which a synthetic voice thanks the audience for watching
    over footage of a goal.
    """
    if not text.strip():
        return True
    if _STAGE_DIRECTION.search(text):
        return True
    normalised = _normalise(text)
    if not normalised:
        return True
    if any(marker in normalised for marker in _HALLUCINATION_MARKERS):
        return True

    # One phrase repeated to fill a silence — the other way Whisper fails on
    # speechless audio.
    words = normalised.split()
    return len(words) >= 8 and len(set(words)) <= len(words) // 3


def is_probably_an_instruction(text: str) -> bool:
    """Is this the reviewer talking to us, rather than words to be spoken?

    A script is a line of commentary. An instruction is a request about the
    clip. They are easy to tell apart and were never checked, so
    "Cut the Canal plus caption or watermark in the upper right corner" was
    synthesised into a voice track.
    """
    normalised = _normalise(text)
    if not normalised or len(normalised.split()) > 40:
        # Long text is a script; instructions are short. Past this the opener
        # heuristic starts catching real commentary that happens to begin with
        # "make" or "use".
        return False
    opens_like_one = normalised.startswith(_INSTRUCTION_OPENERS)
    mentions_the_machinery = any(noun in normalised for noun in _INSTRUCTION_NOUNS)
    return opens_like_one and mentions_the_machinery


def translation_landed(source: str, result: str, *, target_language: str) -> bool:
    """Did the translation actually change the language?

    The failure this catches is specific and was invisible: handed lowercase,
    unpunctuated ASR French and asked to translate, the local model returns the
    same French with capitals and full stops added. It is a plausible reading of
    "clean this up", it satisfies the schema, and the clip is then recorded as
    translated and speaks the wrong language.

    Compared on normalised text so punctuation and case — the only things that
    changed in the observed failure — cannot make a no-op look like work.
    """
    del target_language  # named for the caller's log line, not used in the test
    a, b = _normalise(source), _normalise(result)
    if not b:
        return False
    if a == b:
        return False
    return SequenceMatcher(None, a, b).ratio() < _TRANSLATION_SIMILARITY_CEILING


def speakability(script: str, facts: ClipFacts, *, from_transcript: bool) -> Speakability:
    """Decide whether this text should become a voice track.

    `from_transcript` separates the two cases that deserve different treatment.
    Words lifted from the clip's own audio are the machine's guess and are held
    to every check here. A script the reviewer typed is their own words, so only
    the checks that protect *them* apply — chiefly "this looks like you were
    talking to me rather than writing narration".
    """
    text = " ".join(script.split())
    if not text:
        return Speakability(
            ok=False,
            refusal=(
                "There is nothing to say: this clip has no transcribed speech and no script "
                "was written. Write the line you want spoken, or leave the clip's own audio."
            ),
        )

    if is_probably_an_instruction(text):
        return Speakability(
            ok=False,
            refusal=(
                f"The script reads like an instruction rather than narration ({text[:80]!r}), "
                "so it was not spoken. If you meant it as feedback, put it in the notes box; "
                "if you really want it read aloud, reword it as the line to say."
            ),
        )

    warnings: list[str] = []

    if from_transcript:
        if is_probably_hallucinated(text):
            return Speakability(
                ok=False,
                refusal=(
                    "The transcribed words for this clip look like the recogniser's "
                    f"boilerplate rather than speech ({text[:60]!r}) — which is what it "
                    "produces from music or crowd noise. Nothing was narrated. Write a "
                    "script if you want a voice on this one."
                ),
            )
        if len(text.split()) < _MIN_SPEAKABLE_WORDS:
            return Speakability(
                ok=False,
                refusal=(
                    f"Only {len(text.split())} words were transcribed in this window, which is "
                    "not enough to narrate. Write a script, or keep the clip's own audio."
                ),
            )
        if facts.mean_confidence is not None and facts.mean_confidence < _LOW_CONFIDENCE:
            warnings.append(
                f"the recogniser was unsure of these words ({facts.mean_confidence:.0%} "
                "confidence), so the narration may not match what was actually said"
            )

    return Speakability(ok=True, warnings=warnings)
