"""Looking at the clip before saying anything about it.

## Why this exists

The pipeline was narrating footage it had never seen, and every step was working
correctly while it did.

A reviewer asked for English over French football commentary. Whisper heard
*"très mal à beaude glim cette frappe pure latérale gauche municois"*. The
translator did its job faithfully and returned *"very bad beauty glim this pure
left lateral munitions shot"*. Kokoro read that aloud over a goal, Whisper
transcribed the result back for captions, and the clip went into the review
queue with *"7,000 flaps go. She is magnificent one and"* burnt across the
bottom. The title, taken from the same transcript, was a lowercase French
fragment.

Nothing in that chain could tell the words were nonsense, because nothing in the
chain had seen a football. That is the gap this closes: one look at three
frames, described plainly, handed to everything downstream that writes words.

## What it produces, and what it deliberately does not

Description, never inference. What is in the frame, what the scoreboard reads,
what colours the sides are wearing. Not who is about to score and not why the
moment matters — a model asked to speculate produces confident fiction, and
confident fiction is exactly the failure mode already being fixed.

Measured on the reviewer's own footage: handed one frame of a Bayern match,
`mistral-small3.2` returned the sport, the shirt colours, the scoreline, the
clock and the hoarding text, and got the away side's name wrong. That ratio is
the reason `on_screen_text` is a separate field — the characters it reads off
the score bug are reliable in a way its team naming is not.

## Cost, and why it is optional everywhere

The capable model on this machine is 15 GB against 6 GB of VRAM, so it runs
mostly on the CPU and takes about a minute for three frames. That is affordable
once per remake and not affordable per candidate in a harvest, which is why the
callers treat it as enrichment: `look` returns None rather than raising, and
every caller has a path that works without it.
"""

from __future__ import annotations

import base64
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from clipforge_contracts import LlmVisualContext

from clipforge.models.ollama import OllamaClient, OllamaError
from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = ["VISION_VRAM_MB", "VisualContext", "look"]

# Wide enough for a vision encoder to read a score bug, small enough that three
# of them are a couple of hundred kilobytes of base64 rather than megabytes.
_FRAME_WIDTH = 512

# Three, spaced across the cut. One frame cannot tell a shot from a celebration;
# beyond three the answers stop improving and the wall-clock keeps climbing,
# which on a model this size is the binding constraint.
_FRAMES = 3

# What to tell the broker. Not the model's real size — it does not fit and is
# offloaded — but enough to serialise it against Whisper and the note reader,
# which is the whole reason the broker exists. Two models thrashing 6 GB between
# them is slower than either alone.
VISION_VRAM_MB = 4500

# A minute and a half. The measured cost is around seventy seconds for three
# frames with the model cold; the margin covers a first call that has to read
# fifteen gigabytes off disk.
_TIMEOUT_S = 300.0

SYSTEM_PROMPT = """\
You are looking at still frames taken from one short video clip, in order.

Say what is actually there. Name the sport, setting or activity. Name the teams, \
people or places only if their colours, kit, or text on screen tell you — and if \
they do not, describe what you can see instead of guessing a name.

Read any text in the frames exactly as it appears: scorelines, clocks, team \
abbreviations, captions, hoardings. This is the most reliable thing in a frame \
and it belongs in `onScreenText` character for character.

Describe what happens across the frames in order, as a sequence of events.

When you cannot tell what a moment IS, say what you can see instead of naming it. "A player strikes the ball from the left" is a description; "a corner kick" is a guess about a phase of play you did not watch, and a guess here becomes a confident false statement in a voice-over.

The same applies to outcomes. You are looking at a few stills out of hundreds of frames, so you cannot see whether a shot went in, whether a save was made or whether a pass was completed. Say where the ball is and what the players are doing. Never say a goal was scored.

Do not speculate about what happens after the last frame. Do not say why the \
moment matters. Do not describe the video as a video — no "in the first frame", \
no "this clip shows". Write about what is happening, not about the footage."""


