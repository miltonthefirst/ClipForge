"""Learning what a reviewer keeps asking for, so they stop having to ask.

A reviewer who writes *"follow the ball"* on every football clip is teaching the
same thing every time, and a system that cannot hold it makes them type it
forever. After a remake that actually applied feedback, the local model is asked
one question — *what, if anything, generalises?* — and the answer is stored as a
**proposal**.

The shape is taken from the `sarungano` project's lesson loop, which solves the
same problem for a different medium, and the three rules that make it work there
transfer unchanged:

* **Proposed, never applied.** Nothing shapes a later clip until a human accepts
  it. The asymmetry is the whole argument: a wrong standing rule silently
  reframes every future clip and the reviewer has no reason to suspect it, while
  a missed one costs one more sentence in a note. When in doubt, propose
  nothing.
* **An empty answer is the common one.** Most single corrections are about one
  clip's particular problem and teach nothing durable. The prompt says so, and
  the cap of three exists to stop a model that has decided to be helpful.
* **Never propose the same thing twice.** The model is shown what has been
  accepted *and* what has been rejected, and `dedupe` drops anything already
  held in any status. A suggestion that comes back after being turned down is
  the difference between a system that learns and one that nags.

What is stored is deliberately two things at once: a sentence, and optionally a
few settings. The sentence goes into the note-reading prompt as standing
context; the settings pre-fill the remake form. Plenty of what a reviewer
teaches — *"this channel's wide shots are unusable cropped"* — is a judgement
that fills in no box and is still worth carrying.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from datetime import UTC, datetime

from clipforge_contracts import (
    AppliedRemake,
    Clip,
    FramingMode,
    LlmPreferenceProposal,
    NoteFraming,
    NoteTopic,
    ObscureFound,
    ObscureOptions,
    ObscureRegion,
    Preference,
    PreferenceScope,
    PreferenceStatus,
    RemakeDefaults,
)

from clipforge.media.obscure import describe_region, overlaps
from clipforge.models.ollama import OllamaClient, OllamaError
from clipforge.observability import get_logger

log = get_logger(__name__)

__all__ = [
    "LEARN_PROMPT_VERSION",
    "LEARN_SYSTEM_PROMPT",
    "apply_to_options",
    "dedupe",
    "propose",
    "propose_obscure",
    "standing_guidance",
]

LEARN_PROMPT_VERSION = "v1"

# Three is not a limit anyone will hit honestly. One correction teaches one
# thing at most; a model returning three is usually splitting one observation
# into variations of itself.
MAX_PER_REMAKE = 3

LEARN_SYSTEM_PROMPT = """\
You read one correction a reviewer just made to a short video clip, and decide \
what — if anything — the system should remember so they do not have to ask \
again.

Separate corrections that were about THIS CLIP from ones that reveal a STANDING \
PREFERENCE. Only standing preferences are worth keeping.

The test is simple: **would a different clip from this same source be made better if the system already knew this?** If yes, it is worth remembering. \
Two things in one correction is normal \
— a reviewer who asks for a language and a framing in the same sentence has \
taught you two things.

Worth remembering: something that would change how a *different* clip is made. \
"This channel's wide pitch shots lose the ball unless the window follows it." \
"Commentary on this source is French and this reviewer always wants English." \
"Crowd noise matters on these, so narration should sit over it rather than \
replace it."

NOT worth remembering, do not propose these: anything tied to this clip's own \
moment ("it cuts in late on this one"); a one-off choice with nothing to \
suggest it recurs; anything already in the list of preferences you are shown, \
in any wording; a restatement of what the controls do by default.

An empty list is a perfectly good answer when the correction really was about \
one clip's own moment, and a wrong standing preference costs more than a missed \
one — it shapes every later clip and the reviewer has no reason to suspect it \
is there. But do not reach for the empty list to be safe: a reviewer correcting \
the same thing on every clip of a series is teaching you something, and failing \
to notice it is the reason they have to keep typing.

