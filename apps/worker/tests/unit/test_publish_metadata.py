"""Resolving one upload's metadata from three layers of preference.

The interesting cases are all about *precedence and absence*, which is why this
is tested against a pure function rather than through the stage: "an empty tag
list is not an unset one" is a two-line assertion here and a fixture-heavy
integration test there.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from clipforge.publish.metadata import MAX_TITLE, PublishMetadata, resolve_metadata
from clipforge_contracts import (
    Channel,
    ChannelConnection,
    Clip,
    ClipLocation,
    PublishDefaults,
    PublishOptions,
    PublishPlatform,
    PublishPrivacy,
    ReviewState,
)

# Every test in this file is the unit tier. Without this marker CI's
# `pytest -m unit` silently deselects the whole file — the tests pass locally,
# run nowhere, and protect nothing.
pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def make_clip(
    *, title: str | None = "A hook worth watching", description: str | None = "why"
) -> Clip:
    return Clip(
        id="clip-1",
        uid="user-1",
        candidate_id="cand-1",
        location=ClipLocation.LOCAL,
        local_path="P:/workspace/clips/clip-1.mp4",
        title=title,
        description=description,
        review=ReviewState.APPROVED,
        created_at=NOW,
    )


def make_channel(**overrides: object) -> Channel:
    defaults: dict[str, object] = {
        "privacy": PublishPrivacy.UNLISTED,
        "category_id": "22",
        "tags": [],
        "title_suffix": None,
        "description_template": None,
    }
    return Channel(
        id="youtube-primary",
        uid="worker-1",
        platform=PublishPlatform.YOUTUBE,
        label="YouTube",
        connection=ChannelConnection.CONNECTED,
        defaults=PublishDefaults.model_validate(defaults | overrides),
        created_at=NOW,
    )


def resolve(
    *,
    clip: Clip | None = None,
    options: PublishOptions | None = None,
    channel: Channel | None = None,
    default_privacy: str = "unlisted",
) -> PublishMetadata:
    return resolve_metadata(
        clip=clip or make_clip(),
        options=options,
        channel=channel,
        default_privacy=default_privacy,
    )


# ── Precedence ───────────────────────────────────────────────────────────────


def test_a_publish_with_no_options_at_all_still_resolves_sensibly() -> None:
    """The floor. A publish requested by tapping one button must work."""
    resolved = resolve()

    assert resolved.title == "A hook worth watching"
    assert resolved.description == "why"
    assert resolved.privacy is PublishPrivacy.UNLISTED
    assert resolved.category_id == "22"
    assert resolved.tags == []
    assert resolved.channel_id is None


def test_per_publish_choices_beat_the_channel_defaults() -> None:
    resolved = resolve(
        options=PublishOptions(
            title="Different title for this one",
            description="and a different description",
            privacy=PublishPrivacy.PUBLIC,
            category_id="27",
            tags=["one", "two"],
        ),
        channel=make_channel(privacy=PublishPrivacy.PRIVATE, category_id="10", tags=["standing"]),
    )

    assert resolved.title == "Different title for this one"
    assert resolved.description == "and a different description"
    assert resolved.privacy is PublishPrivacy.PUBLIC
    assert resolved.category_id == "27"
    assert resolved.tags == ["one", "two"]


def test_channel_defaults_beat_the_install_default() -> None:
    resolved = resolve(
        channel=make_channel(privacy=PublishPrivacy.PRIVATE, category_id="28"),
        default_privacy="public",
    )

    assert resolved.privacy is PublishPrivacy.PRIVATE
    assert resolved.category_id == "28"


def test_unset_fields_fall_through_one_layer_at_a_time() -> None:
    """A partly-filled options block must not blank out the rest.

    This is the shape the UI actually produces: the operator opens the panel,
    changes privacy, and leaves everything else alone.
    """
    resolved = resolve(
        options=PublishOptions(privacy=PublishPrivacy.PUBLIC),
        channel=make_channel(category_id="27", tags=["series"]),
    )

    assert resolved.privacy is PublishPrivacy.PUBLIC
    # Untouched by the options block, so the channel still decides.
    assert resolved.category_id == "27"
    assert resolved.tags == ["series"]
    assert resolved.title == "A hook worth watching"


def test_an_empty_tag_list_means_no_tags_not_use_the_channels() -> None:
    """`[]` and `None` are different answers and must stay different.

    "Publish this one without the channel's standing tags" is a real thing to
    want, and a resolver that treated an empty list as absent could not express
    it at all.
    """
    resolved = resolve(options=PublishOptions(tags=[]), channel=make_channel(tags=["standing"]))

    assert resolved.tags == []


def test_a_whitespace_only_title_falls_through_rather_than_publishing_a_blank() -> None:
    resolved = resolve(options=PublishOptions(title="   "))

    assert resolved.title == "A hook worth watching"


def test_a_clip_with_no_title_at_all_gets_a_usable_one() -> None:
    resolved = resolve(clip=make_clip(title=None, description=None))

    assert resolved.title == "Clip"
    assert resolved.description == ""


# ── The channel this went to ─────────────────────────────────────────────────


def test_the_resolved_channel_is_recorded_even_when_it_was_not_chosen() -> None:
    """The publication says where it went, not merely where it was asked to go."""
    resolved = resolve(channel=make_channel())

    assert resolved.channel_id == "youtube-primary"


def test_an_explicitly_chosen_channel_wins() -> None:
    resolved = resolve(
        options=PublishOptions(channel_id="youtube-cooking"),
        channel=make_channel(),
    )

    assert resolved.channel_id == "youtube-cooking"


# ── The title suffix, and the 100-character limit ────────────────────────────


def test_the_title_suffix_is_appended() -> None:
    resolved = resolve(channel=make_channel(title_suffix="#shorts"))

    assert resolved.title == "A hook worth watching #shorts"


def test_the_suffix_survives_a_title_that_needs_trimming() -> None:
    """The point of the whole exercise.

    A naive append-then-truncate loses the suffix on exactly the titles that are
    working hardest, which is the opposite of what a series marker is for.
    """
    long_title = "why " * 40  # 160 characters
    resolved = resolve(clip=make_clip(title=long_title), channel=make_channel(title_suffix="#ep12"))

    assert len(resolved.title) <= MAX_TITLE
    assert resolved.title.endswith("#ep12")


def test_a_trimmed_title_does_not_end_mid_word() -> None:
    resolved = resolve(
        clip=make_clip(title="developers " * 12),  # 132 characters, spaces throughout
        channel=make_channel(),
    )

    assert len(resolved.title) <= MAX_TITLE
    assert resolved.title.endswith("developers")


def test_a_suffix_already_present_is_not_appended_twice() -> None:
    resolved = resolve(
        options=PublishOptions(title="Edited from last time #shorts"),
        channel=make_channel(title_suffix="#shorts"),
    )

    assert resolved.title == "Edited from last time #shorts"


def test_a_title_with_no_spaces_is_hard_cut_rather_than_emptied() -> None:
    resolved = resolve(clip=make_clip(title="x" * 200))

    assert len(resolved.title) == MAX_TITLE


# ── The description template ─────────────────────────────────────────────────


def test_the_description_template_is_appended_below_a_blank_line() -> None:
    resolved = resolve(
        clip=make_clip(description="The clip's own words."),
        channel=make_channel(description_template="Filmed by me. Links: example.com"),
    )

    assert resolved.description == "The clip's own words.\n\nFilmed by me. Links: example.com"


def test_the_template_alone_is_used_when_the_clip_says_nothing() -> None:
    resolved = resolve(
        clip=make_clip(description=None),
        channel=make_channel(description_template="Standing credit."),
    )

    assert resolved.description == "Standing credit."


def test_a_template_already_in_the_description_is_not_repeated() -> None:
    """A per-publish description edited from a previous one keeps its credit.

    Appending a second copy of the licence block is the sort of thing nobody
    notices until it is on the channel.
    """
    resolved = resolve(
        options=PublishOptions(description="New words.\n\nStanding credit."),
        channel=make_channel(description_template="Standing credit."),
    )

    assert resolved.description == "New words.\n\nStanding credit."


def test_tags_are_trimmed_and_capped_at_youtubes_limit() -> None:
    resolved = resolve(
        options=PublishOptions(tags=[" spaced ", "", "kept", *[f"t{i}" for i in range(30)]])
    )

    assert resolved.tags[0] == "spaced"
    assert "" not in resolved.tags
    assert len(resolved.tags) == 20
