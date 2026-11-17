"""Asking the local model to write a song: a title and lyrics the music model can sing.

The words come from one place (``GenerateMusicOptions.lyricsFrom``): a trend
the research run found, a person's notes, or a one-line idea. The model turns
that into a song shaped the way ACE-Step sings best (its own guide,
``packages/ngoma/vendor/ACE-Step-1.5/docs/en/Tutorial.md``, "About Lyrics"):
section tags in brackets, six to ten syllables a line, lines in the same place
the same length, one image per song, a chorus that repeats.

What a song may not do, because it goes out under the channel's name: name or
describe a real person, a company, a brand or another song; take a side in
politics; make light of anyone's loss. A trend is mostly news about named
people, so a trend song is about the feeling under the headline, never the
headline. Every name in the trend is checked for in what comes back, and a
song that still carries one is asked for again once and then refused.

A notes song is the other way round: it must teach what the notes say, and
only that. Accuracy over rhyme.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from clipforge_contracts import GenerateMusicOptions, LyricsSource, Trend
from pydantic import BaseModel, Field

from clipforge.models.ollama import OllamaClient

__all__ = [
    "LYRICS_PROMPT_VERSION",
    "LyricsError",
    "SongBrief",
    "SongDraft",
    "brief_for",
    "condense_notes",
    "names_in",
    "normalise_key",
    "points_for",
    "series_for",
    "tidy_lyrics",
    "write_song",
]

LYRICS_PROMPT_VERSION = "lyrics-v3"


class LyricsError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = "LYRICS"


class SongDraft(BaseModel):
    """What the model returns. Passed to Ollama as the format constraint."""

    title: str = Field(..., min_length=1, max_length=60)
    theme: str = Field(
        ..., max_length=200, description="The one image the song is built on, in a sentence."
    )
    mood: str = Field(
        ..., max_length=80, description="Three or four words for how it should feel and be sung."
    )
    bpm: int = Field(..., ge=60, le=180)
    key: str = Field(..., max_length=20, description="e.g. 'A minor', 'C major'.")
    lyrics: str = Field(..., min_length=20, max_length=4000)


@dataclass(frozen=True)
class SongBrief:
    """What the model is told, and what the track will say it came from."""

    source: LyricsSource
    material: str
    inspiration: str
    avoid: tuple[str, ...] = field(default_factory=tuple)


SYSTEM_PROMPT = """You write songs for a music channel on YouTube. A music model will sing exactly what you write, so the shape matters as much as the words.

Shape:
- Section tags on their own line in square brackets: [Intro], [Verse 1], [Pre-Chorus], [Chorus], [Verse 2], [Bridge], [Final Chorus], [Outro]. At most one modifier, e.g. [Chorus - anthemic] or [Bridge - soft].
- A blank line between sections. Four lines in a verse, four or five in a chorus, two in a pre-chorus or bridge.
- Six to ten syllables a line. Lines in the same place in each verse are about the same length.
- The chorus has one hook line that repeats and is easy to sing along to. Repeat the chorus after each verse, word for word.
- Short lines: most lines are five to eight words. A line that needs two breaths is two lines.
- [Intro] and [Outro] have no words, or one short line.
- Background vocals go in parentheses, sparingly, and only as words someone sings: (oh oh), (together).
- Spell out an abbreviation with spaces between its letters, as it is sung: A T P, D N A.
- Every line is sung. Never write the tempo, the key, the genre or an instrument in the lyrics; never write directions like (fade away) or (guitar).
- No emojis, no notes to the singer outside the tags.

Content:
- One central image, explored from several sides. No pile of vague pictures (neon, endless, electric).
- Concrete, everyday detail beats abstraction. It should sound like a person wrote it.
- Never name or describe a real person, politician, company, brand, product, artist or another song. Never quote a song.
- No sides in politics. No jokes about anyone's loss, death, illness or disaster; if the source is a sad event, write about what people hold on to, without its details.
- English.

Title: two to five words, the hook if it can be."""

TREND_TASK = """Write a song inspired by something people are talking about right now. Do NOT retell the news and do NOT use any name from it: find the human feeling underneath (pressure, hope, change, being heard, holding on) and build the song on that.

What is being talked about:
{material}"""

NOTES_TASK = """Write a study song that teaches the notes below, so a student who sings it remembers them.

Accuracy comes before rhyme. Every factual line restates something the notes say, in simpler words, with the notes' own terms; add no fact, example, number or explanation that is not in the notes. If a rhyme would bend a fact, drop the rhyme. A formula is sung as words (F equals m a).

The chorus states the single most important idea. Each verse covers one part of the notes, in their order, and together the verses cover all of them.

