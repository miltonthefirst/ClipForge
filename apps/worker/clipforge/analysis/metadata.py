"""Naming a clip, describing it, and tagging it — for the feed it lands in.

## What was there before

Nothing that was written to be read. `Clip.title` was `Candidate.hook`, which
the analysis prompt defines as *"the actual opening line, quoted from the
transcript"*, so a real clip reached the review queue titled:

    "on a franchi un premier rideau kane peut enroulé du plat"

Lowercase, mid-sentence, in French, on a clip that had just been re-voiced into
English. `Clip.description` was `Candidate.reason`, which is one sentence on why
the window was *selected*:

    This segment captures the high-tension moment where Kane breaks through
    the defense, creating an immediate visual hook for football fans.

That is a note from an analyst to a pipeline. It was being shown to viewers.

Both fields were honest about what they held. They were simply never the thing
their names implied, and nothing downstream knew that.

## What this writes instead

A title, a description and tags, in the clip's language, grounded in what the
clip actually shows (`clipforge.media.vision`) and what it says. The rules
encoded in the prompt are the ones that decide whether a short is seen at all:
the hook lives in the first few words because that is all a feed shows before it
truncates, the first line of the description is the only line most viewers read,
and tags carry the searchable nouns that the title had no room for.

## Why the language comes from the caller

Because it changes. A clip re-voiced into English whose title is still French is
not a clip with a small inconsistency; it is a clip that will be shown to an
English-speaking feed under a title nobody there can read. So the language is an
argument, and `REMAKE` passes the language it just narrated in.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from clipforge_contracts import LlmClipMetadata

from clipforge.media.vision import VisualContext
from clipforge.models.ollama import OllamaClient, OllamaError
from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = [
    "METADATA_SYSTEM_PROMPT",
    "ClipMetadata",
    "build_metadata_prompt",
    "ungrounded_terms",
    "write_metadata",
]

# YouTube's own ceiling is 100, and a Shorts feed shows far less than that before
# it truncates. The cap here is the contract's; the prompt asks for much shorter.
TITLE_MAX = 100
DESCRIPTION_MAX = 900
MAX_TAGS = 12

# A tag is a search term, not a sentence. Anything longer is a description that
# has wandered into the wrong field, and it crowds out the terms that work.
_TAG_MAX_WORDS = 4
_TAG_MAX_CHARS = 40

# Words that are true of a clip whatever else is, in the languages a voice
# exists for. These pass the grounding check without appearing in the material,
# because the material is in the SOURCE's language and the tags are in the
# viewer's: a Spanish clip cut from French football says "fútbol" nowhere, and
# refusing the tag on that basis left two tags on a clip that deserved eight.
#
# It holds no competition or club names, on purpose. A competition is the thing
# that gets invented — `laliga` and `champions league` are equally fluent
# guesses and only one of them was right — and a list of leagues here would
# launder exactly the fabrication the check exists to catch.
_GENERIC = frozenset(
    [
        "football",
        "soccer",
        "futbol",
        "futebol",
        "fussball",
        "calcio",
        "voetbal",
        "sport",
        "sports",
        "deporte",
        "deportes",
        "esporte",
        "sportivo",
        "highlights",
        "highlight",
        "resumen",
        "resumo",
        "momentos",
        "destacados",
        "faits",
        "marquants",
        "goal",
        "goals",
        "gol",
        "goles",
        "golo",
        "but",
        "buts",
        "tor",
        "tore",
        "match",
        "matchday",
        "partido",
        "partita",
        "jogo",
        "spiel",
        "rencontre",
        "skills",
        "skill",
        "technique",
        "tecnica",
        "habilidades",
        "shorts",
        "short",
        "clip",
        "clips",
        "video",
        "videos",
        "reel",
        "reels",
        "viral",
        "trending",
        "play",
        "plays",
        "action",
        "accion",
        "azione",
        "jogada",
        "save",
        "saves",
        "parada",
        "parade",
        "pass",
        "passes",
        "pase",
        "passe",
        "shot",
        "shots",
        "tiro",
        "tir",
        "tiros",
        "foot",
        "left",
        "right",
        "pie",
        "pied",
        "piede",
        "stadium",
        "estadio",
        "stade",
        "stadio",
        "fans",
        "crowd",
        "hincha",
        "tifosi",
    ]
)


@dataclass(frozen=True)
class ClipMetadata:
    """What goes out with the clip."""

    title: str
    description: str
    tags: list[str]
    language: str
    # Names in the title or description that appear nowhere in the material the
    # model was shown. Not removed — prose cannot be filtered the way a tag list
    # can without leaving holes in sentences — but surfaced, so a reviewer
    # reading "UEFA Europa League" on a Champions League tie is told which words
    # nothing stood behind.
    unverified: list[str]


def write_metadata(
    client: OllamaClient,
    *,
    language: str,
    spoken_text: str,
    duration_sec: float,
    visual: VisualContext | None,
    source_title: str | None = None,
) -> ClipMetadata | None:
    """Write the three fields. Returns None when the model cannot be reached.

    None rather than raising: a clip with a poor title is worth far more than no
    clip, so every caller keeps what it had and carries on.
    """
    try:
        answer = client.generate_structured(
            schema_model=LlmClipMetadata,
            system=METADATA_SYSTEM_PROMPT,
            prompt=build_metadata_prompt(
                language=language,
                spoken_text=spoken_text,
                duration_sec=duration_sec,
                visual=visual,
                source_title=source_title,
            ),
            # Titles are a creative act in a way selection is not, and at
            # temperature 0 a small model reliably produces the same flat
            # "Football Match Highlights" for every clip of a match.
            temperature=0.4,
        )
    except OllamaError as exc:
        log.warning("metadata.unavailable", error=str(exc))
        return None

    title = _tidy_title(answer.title or "")
    # Newlines survive, unlike everywhere else here: the blank line before the
    # hashtags is the whole reason a description reads as a description rather
    # than as one long paragraph nobody finishes.
    description = (answer.description or "").strip()[:DESCRIPTION_MAX]
    # Everything the model was shown, as one string, so a tag can be checked
    # against it. Built from the same pieces the prompt was built from, because
    # anything else would be grading against material the model never saw.
    known = "\n".join(
        part
        for part in (
            source_title or "",
            visual.subject if visual else "",
            visual.happens if visual else "",
            " ".join(visual.on_screen_text) if visual else "",
            spoken_text,
        )
        if part
    )
    tags = _tidy_tags(answer.tags or [], known=known)

    if not title:
        return None

    unverified = ungrounded_terms(f"{title}\n{description}", known)
    if unverified:
        log.info("metadata.unverified", terms=unverified)
    log.info("metadata.written", language=language, title=title[:60], tags=len(tags))
    return ClipMetadata(
        title=title,
        description=description,
        tags=tags,
        language=language,
        unverified=unverified,
    )


METADATA_SYSTEM_PROMPT = """\
You write the title, description and tags for one short vertical video, for a \
feed like YouTube Shorts, TikTok or Reels.

