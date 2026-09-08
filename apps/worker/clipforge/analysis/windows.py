"""Windowing a transcript for the map step.

Decision **D6**. A 60-minute transcript is roughly 12,000 tokens, and a 4B model
at 16K context does poor *holistic* selection over that — it drifts toward the
beginning, repeats itself, and misses the middle entirely. Windowing is what
makes a small model viable at all: each call gets a bounded, coherent chunk and is
asked a question it can actually answer.

The overlap is not redundancy to be eliminated. A 120-second window on a 30-second
stride means every moment is seen four times, from four different contexts — so a
moment that only reads as interesting when you can hear what preceded it still
gets found. The duplicates that produces are exactly what the IoU merge in
`ranking.merge_overlapping` exists to collapse.
"""

from __future__ import annotations

from dataclasses import dataclass

from clipforge_contracts import Transcript, TranscriptSegment

__all__ = [
    "DEFAULT_WINDOW_SPEC",
    "TranscriptWindow",
    "WindowSpec",
    "build_windows",
    "render_window",
]


@dataclass(frozen=True)
class WindowSpec:
    window_sec: float = 120.0
    stride_sec: float = 30.0


DEFAULT_WINDOW_SPEC = WindowSpec()


@dataclass(frozen=True)
class TranscriptWindow:
    """A slice of transcript, with the timings the model needs to answer in."""

    index: int
    start_sec: float
    end_sec: float
    segments: tuple[TranscriptSegment, ...]

    @property
    def is_empty(self) -> bool:
        return not self.segments

    @property
    def text(self) -> str:
        return " ".join(segment.text for segment in self.segments).strip()


def build_windows(transcript: Transcript, spec: WindowSpec | None = None) -> list[TranscriptWindow]:
    """Slice a transcript into overlapping windows.

    Segments are assigned by overlap, not by containment, so a sentence straddling
    a window edge appears in both — the model should never be shown half a
    sentence and asked whether it is self-contained.
    """
    spec = spec or DEFAULT_WINDOW_SPEC
    if not transcript.segments:
        return []

    duration = transcript.duration_sec or max(s.end_sec for s in transcript.segments)
    if duration <= 0:
        return []

    windows: list[TranscriptWindow] = []
    index = 0
    start = 0.0

    while start < duration:
        end = min(start + spec.window_sec, duration)
        segments = tuple(
            segment
            for segment in transcript.segments
            if segment.end_sec > start and segment.start_sec < end
        )
        if segments:
            windows.append(
                TranscriptWindow(index=index, start_sec=start, end_sec=end, segments=segments)
            )
            index += 1

        # The final window always reaches the end; stop rather than emitting a
        # sequence of ever-shorter tails that all say the same thing.
        if end >= duration:
            break
        start += spec.stride_sec

    return windows


def render_window(window: TranscriptWindow) -> str:
    """Format a window for the prompt.

    Timestamps are included per segment and stated in absolute seconds, because
    the model is asked to return absolute times. Giving it window-relative text
    and expecting absolute answers is a reliable way to get boundaries that are
    wrong by exactly the window offset.
    """
    lines = [
        f"[{segment.start_sec:.1f}-{segment.end_sec:.1f}] {segment.text}"
        for segment in window.segments
    ]
    return "\n".join(lines)
