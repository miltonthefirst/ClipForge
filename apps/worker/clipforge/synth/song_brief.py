"""Reading a pasted brief: a song described in someone's own words (docs/MUSIC-PLAN.md, M6).

A person who is not a musician asks a chat assistant for a song and pastes the
answer: usually a title, a "style" line, lyrics with [Verse] and [Chorus]
markers, and a note or two on the voice, the length or the video. A BRIEF song
is made from exactly that, with none of the form's choices.

Two parts, because they fail differently:

- **The lyrics are found by rule, never by the model.** A small model asked to
  copy lyrics rewrites them. So the words are taken from the first section
  marker (or a "Lyrics" heading) to where the song stops, markers are put in
  the music model's form (``**Verse 1:**`` -> ``[Verse 1]``), a "repeat the
  chorus" line becomes the chorus again, and directions like ``(guitar solo)``
  are dropped. Every sung word is the person's.
- **The rest is read by the local model** into a ``BriefSpec``: the sound in
  words (an artist's name becomes what that artist sounds like), the voice,
  the language, the tempo, the key, a length if one is asked for, a title, and
  the background scene. Anything the brief does not say, the model chooses to
  suit what it does say.

With no lyrics in the brief, the writer in clipforge/synth/lyrics.py writes
them from the brief, under its usual rules.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from clipforge.models.ollama import OllamaClient
from clipforge.synth.lyrics import ArtStyle, tidy_lyrics

__all__ = [
    "SONG_BRIEF_PROMPT_VERSION",
    "BriefSpec",
    "decode_brief",
    "decode_reference",
    "find_lyrics",
    "language_code",
    "voice_caption",
]

SONG_BRIEF_PROMPT_VERSION = "song-brief-v1"

Vocal = Literal["female", "male", "duet", "choir", "instrumental", "any"]


class BriefSpec(BaseModel):
    """What the model reads out of a brief. Passed to Ollama as the format constraint."""

    title: str = Field(..., max_length=60, description="The song's title.")
    about: str = Field(..., max_length=300, description="What the song is about, in a sentence.")
    style: str = Field(
        ...,
        max_length=200,
        description="The sound in words: genre, instruments, production, mood. No names.",
    )
    vocal: Vocal = Field(..., description="Who sings.")
    language: str = Field(
        ..., max_length=20, description="The language the words are sung in, e.g. 'English'."
    )
    mood: str = Field(..., max_length=80, description="Three or four words for how it feels.")
    bpm: int = Field(..., ge=0, le=200, description="Beats per minute; 0 when not said.")
    key: str = Field(..., max_length=20, description="e.g. 'A minor'; empty when not said.")
    seconds: int = Field(..., ge=0, le=900, description="Length asked for; 0 when not said.")
    scene: str = Field(
        ...,
        max_length=400,
        description="The video's background picture: one place, its light and objects. No people.",
    )
    art_style: ArtStyle = Field(..., description="How the picture is made.")


DECODE_SYSTEM = """You read a request for a song that someone pasted, often a chat assistant's answer, and fill in a form a music model can use. The person is not a musician: understand what they mean and choose well for anything they leave out.

- title: the title the brief gives; if none, a two-to-five word title that fits.
- about: what the song is about, in one sentence of your own.
- style: the sound in plain words for a music model: genre, instruments, production, energy, mood, e.g. "amapiano, deep log drum, shakers, warm piano chords, soulful". Never a name: if the brief names an artist, band or song, describe how they sound instead. Never the tempo or key here.
- vocal: who sings, as the brief says; "duet" for a man and a woman, "choir" for a group, "instrumental" only if the brief asks for no singing, "any" if it does not say.
- language: the language the lyrics are written in, or the one asked for; English if neither.
- mood: three or four words.
- bpm: the tempo if the brief gives one, else one that suits the style. Never 0 unless nothing at all suggests a tempo.
- key: the key if the brief gives one, else an empty string.
- seconds: the length the brief asks for, in seconds ("3 minutes" is 180, "3:30" is 210); 0 if it asks for none.
- scene: the picture behind the video, in one or two sentences: a single place with its time of day, light, weather, colours and a few objects. Use what the brief says about the video or the picture, but only places and things: no person, child, crowd, figure, face, animal character, logo or writing, even if the brief asks for one; show where they would be instead.
- art_style: how that picture is made.

