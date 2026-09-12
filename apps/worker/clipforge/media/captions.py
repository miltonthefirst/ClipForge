"""Generating ASS subtitles with word-by-word karaoke timing.

This is where Phase 4's insistence on word-level timestamps pays off. Segment-level
captions appear a sentence at a time and read as a transcript; word-level karaoke
highlights each word as it is spoken, which is what makes short-form captions feel
like part of the video rather than an accessibility afterthought.

Everything here is a **pure function over words**. No ffmpeg, no files — the ASS
document is a string, which means caption timing is unit-testable without
rendering anything.

Two details that are easy to get wrong:

**ASS `\\k` durations are centiseconds, not milliseconds.** A factor-of-ten error
here produces captions that drift further out of sync the longer the clip runs,
which looks like a transcription problem and is not one.

**Times are relative to the clip, not the source.** The renderer cuts first and
burns captions onto the cut, so a caption timed against source timestamps would
be offset by exactly the clip's start.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from clipforge_contracts import TranscriptWord

from clipforge.media.profiles import OUTPUT_HEIGHT, OUTPUT_WIDTH, CaptionStyle

__all__ = ["CaptionCue", "build_ass", "group_into_cues"]


@dataclass(frozen=True)
class CaptionCue:
    """One on-screen caption: a few words shown together."""

    start_sec: float
    end_sec: float
    words: tuple[TranscriptWord, ...]

    @property
    def text(self) -> str:
        return " ".join(w.text.strip() for w in self.words)


def group_into_cues(
    words: Sequence[TranscriptWord],
    *,
    style: CaptionStyle,
    clip_start_sec: float,
    clip_end_sec: float,
    max_gap_sec: float = 0.6,
) -> list[CaptionCue]:
    """Group words into readable cues, in clip-relative time.

    Grouped by character budget rather than by word count, because "and" and
    "extraordinarily" occupy very different amounts of a 1080-pixel line. A pause
    also breaks a cue: reading across a silence feels wrong even when the
    characters would fit.
    """
    budget = style.max_chars_per_line * style.max_lines
    cues: list[CaptionCue] = []
    current: list[TranscriptWord] = []
    length = 0

    for word in words:
        if word.end_sec <= clip_start_sec or word.start_sec >= clip_end_sec:
            continue

        # Re-based to the clip, and clamped: a word straddling the boundary is
        # shown for the part that is actually visible.
        rebased = TranscriptWord(
            text=word.text,
            start_sec=max(0.0, word.start_sec - clip_start_sec),
            end_sec=min(clip_end_sec - clip_start_sec, word.end_sec - clip_start_sec),
            probability=word.probability,
        )
        if rebased.end_sec <= rebased.start_sec:
            continue

        token = rebased.text.strip()
        gap = rebased.start_sec - current[-1].end_sec if current else 0.0
        would_overflow = current and length + len(token) + 1 > budget

        if would_overflow or (current and gap > max_gap_sec):
            cues.append(_close(current))
            current, length = [], 0

        current.append(rebased)
        length += len(token) + 1

    if current:
        cues.append(_close(current))
    return cues


def _close(words: list[TranscriptWord]) -> CaptionCue:
    return CaptionCue(start_sec=words[0].start_sec, end_sec=words[-1].end_sec, words=tuple(words))


def _timestamp(seconds: float) -> str:
    """ASS wants `H:MM:SS.cc` — centisecond precision, one-digit hours."""
    seconds = max(0.0, seconds)
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    centis = round((seconds - int(seconds)) * 100)
    if centis == 100:  # Rounding can carry.
        centis = 0
        secs += 1
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _escape(text: str) -> str:
    """ASS treats braces as override blocks and backslashes as escapes.

    A transcript containing a brace is rare; a transcript containing one and
    silently swallowing the rest of the line is the kind of bug that only shows
    up on someone else's video.
    """
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def build_ass(
    cues: Sequence[CaptionCue],
    *,
    style: CaptionStyle,
    width: int = OUTPUT_WIDTH,
    height: int = OUTPUT_HEIGHT,
) -> str:
    """Render cues to a complete ASS document."""
    margin_v = int(height * style.margin_v_pct / 100)
    margin_h = int(width * style.margin_h_pct / 100)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: ClipForge,{style.font_name},{style.font_size},{style.primary_colour},{style.highlight_colour},{style.outline_colour},{style.back_colour},{-1 if style.bold else 0},0,0,0,100,100,0,0,1,{style.outline},{style.shadow},2,{margin_h},{margin_h},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"""

    lines = [header]
    for cue in cues:
        lines.append(
            f"Dialogue: 0,{_timestamp(cue.start_sec)},{_timestamp(cue.end_sec)},"
            f"ClipForge,,0,0,0,,{_cue_text(cue, style)}"
        )
    return "\n".join(lines) + "\n"


def _cue_text(cue: CaptionCue, style: CaptionStyle) -> str:
    if not style.karaoke:
        return _escape(cue.text)

    parts: list[str] = []
    cursor = cue.start_sec
    for word in cue.words:
        # Any gap before the word is held on the previous highlight state, so the
        # karaoke sweep does not jump ahead during a pause.
        lead = max(0.0, word.start_sec - cursor)
        duration = max(0.0, word.end_sec - word.start_sec) + lead
        # Centiseconds. Milliseconds here is a factor-of-ten desync that grows
        # across the clip and looks like bad transcription.
        centis = max(1, round(duration * 100))
        parts.append(f"{{\\k{centis}}}{_escape(word.text.strip())}")
        cursor = word.end_sec

    return " ".join(parts)
