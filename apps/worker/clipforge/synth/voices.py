"""Giving each character a voice of their own.

The model says what kind of voice a character has — a man or a woman, US or
UK — and this picks an actual Kokoro voice of that kind, a different one for
each character, so two men in one conversation do not sound like one man
talking to himself. The narrator keeps the voice the job asked for.

Deterministic from the seed: the same cast with the same seed gets the same
voices, which is part of what makes a composed clip reproducible.
"""

from __future__ import annotations

from collections.abc import Sequence

from clipforge_contracts import VoiceKind

__all__ = ["NARRATOR", "VOICES_BY_KIND", "assign_voices", "is_narrator", "voice_kind_for_name"]

#: The speaker name that means "nobody on screen". Matched without case.
NARRATOR = "Narrator"

#: Kokoro's English voices by kind, best first. The first of each kind is the
#: one the remake stage already used, so a one-character video sounds as it did.
VOICES_BY_KIND: dict[VoiceKind, tuple[str, ...]] = {
    VoiceKind.WOMAN_US: ("af_heart", "af_bella", "af_sarah", "af_nova", "af_jessica", "af_sky"),
    VoiceKind.MAN_US: ("am_michael", "am_adam", "am_eric", "am_liam", "am_onyx", "am_echo"),
    VoiceKind.WOMAN_UK: ("bf_emma", "bf_isabella", "bf_alice", "bf_lily"),
    VoiceKind.MAN_UK: ("bm_george", "bm_lewis", "bm_daniel", "bm_fable"),
}


def is_narrator(speaker: str) -> bool:
    return speaker.strip().casefold() == NARRATOR.casefold()


def voice_kind_for_name(name: str, index: int) -> VoiceKind:
    """A kind for a character the model did not cast, alternating so a pair differs."""
    del name
    return (VoiceKind.WOMAN_US, VoiceKind.MAN_US, VoiceKind.WOMAN_UK, VoiceKind.MAN_UK)[index % 4]


def assign_voices(
    cast: Sequence[tuple[str, VoiceKind]],
    *,
    narrator_voice: str,
    seed: int = 0,
) -> dict[str, str]:
    """A voice per character, keyed by folded name; distinct within a kind while it can be.

    The narrator's voice is skipped for characters when the kind has another,
    so the narrator and a character are not the same voice. Rotation starts at
    the seed so different videos get different pairings; the same seed gets
    the same pairing.
    """
    assigned: dict[str, str] = {}
    used: set[str] = {narrator_voice}
    for name, kind in cast:
        key = name.strip().casefold()
        if not key or key in assigned or is_narrator(name):
            continue
        options = VOICES_BY_KIND[kind]
        start = seed % len(options)
        ordered = [*options[start:], *options[:start]]
        choice = next((voice for voice in ordered if voice not in used), ordered[0])
        assigned[key] = choice
        used.add(choice)
    return assigned