Your job is to get it watched. That is not the same as describing it accurately \
to someone who has already watched it, and it is the opposite of explaining why \
it was selected. Never write like an analyst: no "this segment captures", no \
"this clip works because", no "demonstrates", no "showcases", no "highlights \
the moment where".

**Title.** Put the hook in the first four or five words, because that is all a \
feed shows before it cuts the rest off. Name the thing people search for — the \
team, the player, the competition, the place — as early as you can. Keep it \
under about sixty characters. Sentence case, not Title Case and not caps. No \
quotation marks around it. Do not promise something the clip does not contain.

**Description.** The first line is the only line most people see, so it carries \
the hook again in slightly different words. Then one or two short sentences of \
context, using the words someone would type into a search box. Then a blank \
line, and then three to six hashtags on a line of their own. The hashtags are \
not optional and they are not decoration — they are how the post gets filed.

**Tags.** Lowercase search terms, eight to twelve of them. Build them from the \
names you were actually given: one for each team, one for each person named, \
one for the place if you know it — and then the sport, the format and the \
action ("football", "highlights", "free kick"). These are where the nouns go \
that the title had no room for, so use the ones in front of you rather than \
reaching for ones you half-remember.

Write everything in the target language, including the hashtags.

**Every proper noun must come from what you were given.** Clubs, players, \
competitions, stadiums and cities are in the material above or they do not go \
in. This is the hardest rule to keep and the one that matters most: an invented \
competition is not a small inaccuracy, it is a search term that matches nothing \
and a viewer who stops trusting the channel.

**Never expand an abbreviation.** If the material says "LDC" or "BOD", those \
are the letters you have. Guessing what they stand for is how a Champions \
League tie gets described as the Europa League — which happened, in exactly \
those words. If you are not certain which competition or which ground this is, \
do not mention one at all. "Football" and "highlights" cost you nothing.