Ignore greetings, offers of more help and anything not about the song."""


def decode_brief(
    client: OllamaClient, brief: str, lyrics: str | None, *, seed: int = 0
) -> BriefSpec:
    """What the brief asks for. The lyrics, when found, are shown only for their language and feeling."""
    rest = _without(brief, lyrics) if lyrics else brief
    parts = [f"The brief:\n{rest.strip()[:6000] or '(only lyrics)'}"]
    if lyrics:
        parts.append(
            "\nIt comes with lyrics, which are sung as they are. The start of them, "
            f"for their language and feeling:\n{lyrics[:1200]}"
        )
    spec = client.generate_structured(
        schema_model=BriefSpec,
        system=DECODE_SYSTEM,
        prompt="\n".join(parts),
        temperature=0.0,
        seed=seed,
    )
    return _tidied(spec)


def _tidied(spec: BriefSpec) -> BriefSpec:
    return spec.model_copy(
        update={
            "title": spec.title.strip().strip('"').strip(),
            "style": spec.style.strip().strip(".") or DEFAULT_STYLE,
            "scene": spec.scene.strip(),
        }
    )


DEFAULT_STYLE = "pop, catchy melody, warm modern production"


# ── A song someone likes (M7) ────────────────────────────────────────────────

REFERENCE_SYSTEM = """You describe a song someone likes so that a music model can make a NEW, original song in the same style. You are given what its video says about itself, its measured tempo and length, and the words heard in it. Style is free to use; the song's melody, words and title are not, so you describe and never copy.

- style: the sound in plain words for a music model: genre and sub-genre, instruments, production, energy, mood, e.g. "amapiano, deep log drum, shakers, airy pads, warm piano chords, soulful". Work it out from the title, channel, tags, description and words. If they name an artist or a song, describe how that music sounds; never write a name. Never the tempo or key here.
- vocal: who sings in it, judging by the words heard: none or almost none is "instrumental"; "duet" for a man and a woman; "any" if you cannot tell.
- language: the language of the words heard; English if none.
- about: what the song is about, as a general theme in one sentence of your own, e.g. "missing someone who moved far away". Never a line or a phrase from it.
- title: a NEW title of two to five words for the new song. It shares no word with the original title.
- mood: three or four words.
- bpm: the measured tempo when one is given, else one that suits the style.
- key: an empty string.
- seconds: the original's length in seconds.
- scene: the picture behind the new song's video, in one or two sentences: a single place with its time of day, light, weather, colours and a few objects that carry the song's feeling. Only places and things: no person, crowd, figure, face, logo or writing.
- art_style: how that picture is made."""


def decode_reference(
    client: OllamaClient,
    *,
    title: str,
    channel: str,
    tags: tuple[str, ...],
    description: str,
    duration_s: float | None,
    tempo: float | None,
    heard: str,
    language: str | None,
    seed: int = 0,
) -> BriefSpec:
    """What a liked song sounds like, in words, and a theme and title of the new song's own."""
    lines = [
        f"Video title: {title}",
        f"Channel: {channel}",
        f"Tags: {', '.join(tags) or '(none)'}",
        f"Length: {round(duration_s)} s" if duration_s else "Length: unknown",
        f"Measured tempo: {round(tempo)} bpm" if tempo else "Measured tempo: unclear",
        f"Language heard: {language or 'unknown'}",
        f"Description:\n{description.strip()[:1200] or '(none)'}",
        f"Words heard:\n{heard.strip()[:3000] or '(no words: an instrumental)'}",
    ]
    spec = client.generate_structured(
        schema_model=BriefSpec,
        system=REFERENCE_SYSTEM,
        prompt="\n".join(lines),
        temperature=0.0,
        seed=seed,
    )
    return _tidied(spec)