The notes:
{material}"""

IDEA_TASK = """Write a song from this idea:
{material}"""

STRUCTURES = {
    "short": "[Verse 1], [Chorus], [Verse 2], [Chorus]",
    "medium": "[Intro], [Verse 1], [Pre-Chorus], [Chorus], [Verse 2], [Chorus], [Outro]",
    "long": "[Intro], [Verse 1], [Pre-Chorus], [Chorus], [Verse 2], [Pre-Chorus], [Chorus], [Bridge], [Final Chorus], [Outro]",
}

# Capitalised words that are not names, so a headline in Title Case does not
# forbid half the language.
_COMMON = frozenset(
    [
        "a",
        "about",
        "after",
        "against",
        "all",
        "an",
        "and",
        "are",
        "as",
        "at",
        "back",
        "be",
        "been",
        "before",
        "but",
        "by",
        "can",
        "could",
        "day",
        "did",
        "do",
        "does",
        "down",
        "for",
        "from",
        "get",
        "gets",
        "got",
        "had",
        "has",
        "have",
        "he",
        "her",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "like",
        "make",
        "man",
        "more",
        "most",
        "my",
        "new",
        "no",
        "not",
        "now",
        "of",
        "off",
        "on",
        "one",
        "only",
        "or",
        "our",
        "out",
        "over",
        "people",
        "says",
        "said",
        "she",
        "so",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "too",
        "up",
        "us",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "without",
        "would",
        "year",
        "years",
        "you",
        "your",
        "why",
        "video",
        "watch",
        "live",
        "breaking",
        "update",
        "news",
        "new",
        "first",
        "last",
        "best",
        "week",
        "today",
        "inside",
        "moment",
        "shows",
        "show",
        "calls",
        "call",
        "says",
        "say",
        "back",
        "must",
        "because",
        "over",
        "open",
        "opens",
    ]
)


def names_in(*texts: str) -> tuple[str, ...]:
    """Words in a trend that could be a name: capitalised or all capitals, not common."""
    found: dict[str, None] = {}
    for text in texts:
        for word in re.findall(r"[A-Za-z][A-Za-z'\-]+", text or ""):
            bare = word.strip("'-").removesuffix("'s")
            if len(bare) < 3 or not (bare[0].isupper() or bare.isupper()):
                continue
            if bare.lower() in _COMMON:
                continue
            found.setdefault(bare, None)
    return tuple(found)[:40]


def brief_for(
    options: GenerateMusicOptions,
    trend: Trend | None = None,
    *,
    notes_text: str | None = None,
    key_points: list[str] | None = None,
    file_name: str | None = None,
) -> SongBrief:
    source = options.lyrics_from
    if source is LyricsSource.TREND:
        if trend is None:
            raise LyricsError("the trend this song is about is not there any more")
        headlines = [video.title for video in trend.videos[:6]]
        details = [signal.detail for signal in trend.signals[:6] if signal.detail]
        lines = [f"Topic: {trend.topic}"]
        if trend.angle:
            lines.append(f"The angle: {trend.angle}")
        lines += [f"- {text}" for text in (*details, *headlines)]
        return SongBrief(
            source=source,
            material="\n".join(lines)[:2500],
            inspiration=f"Trend: {trend.topic}"[:200],
            avoid=names_in(trend.topic, *headlines, *details),
        )
    if source is LyricsSource.NOTES:
        notes = "\n\n".join(t.strip() for t in (notes_text, options.notes) if t and t.strip())
        if not notes:
            raise LyricsError("a song from notes needs the notes")
        first = next((line.strip() for line in notes.splitlines() if line.strip()), "")
        label = file_name or first
        if key_points:
            material = "Key points of the notes, in their order:\n" + "\n".join(
                f"- {point}" for point in key_points
            )
        else:
            material = notes[:DIRECT_NOTES_CHARS]
        if options.focus:
            material = f"Focus on: {options.focus}\n\n{material}"
        return SongBrief(source=source, material=material, inspiration=f"Your notes: {label}"[:200])
    if source is LyricsSource.IDEA:
        idea = (options.idea or "").strip()
        if not idea:
            raise LyricsError("a song from an idea needs the idea")
        return SongBrief(source=source, material=idea[:300], inspiration=f"Idea: {idea}"[:200])
    raise LyricsError("an instrumental has no lyrics to write")


# ── Long notes: the key points first ─────────────────────────────────────────
#
# A lecture is longer than the model reads well at once (16k tokens, and a 4B
# model loses the middle of a long prompt well before that), and a song can
# only teach a dozen things anyway. So notes up to DIRECT_NOTES_CHARS go to
# the writer as they are; longer ones are read in pieces, each piece gives its
# key facts, and those are cut down to what a song of this length can carry.
# Nothing is skipped: every piece is read, however long the notes are.

DIRECT_NOTES_CHARS = 9000
CHUNK_CHARS = 9000


class KeyPoints(BaseModel):
    """What one piece of the notes says that matters. The format constraint."""

    points: list[str] = Field(..., max_length=14)


EXTRACT_SYSTEM = """You pull the key facts out of a student's notes, so they can be turned into a study song.