Scope each one you do propose:
- SOURCE: this channel or series only. The right default. Footage differs more \
than people do, and a rule learned from football should not reframe a talking \
head.
- EVERYTHING: true of every clip this reviewer will ever review. Rarer than it \
feels while writing one. Use it only for something about the reviewer rather \
than about the footage."""


def _normalise(text: str) -> str:
    """A comparison key that ignores how a sentence happens to be written.

    Apostrophes are deleted rather than turned into spaces, so "this channel's
    wide shots" and "this channels wide shots" collapse to the same key. They
    are the difference between two phrasings of one preference, and treating
    them as distinct is how the same suggestion comes back a second time.
    """
    stripped = unicodedata.normalize("NFKD", (text or "").lower())
    # Named escapes rather than the characters themselves: a curly quote in
    # source is indistinguishable from a straight one at a glance, and the
    # linter is right to object to it.
    for apostrophe in ("'", "\N{LEFT SINGLE QUOTATION MARK}", "\N{RIGHT SINGLE QUOTATION MARK}"):
        stripped = stripped.replace(apostrophe, "")
    return " ".join(re.sub(r"[^\w\s]", " ", stripped).split())


def build_prompt(
    *,
    note: str,
    applied: AppliedRemake,
    source_title: str | None,
    known: list[Preference],
) -> str:
    """The user half: what just happened, and what is already remembered."""
    voice = applied.voice
    lines = [
        "The reviewer wrote this about a clip:",
        "",
        note.strip() or "(no note; they used the controls directly)",
        "",
        "What the system did about it:",
        f"- framing: {applied.framing_mode.value}",
    ]
    if voice is not None:
        lines.append(
            f"- voice: {voice.voice} in {voice.language}"
            f"{', translated' if voice.translated else ''}"
        )
    if applied.refusals:
        lines.append(f"- could not do: {'; '.join(str(r) for r in applied.refusals)}")
    if source_title:
        lines.append(f"\nThe clip came from: {source_title}")

    lines.append(
        "\nPreferences already held for this reviewer — do not propose any of "
        "these again, in any wording, whether they were accepted or turned down:"
    )
    if known:
        lines.append(
            "```json\n"
            + json.dumps(
                [
                    {"lesson": p.lesson, "scope": p.scope.value, "status": p.status.value}
                    for p in known
                ],
                indent=2,
            )
            + "\n```"
        )
    else:
        lines.append("(none yet)")

    lines.append(
        "\nQuestion: if a DIFFERENT clip arrived from this same source tomorrow, "
        "is there anything here the system should already know, so the reviewer "
        "does not have to say it again? List it if so, and return an empty list "
        "if this correction was only about this one clip."
    )
    return "\n".join(lines)


def propose(
    client: OllamaClient,
    *,
    note: str,
    applied: AppliedRemake,
    clip: Clip,
    source_title: str | None,
    known: list[Preference],
    uid: str,
) -> list[Preference]:
    """Ask what generalises. Never raises.

    A failure here must not affect the remake, which has already been produced
    and saved: learning is a bonus pass over a finished result, and a model that
    is unreachable or unhelpful should cost nothing but the lesson.
    """
    try:
        answer = client.generate_structured(
            schema_model=LlmPreferenceProposal,
            system=LEARN_SYSTEM_PROMPT,
            prompt=build_prompt(note=note, applied=applied, source_title=source_title, known=known),
            temperature=0.2,
        )
    except OllamaError as exc:
        log.info("preferences.unavailable", error=str(exc))
        return []

    now = datetime.now(UTC)
    proposed: list[Preference] = []
    for item in (answer.preferences or [])[:MAX_PER_REMAKE]:
        lesson = (item.lesson or "").strip()
        if not lesson:
            continue
        scope = item.scope or PreferenceScope.SOURCE
        proposed.append(
            Preference(
                id=uuid.uuid4().hex,
                uid=uid,
                scope=scope,
                # An EVERYTHING preference is stored unattached, so it is
                # retrieved for every clip rather than only for the source that
                # happened to teach it.
                source_id=None if scope is PreferenceScope.EVERYTHING else clip.source_id,
                category=item.category or NoteTopic.FRAMING,
                lesson=lesson[:400],
                defaults=_defaults_from(item),
                status=PreferenceStatus.PROPOSED,
                from_clip_id=clip.id,
                from_note=note[:2000] or None,
                created_at=now,
            )
        )
    log.info("preferences.proposed", count=len(proposed))
    return proposed


def propose_obscure(
    regions: list[ObscureRegion],
    *,
    clip: Clip,
    uid: str,
    known: list[Preference],
) -> list[Preference]:
    """The one lesson that is written without asking the model anything.

    Everything else here is a sentence, and a sentence is what a language model
    is for. This one is a rectangle. A 4B asked where a channel puts its logo
    will produce plausible coordinates, and plausible coordinates blur the
    crowd and leave the logo — so the numbers come from the detector that
    actually measured them, and the model is not consulted at all.

    It follows that this fires on a remake with **no note**, unlike the rest of
    learning. Ticking a box and getting three marks found is exactly the moment
    to ask whether they should always be hidden, and there is no sentence
    involved anywhere in it.
    """
    novel = [region for region in regions if region.found is not ObscureFound.REMEMBERED]
    if not novel or not clip.source_id:
        return []

    for preference in known:
        if preference.category is not NoteTopic.OBSCURE:
            continue
        held = (
            preference.defaults.obscure.regions
            if preference.defaults and preference.defaults.obscure
            else None
        ) or []
        # Compared by overlap rather than by wording. The same logo measured on
        # two different clips comes back a tenth of a percent apart, and a
        # text comparison would propose it again every single time.
        if held and all(any(overlaps(region, seen) for seen in held) for region in novel):
            return []

    where = " and ".join(describe_region(region) for region in novel[:3])
    mark, them = ("mark", "it") if len(novel) == 1 else ("marks", "them")
    return [
        Preference(
            id=uuid.uuid4().hex,
            uid=uid,
            scope=PreferenceScope.SOURCE,
            source_id=clip.source_id,
            category=NoteTopic.OBSCURE,
            lesson=(
                f"This source burns {len(novel)} fixed {mark} into the picture "
                f"— {where} — so hide {them} on every clip cut from it."
            )[:400],
            defaults=RemakeDefaults(
                obscure=ObscureOptions(
                    auto=False,
                    regions=[
                        region.model_copy(update={"found": ObscureFound.REMEMBERED})
                        for region in novel[:6]
                    ],
                )
            ),
            status=PreferenceStatus.PROPOSED,
            from_clip_id=clip.id,
            created_at=datetime.now(UTC),
        )
    ]


def _defaults_from(item: object) -> RemakeDefaults | None:
    """The machine-readable half, when the model named one.

    `NoteFraming` carries NOT_MENTIONED and `FramingMode` does not, so the
    translation between them is also the filter: a preference that named no mode
    simply has no framing default.
    """
    mode = getattr(item, "framing_mode", None)
    language = (getattr(item, "language", None) or "").strip() or None
    framing = (
        FramingMode(mode.value)
        if mode is not None and mode is not NoteFraming.NOT_MENTIONED
        else None
    )
    if framing is None and language is None:
        return None
    return RemakeDefaults(framing_mode=framing, language=language)


def dedupe(proposed: list[Preference], known: list[Preference]) -> list[Preference]:
    """Drop anything already held, whatever its status.

    Against every status, not just the accepted ones. A preference the reviewer
    rejected must not reappear on the next correction of the same kind of clip —
    that is precisely the loop that turns a learning system into a nag, and the
    rejection is itself the thing worth remembering.
    """
    seen = {_normalise(p.lesson) for p in known}
    fresh: list[Preference] = []
    for candidate in proposed:
        key = _normalise(candidate.lesson)
        if not key or key in seen:
            continue
        seen.add(key)
        fresh.append(candidate)
    return fresh


def standing_guidance(accepted: list[Preference]) -> list[str]:
    """The accepted preferences, as lines for the note-reading prompt.

    They are shown to the model that interprets the next note, so a reviewer who
    has already taught something does not have to repeat it to be understood.
    """
    return [f"{p.category.value.lower()}: {p.lesson}" for p in accepted]


def apply_to_options(
    accepted: list[Preference],
    *,
    framing_set: bool,
    voice_set: bool,
) -> RemakeDefaults:
    """Collapse the accepted preferences into settings to pre-fill.

    Only fills what the reviewer has not already decided for this particular
    remake — the same precedence the note layer uses, and for the same reason: a
    preference is a default, and a default that overrides a choice is not a
    default.

    Later preferences win over earlier ones where both name the same setting. A
    reviewer who taught something twice has changed their mind, and the most
    recent statement is the one to keep.
    """
    framing: FramingMode | None = None
    language: str | None = None
    for preference in accepted:
        defaults = preference.defaults
        if defaults is None:
            continue
        if defaults.framing_mode is not None and not framing_set:
            framing = defaults.framing_mode
        if defaults.language and not voice_set:
            language = defaults.language
    return RemakeDefaults(framing_mode=framing, language=language)