# ── The lyrics, by rule ──────────────────────────────────────────────────────

_SECTIONS = (
    r"intro|verse|pre[- ]?chorus|post[- ]?chorus|chorus|hook|refrain|bridge|outro|"
    r"interlude|breakdown|break|drop|build[- ]?up|instrumental|solo|guitar solo|"
    r"rap|spoken|coda|tag|vamp|ending"
)
# A section's name at the start of a marker line: Verse 1, Pre-Chorus, Final Chorus.
_NAME = re.compile(
    rf"^((?:(?:final|last|first|second|third|double)\s+)?(?:{_SECTIONS})(?:\s*\d+)?)\b(.{{0,30}})$",
    re.IGNORECASE,
)
_REPEAT = re.compile(r"^(?:repeat|back to)\s+(?:the\s+)?(chorus|hook|refrain)\b.*$", re.IGNORECASE)
_TIMES = re.compile(r"\s*[x×]\s*\d\s*$", re.IGNORECASE)  # noqa: RUF001 - the times sign, as in "Chorus x2"
# What separates a section's name from its modifier: a colon, a bracket, a dash.
_DASHES = "-–—"  # noqa: RUF001 - hyphen, en and em dash
_LYRICS_HEADING = re.compile(r"^(?:the\s+|full\s+|song\s+)?lyrics\s*:?$", re.IGNORECASE)
# A line that starts the brief's other parts: "Style: ...", "**Vocals:** ...".
_LABEL = re.compile(
    r"^(?:title|song title|style|style of music|music style|genre|sound|mood|vibe|tempo|bpm|"
    r"key|vocals?|voice|singer|language|length|duration|visuals?|video|scene|background|"
    r"picture|image|artwork|thumbnail|notes?|instruments?|instrumentation|production|"
    r"structure|description|prompt|suno prompt|tags)\b[^:\n]{0,30}:",
    re.IGNORECASE,
)
# What is in brackets or asterisks to direct the music, not to be sung.
_DIRECTION = re.compile(
    r"\b(instrumental|solo|fade|beat|drop|music|guitar|piano|drums?|bass|synth|repeat|"
    r"spoken|whisper|pause|silence|outro|intro)\b",
    re.IGNORECASE,
)
_FENCE = re.compile(r"^(```|~~~)")
# Prose, not a sung line: a sentence that would take three breaths.
_PROSE_WORDS = 16
# A chat assistant talking to the reader after the song.
_CHATTER = re.compile(
    r"^(?:let me know|i hope|hope you|feel free|would you like|do you want|want me to|"
    r"if you(?:'d| would)? like|here(?:'s| is| are)|this song|enjoy|note:)",
    re.IGNORECASE,
)


def _plain(line: str) -> str:
    """A line without its markdown dress: **bold**, # headings, > quotes, list bullets."""
    text = line.strip()
    text = re.sub(r"^(?:#{1,6}\s*|>\s*|[-*•]\s+(?=\S))", "", text)
    text = re.sub(r"(\*\*|__|\*|_)(.+?)\1", r"\2", text)
    return text.strip().strip("*_").strip()