Each point is one short, plain sentence in the notes' own terms: a definition, a law, a step, a formula written as words, a cause and its effect, a date that matters. Keep the order of the notes. Leave out headers, page and slide numbers, references, admin (deadlines, reading lists) and anything repeated. Add nothing that is not in the text."""

SELECT_SYSTEM = """You choose what a study song will teach from a list of key facts taken from a student's notes.

Keep the most important facts: the ones an exam would ask about and the ones the others depend on. Keep their order. Merge two points that say the same thing. Rewrite nothing into a new claim; shorten only. A point must teach something: never return the topic or the focus itself as a point."""


def points_for(seconds: int) -> int:
    """How many facts a song of this length can teach."""
    if seconds <= 75:
        return 6
    if seconds <= 140:
        return 10
    return 14


def condense_notes(
    client: OllamaClient,
    text: str,
    *,
    focus: str | None,
    seconds: int,
    progress: Callable[[str], None] = lambda _note: None,
) -> list[str]:
    """The key points of long notes, or [] when they are short enough to use as they are."""
    from clipforge.synth.notes import chunks

    if len(text) <= DIRECT_NOTES_CHARS:
        return []
    focus_line = (
        f"\nOnly facts about: {focus}. A part with nothing about it gives no points."
        if focus
        else ""
    )
    pieces = list(chunks(text, CHUNK_CHARS))
    points: list[str] = []
    for number, piece in enumerate(pieces, start=1):
        progress(f"Reading your notes: part {number} of {len(pieces)}")
        found = client.generate_structured(
            schema_model=KeyPoints,
            system=EXTRACT_SYSTEM,
            prompt=f"The notes, part {number} of {len(pieces)}:{focus_line}\n\n{piece}",
            temperature=0.0,
        )
        points += [p.strip() for p in found.points if p.strip()]

    keep = points_for(seconds)
    # Very long notes give more points than one prompt holds well: cut them down
    # in rounds until they fit, then choose.
    while len("\n".join(points)) > DIRECT_NOTES_CHARS:
        progress("Picking out the most important points")
        batches = list(chunks("\n\n".join(points), CHUNK_CHARS))
        reduced: list[str] = []
        for batch in batches:
            reduced += _select(client, batch.split("\n\n"), keep, focus)
        if len(reduced) >= len(points):
            break
        points = reduced
    if len(points) > keep:
        progress("Picking out the most important points")
        points = _select(client, points, keep, focus)
    if not points:
        raise LyricsError(
            "nothing in the notes could be taught"
            + (f" about {focus!r}" if focus else "")
            + "; check the file has text in it"
        )
    return points


def _select(client: OllamaClient, points: list[str], keep: int, focus: str | None) -> list[str]:
    focus_line = f"\nThe song is about: {focus}." if focus else ""
    listed = "\n".join(f"- {point}" for point in points)
    chosen = client.generate_structured(
        schema_model=KeyPoints,
        system=SELECT_SYSTEM,
        prompt=f"Choose at most {keep} of these, in order.{focus_line}\n\n{listed}",
        temperature=0.0,
    )
    return [p.strip() for p in chosen.points if p.strip()][:keep]


def _structure(seconds: int) -> str:
    if seconds <= 75:
        return STRUCTURES["short"]
    if seconds <= 140:
        return STRUCTURES["medium"]
    return STRUCTURES["long"]


def build_prompt(
    brief: SongBrief,
    options: GenerateMusicOptions,
    avoid: tuple[str, ...] = (),
    refused: tuple[str, ...] = (),
) -> str:
    task = {LyricsSource.TREND: TREND_TASK, LyricsSource.NOTES: NOTES_TASK}.get(
        brief.source, IDEA_TASK
    )
    seconds = options.seconds or 150
    parts = [
        task.format(material=brief.material),
        "",
        f"The music: {options.style}.",
        f"It lasts about {seconds} seconds. Use these sections, in order: {_structure(seconds)}.",
        "Pick a tempo (bpm) and a key that suit the music.",
    ]
    if avoid:
        parts.append("Do not use any of these words anywhere: " + ", ".join(avoid) + ".")
    if refused:
        parts.append(
            "A first attempt was refused because "
            + "; ".join(refused)
            + ". Write it again without that."
        )
    return "\n".join(parts)


