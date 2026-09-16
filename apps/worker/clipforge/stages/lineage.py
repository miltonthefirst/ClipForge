"""What a new clip inherits from the version it was made from.

MUSIC and REMAKE both produce a new clip out of a finished one, and both have to
answer the same questions before they can re-cut a picture: which window the
parent was actually cut from, how it was framed, where its window travelled, and
which profile rendered it. They answered them differently, and the disagreement
is the bug.

MUSIC's caption-free re-render read the *candidate* window, passed no framing
and no obscure regions, and then saved the parent's ``AppliedRemake`` over the
result — so taking the captions off a remade clip threw the reviewer's trim,
their reframe and their blur boxes out of the pixels while the document went on
asserting all three. Two production clips matched a plain centre crop of their
source at 31-34 dB PSNR_y and their own remade parents at 14-17, with a
watermark the parent had blurred sharp again in the child. The reviewer's own
words for it: "adding music is overriding the current remade".

Nothing here touches a store or the disk, so the precedence rules are one
testable thing rather than a branch inside an I/O path.
"""

from __future__ import annotations

from clipforge_contracts import Clip, Framing, FramingMode, PanKeyframe

__all__ = [
    "inherited_framing",
    "inherited_keyframes",
    "inherited_mode",
    "inherited_profile",
    "parent_window",
]


def parent_window(clip: Clip) -> tuple[float, float] | None:
    """The window this clip's own picture was cut from, when it records one.

    ``AppliedRemake`` is the only record of it, and ``None`` means there is
    none — the caller falls back to the candidate, which is the sole truth for a
    clip RENDER made.

    Resolving through ``candidateId`` first instead is how a second correction
    silently discarded the first: the candidate is copied onto every derived
    clip, so every generation resolved back to the same original window.
    """
    applied = clip.remake
    return (applied.start_sec, applied.end_sec) if applied is not None else None


def inherited_framing(clip: Clip) -> Framing | None:
    """The framing a re-cut of this clip should use, absent a new request.

    ``None`` means the profile's fixed crop, which is the right answer for a
    clip RENDER made — that IS what it has. For one that was reframed, the
    answer is whatever it was reframed to, so a job about something else does
    not quietly undo it.

    FIT's fill, zoom and vertical offset are not on ``AppliedRemake``, so an
    inherited FIT comes back with the defaults. That is a real if small loss of
    fidelity and is worth less than the alternative, which is an inherited FIT
    that is not FIT at all.
    """
    applied = clip.remake
    if applied is None or applied.framing_mode is FramingMode.AS_RENDERED:
        return None
    return Framing(
        mode=applied.framing_mode,
        # Only PAN reads them off the framing. TRACK re-runs the tracker in
        # REMAKE — the more correct answer when the trim has moved, the same
        # answer when it has not — and MUSIC replays the recorded path through
        # :func:`inherited_keyframes` instead, because its cut is the parent's
        # cut and a second tracker run would decide it differently.
        keyframes=list(applied.keyframes or []) if applied.framing_mode is FramingMode.PAN else [],
    )


def inherited_keyframes(clip: Clip) -> list[PanKeyframe]:
    """The path this clip's window actually took, PAN or TRACK alike.

    ``AppliedRemake.keyframes`` is the record of what was rendered, not of what
    was asked for, which is why it is usable for both modes: the reviewer's own
    points under PAN and the tracker's findings under TRACK. Reproducing a
    picture exactly means replaying them rather than deciding again.
    """
    return list(clip.remake.keyframes or []) if clip.remake is not None else []


def inherited_mode(clip: Clip) -> FramingMode:
    """What this clip's picture is framed as, for the record on a copy of it."""
    return clip.remake.framing_mode if clip.remake is not None else FramingMode.AS_RENDERED


def inherited_profile(clip: Clip) -> str:
    """The profile this clip was rendered with, by name.

    ``renderProfile`` holds ``name:version``; :func:`load_profile` wants the
    name, and handed the whole identifier it finds no such file, warns, and
    returns the built-in default — so a custom profile was lost on every re-cut
    that passed the identifier straight through.
    """
    identifier = clip.render_profile
    if not identifier:
        return "default"
    return identifier.split(":", 1)[0]