def _marker(line: str) -> str | None:
    """The music model's tag for a section-marker line, or None if the line is not one.

    Dressed as a marker ([Verse 1], (Chorus), **Bridge**, ## Outro), a name may
    carry a few words of modifier. Plain, it must be only the name and number,
    or end in a colon, so a sung line like "Break the chains" stays sung.
    """
    raw = line.strip()
    dressed = bool(re.match(r"^(?:#{1,6}\s|\*\*|__|\[|\()", raw))
    text = _plain(raw)
    if not text:
        return None
    bracketed = (text[0], text[-1]) in {("[", "]"), ("(", ")")}
    inner = (text[1:-1] if bracketed else text).strip()
    colon = inner.endswith(":")
    inner = _TIMES.sub("", inner.rstrip(":").strip())
    repeat = _REPEAT.match(inner)
    if repeat:
        return f"[{repeat.group(1).title()}]"
    match = _NAME.match(inner)
    if not match:
        return None
    name, rest = match.group(1), match.group(2)
    modifier = rest.strip(" :()[]" + _DASHES)
    separated = re.match(r"^\s*[:(–—]|^\s+-\s", rest)  # noqa: RUF001
    if modifier and not (dressed or bracketed or colon or separated):
        return None
    name = " ".join(
        "-".join(part.capitalize() if part.islower() else part for part in word.split("-"))
        for word in name.split()
    )
    return f"[{name} - {modifier[:24].strip()}]" if modifier else f"[{name}]"


def find_lyrics(brief: str) -> str | None:
    """The lyrics in a brief, in the music model's tagged form; None if it has none.

    Lyrics start at the first section marker, or under a "Lyrics" heading, and
    end where the brief goes on to something else: a "Style:"-like label, or a
    paragraph of prose once a blank line has closed the last section.
    """
    lines = [
        line for line in brief.replace("\r\n", "\n").split("\n") if not _FENCE.match(line.strip())
    ]
    start = None
    for number, line in enumerate(lines):
        plain = _plain(line)
        if _LYRICS_HEADING.match(plain):
            start = number + 1
            break
        if _marker(line):
            start = number
            break
    if start is None:
        return None

    out: list[str] = []
    after_blank = False
    for line in lines[start:]:
        plain = _plain(line)
        if not plain:
            after_blank = True
            out.append("")
            continue
        tag = _marker(line)
        if tag:
            out.append(tag)
            after_blank = False
            continue
        if _LYRICS_HEADING.match(plain):
            continue
        if _LABEL.match(plain) or (
            after_blank and (len(plain.split()) > _PROSE_WORDS or _CHATTER.match(plain))
        ):
            break
        if _is_direction(line.strip()) or _is_direction(plain):
            continue
        out.append(plain)
        after_blank = False

    lyrics = _fill_repeats(_tag_stanzas("\n".join(out)))
    lyrics = tidy_lyrics(lyrics)
    sung = [line for line in lyrics.splitlines() if line.strip() and not line.startswith("[")]
    return lyrics if len(sung) >= 2 else None


def _is_direction(line: str) -> bool:
    """(Guitar solo), *beat drops*, [fade out]: wrapped whole and about the music."""
    wrapped = (
        (line.startswith("(") and line.endswith(")"))
        or (line.startswith("[") and line.endswith("]"))
        or (line.startswith("*") and line.endswith("*"))
    )
    return wrapped and bool(_DIRECTION.search(line))


def _tag_stanzas(text: str) -> str:
    """Lyrics with no markers at all: a stanza that comes back is the chorus, the rest verses."""
    if re.search(r"^\[[^\]]+\]$", text, flags=re.MULTILINE):
        return text
    stanzas = [s.strip() for s in re.split(r"\n\s*\n", text) if s.strip()]
    seen = {s: stanzas.count(s) for s in stanzas}
    verse = 0
    out = []
    for stanza in stanzas:
        if seen[stanza] > 1:
            out.append(f"[Chorus]\n{stanza}")
        else:
            verse += 1
            out.append(f"[Verse {verse}]\n{stanza}")
    return "\n\n".join(out)