def tidy_lyrics(text: str) -> str:
    """One blank line between sections, no trailing spaces, tags on their own lines."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if out and out[-1] != "":
                out.append("")
            out.append(stripped)
        elif stripped and not any(char.isalpha() for char in stripped):
            continue  # a stray brace or ellipsis, nothing to sing
        elif stripped:
            out.append(stripped)
        elif out and out[-1] != "":
            out.append("")
    return "\n".join(out).strip()[:4096]


def _check(draft: SongDraft) -> list[str]:
    problems = []
    tags = re.findall(r"^\[([^\]]+)\]$", draft.lyrics, flags=re.MULTILINE)
    if not any(tag.lower().startswith("chorus") or "chorus" in tag.lower() for tag in tags):
        problems.append("it has no [Chorus]")
    if not any(tag.lower().startswith("verse") for tag in tags):
        problems.append("it has no [Verse]")
    sung = [line for line in draft.lyrics.splitlines() if line.strip() and not line.startswith("[")]
    if len(sung) < 8:
        problems.append("it has fewer than eight sung lines")
    long_lines = [line for line in sung if len(line.split()) > LONGEST_LINE_WORDS]
    if len(long_lines) * 3 > len(sung):
        problems.append(f"too many lines are longer than {LONGEST_LINE_WORDS} words")
    if any(_DIRECTION.search(line) for line in sung):
        problems.append("it sings the tempo, the key or an instrument, which are not lyrics")
    return problems


# More than this many words is not one sung line; a third of them over it is a
# song written as prose.
LONGEST_LINE_WORDS = 10
# What a model writes into lyrics that is not meant to be sung.
_DIRECTION = re.compile(
    r"\b(bpm|tempo|[A-G][#b]? (major|minor)|fade (out|away)|instrumental|guitar solo|drum break)\b",
    re.IGNORECASE,
)


def _named(draft: SongDraft, avoid: tuple[str, ...]) -> list[str]:
    text = f"{draft.title}\n{draft.lyrics}"
    return [
        name for name in avoid if re.search(rf"\b{re.escape(name)}\b", text, flags=re.IGNORECASE)
    ]


def write_song(
    client: OllamaClient, brief: SongBrief, options: GenerateMusicOptions, *, seed: int = 0
) -> SongDraft:
    """Ask for the song; ask again once if it is malformed or names a name. Raises LyricsError."""
    avoid = brief.avoid
    extra: tuple[str, ...] = ()
    last: list[str] = []
    # Sampled: a song from greedy decoding is the same song every time. Cooler
    # for notes, where a wrong fact costs more than a dull line.
    temperature = 0.4 if brief.source is LyricsSource.NOTES else 0.8
    for attempt in range(2):
        draft = client.generate_structured(
            schema_model=SongDraft,
            system=SYSTEM_PROMPT,
            prompt=build_prompt(brief, options, avoid=extra, refused=tuple(last)),
            temperature=temperature,
            seed=seed + attempt,
        )
        draft = draft.model_copy(
            update={"lyrics": tidy_lyrics(draft.lyrics), "title": draft.title.strip()}
        )
        named = _named(draft, avoid)
        last = _check(draft) + [f"it uses {name!r}, a name from the trend" for name in named]
        if not last:
            return draft
        # Long lines are a worse song, not a wrong one: the second time, they pass.
        if attempt == 1 and all(problem.startswith("too many lines") for problem in last):
            return draft
        extra = tuple(named) or extra
    raise LyricsError("the model could not write a usable song: " + "; ".join(last))


_KEY = re.compile(r"^([A-Ga-g])\s*(#|b|♯|♭)?\s*(major|minor|maj|min|m)?$", re.IGNORECASE)


def normalise_key(key: str | None) -> str | None:
    """'a minor' -> 'A minor', 'F#m' -> 'F# minor'; anything else -> None (the model decides)."""
    match = _KEY.match((key or "").strip())
    if not match:
        return None
    note, accidental, mode = match.groups()
    accidental = {"♯": "#", "♭": "b"}.get(accidental or "", accidental or "")
    quality = "minor" if (mode or "").lower() in ("minor", "min", "m") else "major"
    return f"{note.upper()}{accidental} {quality}"


def series_for(options: GenerateMusicOptions) -> str:
    """The series a new track joins: the one asked for, else one named from the style."""
    if options.series:
        return options.series.strip()[:60]
    if options.lyrics_from is LyricsSource.NOTES:
        return "Study songs"
    first = options.style.split(",")[0].strip() or "Music"
    return first[:1].upper() + first[1:60]