A dull true title beats an exciting invented one, every time."""


def build_metadata_prompt(
    *,
    language: str,
    spoken_text: str,
    duration_sec: float,
    visual: VisualContext | None,
    source_title: str | None = None,
) -> str:
    lines = [f"Target language: {language}", f"Length: {duration_sec:.0f} seconds"]
    if source_title:
        lines.append(f"Cut from a video called: {source_title}")
    if visual is not None:
        lines += ["", visual.as_prompt()]
    if spoken_text.strip():
        lines += ["", "What the clip says:", spoken_text.strip()[:1200]]
    if visual is None and not spoken_text.strip():
        lines += [
            "",
            "Nothing is known about this clip beyond the title of the video it "
            "came from. Write something honest and general rather than inventing "
            "detail.",
        ]
    lines += ["", f"Write the title, description and tags in {language}."]
    return "\n".join(lines)


def ungrounded_terms(text: str, known: str) -> list[str]:
    """Capitalised words in the prose that appear nowhere in the material.

    A tag list can be filtered; a sentence cannot, not without leaving a hole
    where a clause used to be. So the description is published as written and
    the words nothing stood behind are reported alongside it.

    Capitalisation is the signal, and it is a weak one — it misses a lowercase
    invention and flags a proper noun the model spelled differently. It is used
    anyway because the failure it catches is the expensive one: "UEFA Europa
    League" on a Champions League tie, written because the material said "LDC"
    and the model expanded it. That word was capitalised, and it was wrong.

    Words that open a sentence are skipped, because their capital says nothing.
    """
    vocabulary = {word for word in re.findall(r"[^\W\d_]+", known.lower()) if len(word) > 2}
    if not vocabulary:
        return []

    found: list[str] = []
    seen: set[str] = set()
    for sentence in re.split(r"[.!?\n]+", text):
        words = re.findall(r"[^\W\d_]+", sentence)
        for index, word in enumerate(words):
            if index == 0 or len(word) < 4 or not word[0].isupper():
                continue
            lowered = word.lower()
            if lowered in vocabulary or lowered in _GENERIC or lowered in seen:
                continue
            seen.add(lowered)
            found.append(word)
    return found[:8]


def _tidy_title(raw: str) -> str:
    """Strip what a model wraps a title in, and cap it at the contract's limit.

    Quotation marks around the whole thing are the common one, and they are not
    cosmetic: a title that arrives quoted is published quoted.
    """
    title = " ".join(raw.split()).strip()
    while len(title) >= 2 and title[0] in "\"'“”«" and title[-1] in "\"'“”»":
        title = title[1:-1].strip()
    title = re.sub(r"^(title|titre|título|titolo)\s*[:\-]\s*", "", title, flags=re.IGNORECASE)
    return title[:TITLE_MAX].strip()


def _tidy_tags(raw: Sequence[object], *, known: str = "") -> list[str]:
    """Lowercase, de-hashed, de-duplicated, capped — and grounded.

    Deduplication is case- and hash-insensitive because a model asked for tags
    returns "Bayern", "bayern" and "#bayern" in the same list, and three of the
    twelve slots are then one tag.

    **Grounding is the part that earns its place.** Asked for tags on a
    Champions League tie, qwen3.5:4b returned `laliga`, `bayer leipzig`,
    `bayer leversen`, `lck` and `thiago diaz` — a league that is not this one,
    two clubs that do not exist, an esports competition and a player who is not
    playing. The prompt forbids every one of them in as many words. Prompts do
    not fix this class of problem; they never have.

    So **every word of a tag must be either grounded or generic.** Grounded
    means it appears in the material the model was given: the source's title,
    what the pictures showed, the text read off the screen, and what the clip
    says. Generic means it is one of the sport-and-format words below, which are
    true of the clip whatever else is: nobody is misled by `football`.

    Requiring *every* word rather than *any* is what stops the plausible
    half-truth. `liverpool vs bayern` and `diaz real madrid` each contain one
    grounded word and name a fixture that did not happen; under an any-word rule
    both survive, and a tag that names the wrong match is worse than no tag.
    The price is `harry kane` on a broadcast that only ever says "Kane" — and
    `kane` survives on its own, so the cost is a duplicate rather than a loss.

    The generic list is short and deliberately holds no competition names. A
    competition is exactly what gets invented: `laliga` and `champions league`
    are equally fluent guesses, one of them was right, and nothing here can tell
    which without being told.

    With nothing known, nothing is filtered: no material means no evidence, and
    refusing every tag would be a worse answer than an unverified one.
    """
    vocabulary = {word for word in re.findall(r"[^\W\d_]+", known.lower()) if len(word) > 2}

    seen: set[str] = set()
    tags: list[str] = []
    for item in raw:
        text = str(getattr(item, "root", item)).strip().lstrip("#").lower()
        text = " ".join(text.split())
        if not text or len(text) > _TAG_MAX_CHARS or len(text.split()) > _TAG_MAX_WORDS:
            continue
        if text in seen:
            continue
        words = re.findall(r"[^\W\d_]+", text)
        if vocabulary and not all(
            word in vocabulary or word in _GENERIC or len(word) <= 2 for word in words
        ):
            log.info("metadata.tag_ungrounded", tag=text)
            continue
        seen.add(text)
        tags.append(text)
        if len(tags) >= MAX_TAGS:
            break
    return tags