def _fill_repeats(text: str) -> str:
    """A chorus marker with no words under it ("Chorus x2", "Repeat chorus") sings the first chorus again."""
    sections = re.split(r"^(\[[^\]]+\])$", text, flags=re.MULTILINE)
    head, pairs = sections[0], list(zip(sections[1::2], sections[2::2], strict=False))
    first: dict[str, str] = {}
    for tag, body in pairs:
        kind = _kind(tag)
        if kind and body.strip() and kind not in first:
            first[kind] = body.strip()
    out = [head.strip()] if head.strip() else []
    for tag, body in pairs:
        kind = _kind(tag)
        if not body.strip() and kind in first:
            body = first[kind]
        out.append(f"{tag}\n{body.strip()}".strip())
    return "\n\n".join(out)


def _kind(tag: str) -> str | None:
    match = re.search(r"\b(chorus|hook|refrain)\b", tag, flags=re.IGNORECASE)
    if not match or re.search(r"pre|post", tag, flags=re.IGNORECASE):
        return None
    return match.group(1).lower()


def _without(brief: str, lyrics: str) -> str:
    """The brief with its sung lines taken out, so the model reads only what is about the song."""
    sung = {line.strip().lower() for line in lyrics.splitlines() if line.strip()}
    kept = [
        line
        for line in brief.splitlines()
        if _plain(line).lower() not in sung and not _marker(line)
    ]
    return "\n".join(kept)


# ── What the music model is told ─────────────────────────────────────────────

# ACE-Step's own list (packages/ngoma/vendor/ACE-Step-1.5/acestep/constants.py).
SUNG_LANGUAGES = frozenset(
    [
        "ar",
        "az",
        "bg",
        "bn",
        "ca",
        "cs",
        "da",
        "de",
        "el",
        "en",
        "es",
        "fa",
        "fi",
        "fr",
        "he",
        "hi",
        "hr",
        "ht",
        "hu",
        "id",
        "is",
        "it",
        "ja",
        "ko",
        "la",
        "lt",
        "ms",
        "ne",
        "nl",
        "no",
        "pa",
        "pl",
        "pt",
        "ro",
        "ru",
        "sa",
        "sk",
        "sr",
        "sv",
        "sw",
        "ta",
        "te",
        "th",
        "tl",
        "tr",
        "uk",
        "ur",
        "vi",
        "yue",
        "zh",
    ]
)
_LANGUAGE_NAMES = {
    "english": "en",
    "spanish": "es",
    "french": "fr",
    "portuguese": "pt",
    "german": "de",
    "italian": "it",
    "dutch": "nl",
    "swahili": "sw",
    "kiswahili": "sw",
    "arabic": "ar",
    "hindi": "hi",
    "japanese": "ja",
    "korean": "ko",
    "chinese": "zh",
    "mandarin": "zh",
    "cantonese": "yue",
    "russian": "ru",
    "turkish": "tr",
    "indonesian": "id",
    "malay": "ms",
    "tagalog": "tl",
    "filipino": "tl",
    "haitian creole": "ht",
    "polish": "pl",
    "ukrainian": "uk",
    "vietnamese": "vi",
    "thai": "th",
    "greek": "el",
    "hebrew": "he",
    "persian": "fa",
    "farsi": "fa",
    "urdu": "ur",
    "bengali": "bn",
    "tamil": "ta",
    "telugu": "te",
    "punjabi": "pa",
    "swedish": "sv",
    "norwegian": "no",
    "danish": "da",
    "finnish": "fi",
    "romanian": "ro",
    "hungarian": "hu",
    "czech": "cs",
}


def language_code(language: str | None) -> str:
    """The music model's code for a language; "unknown" lets it hear for itself (Zulu, Shona, a mix)."""
    text = (language or "").strip().lower()
    if not text:
        return "en"
    if text in SUNG_LANGUAGES:
        return text
    first = re.split(r"[,/;(]| and ", text)[0].strip()
    return _LANGUAGE_NAMES.get(first, "unknown")


def voice_caption(vocal: str) -> str | None:
    """The caption's words for who sings; None lets each take differ, one female, one male."""
    return {
        "female": "female vocal",
        "male": "male vocal",
        "duet": "male and female duet vocals",
        "choir": "choir vocals",
    }.get(vocal)