@dataclass(frozen=True)
class VisualContext:
    """What the frames show, plus which model said so."""

    subject: str
    happens: str
    on_screen_text: list[str]
    model: str

    def as_prompt(self) -> str:
        """The block handed to whatever is about to write words.

        Phrased as evidence rather than as instruction, and labelled as coming
        from the pictures, so a downstream model can weigh it against a
        transcript that disagrees instead of treating both as fact.
        """
        lines = [f"What the pictures show: {self.subject}", f"What happens: {self.happens}"]
        if self.on_screen_text:
            lines.append("Text visible on screen: " + "; ".join(self.on_screen_text[:8]))
        return "\n".join(lines)


def look(
    source: Path,
    *,
    start_sec: float,
    end_sec: float,
    client: OllamaClient,
    model: str,
    frames: int = _FRAMES,
    ffmpeg_bin: str = "ffmpeg",
) -> VisualContext | None:
    """Describe what is on screen between two timestamps. Never raises.

    None on any failure — no model, no frames, a refusal, a timeout. Every
    caller has a path that works without this, and losing the look must never
    lose the clip: it is the difference between a narration that is merely
    uninformed and a job that failed.
    """
    if end_sec - start_sec <= 0.2:
        return None

    try:
        stills = _stills(
            source,
            start_sec=start_sec,
            end_sec=end_sec,
            count=max(1, frames),
            ffmpeg_bin=ffmpeg_bin,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("vision.no_frames", error=str(exc))
        return None
    if not stills:
        return None

    try:
        answer = client.generate_structured(
            schema_model=LlmVisualContext,
            system=SYSTEM_PROMPT,
            prompt=(
                "Describe this clip from these frames, taken in order across it. "
                "Answer about the clip as a whole."
            ),
            images=stills,
            model=model,
            timeout_s=_TIMEOUT_S,
            temperature=0.0,
        )
    except OllamaError as exc:
        log.info("vision.unavailable", model=model, error=str(exc))
        return None

    subject = (answer.subject or "").strip()
    happens = (answer.happens or "").strip()
    if not subject and not happens:
        return None

    context = VisualContext(
        subject=subject,
        happens=happens,
        on_screen_text=[
            text for text in (_plain(item) for item in answer.on_screen_text or []) if text
        ],
        model=model,
    )
    log.info(
        "vision.looked",
        model=model,
        frames=len(stills),
        subject=context.subject[:80],
        on_screen=len(context.on_screen_text),
    )
    return context


def _plain(item: object) -> str:
    """One string out of a generated list item.

    A `maxLength` on an array's items makes the generator emit a RootModel for
    them, so `str(item)` is `"root='CANAL+'"` rather than `CANAL+`. That went
    straight into a prompt before anyone noticed.
    """
    return str(getattr(item, "root", item)).strip()


def _stills(
    source: Path,
    *,
    start_sec: float,
    end_sec: float,
    count: int,
    ffmpeg_bin: str,
) -> list[str]:
    """Evenly spaced frames, as base64 JPEG.

    Taken at the midpoint of each of `count` equal slices rather than at the
    boundaries: the first and last frames of a cut are the ones most likely to
    be a dissolve, a replay wipe or a graphic, and those describe the broadcast
    rather than the action.

    Written to disk and read back rather than piped, because one ffmpeg seek per
    frame is far cheaper than decoding the whole window, and because `-ss`
    before `-i` only seeks when there is a single output.
    """
    span = end_sec - start_sec
    stills: list[str] = []
    with TemporaryDirectory(prefix="clipforge-vision-") as scratch:
        directory = Path(scratch)
        for index in range(count):
            at = start_sec + span * (index + 0.5) / count
            path = directory / f"frame{index}.jpg"
            done = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [
                    ffmpeg_bin,
                    "-v",
                    "error",
                    "-ss",
                    f"{at:.3f}",
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale={_FRAME_WIDTH}:-2",
                    "-q:v",
                    "4",
                    str(path),
                ],
                capture_output=True,
                timeout=60.0,
                check=False,
            )
            if done.returncode != 0 or not path.is_file():
                log.info("vision.frame_failed", at=round(at, 2), error=done.stderr.decode()[:200])
                continue
            stills.append(base64.b64encode(path.read_bytes()).decode("ascii"))
    return stills
