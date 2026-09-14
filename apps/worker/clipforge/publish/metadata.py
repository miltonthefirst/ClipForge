"""What actually goes out: resolving one upload's metadata from three layers.

An operator can say something about a publish at three different times, and this
is the one place that decides which one wins:

1. **Per publish** — ``job.publishOptions``, chosen on the publish screen for
   this clip and no other. Most specific, so it wins.
2. **Per channel** — ``channels/{id}.defaults``, the standing preferences for a
   destination. Editable from a phone because none of it is secret.
3. **The install** — the clip's own title and description, and
   ``CLIPFORGE_YOUTUBE_DEFAULT_PRIVACY``. The floor, so a publish requested with
   no options at all still behaves sensibly.

Kept as a pure function on purpose. Resolution is the part with the interesting
edge cases — an empty tag list is not the same as an unset one, a title suffix
must survive truncation — and none of them need a network, a token or a
Firestore connection to test.

## Why the suffix is not simply appended

``titleSuffix`` is a channel handle or a series marker: the thing that makes a
clip recognisable as *yours*. YouTube rejects titles over 100 characters, and a
naive ``(title + suffix)[:100]`` chops the suffix off precisely on the titles
that are working hardest. So the suffix reserves its room first and the title is
what gives way, on a word boundary where one is close enough to matter.
"""

from __future__ import annotations

from dataclasses import dataclass

from clipforge_contracts import Channel, Clip, PublishDefaults, PublishOptions, PublishPrivacy

__all__ = [
    "FALLBACK_CATEGORY",
    "FALLBACK_TITLE",
    "MAX_DESCRIPTION",
    "MAX_TITLE",
    "PublishMetadata",
    "resolve_metadata",
]

# YouTube's own limits. Enforced here rather than at the HTTP boundary so the
# publication record — the audit trail — holds what was actually sent.
MAX_TITLE = 100
MAX_DESCRIPTION = 4900
MAX_TAGS = 20

# A title trimmed to fit should not end mid-word if a space is close by. Beyond
# this distance, cutting at the space would lose more than the tidiness is
# worth, so the hard cut wins.
WORD_BOUNDARY_REACH = 15

FALLBACK_TITLE = "Clip"
FALLBACK_CATEGORY = "22"


@dataclass(frozen=True)
class PublishMetadata:
    """The resolved answer. Everything the upload and its audit record need."""

    channel_id: str | None
    title: str
    description: str
    tags: list[str]
    privacy: PublishPrivacy
    category_id: str


def resolve_metadata(
    *,
    clip: Clip,
    options: PublishOptions | None,
    channel: Channel | None,
    default_privacy: str,
) -> PublishMetadata:
    """Decide what this upload says, from most specific source to least.

    ``channel`` is passed in already resolved rather than looked up here: a
    publish that names a channel which no longer exists is a failure the caller
    must report, not a fallback this function should quietly paper over.
    """
    defaults = channel.defaults if channel is not None else None

    privacy = (
        _privacy(options)
        or (defaults.privacy if defaults is not None else None)
        or PublishPrivacy(default_privacy)
    )

    category = _first_text(
        options.category_id if options is not None else None,
        defaults.category_id if defaults is not None else None,
        FALLBACK_CATEGORY,
    )

    # An explicitly chosen empty list means "no tags on this one", and it has to
    # stay distinguishable from "I did not open the tags field". `None` is the
    # latter; `[]` is the former, and it must not fall through to the channel's.
    per_publish_tags = options.tags if options is not None else None
    # Then the clip's own, which were written with its title and describe THIS
    # clip; the channel's are the same on a goal and on a press conference and
    # are the right answer only when nothing more specific exists.
    tags = (
        per_publish_tags
        if per_publish_tags is not None
        else (list(clip.tags) if clip.tags else _channel_tags(defaults))
    )

    return PublishMetadata(
        # None, not "": the publication record says which channel this went to,
        # and an empty string there would read as a channel whose id is blank.
        channel_id=_first_text(
            options.channel_id if options is not None else None,
            channel.id if channel is not None else None,
        )
        or None,
        title=_title(
            base=_first_text(
                options.title if options is not None else None,
                clip.title,
                FALLBACK_TITLE,
            ),
            suffix=defaults.title_suffix if defaults is not None else None,
        ),
        description=_description(
            base=_first_text(
                options.description if options is not None else None,
                clip.description,
                "",
            ),
            template=defaults.description_template if defaults is not None else None,
        ),
        # `str(getattr(tag, "root", tag))` because a `maxLength` on an array's
        # items makes the generator emit a RootModel for them, so a clip's own
        # tags arrive as objects while a channel's arrive as plain strings.
        tags=[text for text in (str(getattr(tag, "root", tag)).strip() for tag in tags) if text][
            :MAX_TAGS
        ],
        privacy=privacy,
        category_id=category,
    )


# ── The layers, one at a time ────────────────────────────────────────────────


def _privacy(options: PublishOptions | None) -> PublishPrivacy | None:
    return options.privacy if options is not None else None


def _channel_tags(defaults: PublishDefaults | None) -> list[str]:
    if defaults is None or not defaults.tags:
        return []
    return list(defaults.tags)


def _first_text(*candidates: str | None) -> str:
    """The first candidate with something in it.

    Whitespace does not count as something. A title field the operator opened,
    thought better of and left as a single space should fall through to the
    clip's title rather than publish a blank.
    """
    for candidate in candidates:
        if candidate is not None and candidate.strip():
            return candidate.strip()
    return ""


def _title(*, base: str, suffix: str | None) -> str:
    marker = (suffix or "").strip()
    if not marker:
        return _trim(base, MAX_TITLE)
    if base.endswith(marker):
        # Already there — most likely because the operator typed a per-publish
        # title by editing a previous one. Appending it twice looks like a bug
        # to everyone who sees the video, and it costs nothing to notice.
        return _trim(base, MAX_TITLE)

    room = MAX_TITLE - len(marker) - 1
    if room <= 0:
        # A suffix that fills the whole title on its own is a configuration
        # mistake, but it is the operator's choice and it is not this function's
        # place to discard it.
        return marker[:MAX_TITLE]
    return f"{_trim(base, room)} {marker}"


def _description(*, base: str, template: str | None) -> str:
    block = (template or "").strip()
    if not block:
        return base[:MAX_DESCRIPTION]
    if block in base:
        return base[:MAX_DESCRIPTION]
    joined = f"{base}\n\n{block}" if base else block
    return joined[:MAX_DESCRIPTION]


def _trim(text: str, limit: int) -> str:
    """Shorten to fit, preferring a word boundary that is close to the cut."""
    if len(text) <= limit:
        return text
    hard = text[:limit].rstrip()
    space = hard.rfind(" ")
    if space >= limit - WORD_BOUNDARY_REACH:
        return hard[:space].rstrip()
    return hard
