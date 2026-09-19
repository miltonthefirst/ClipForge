"""The category catalogue: the invariants the rest of the system leans on.

The catalogue is data in packages/contracts, rendered into both consumers. What
is pinned here is not any one entry but the shape every entry must have: a
code the rules will accept, subreddit names the Reddit provider can put in a
URL, at least one search term for a run with no topics, and no two entries
that would answer to the same code.
"""

from __future__ import annotations

import re
from collections import Counter

import pytest
from clipforge_contracts import CATEGORIES, category_by_code

pytestmark = pytest.mark.unit

# The same patterns the rules and the schema apply. Duplicated on purpose:
# a catalogue entry the rules would refuse is a category nobody can pick.
CODE = re.compile(r"^[a-z0-9-]{1,40}$")
SUBREDDIT = re.compile(r"^[A-Za-z0-9_]{1,40}$")


def test_the_catalogue_is_wide_and_every_code_is_unique() -> None:
    assert len(CATEGORIES) >= 200
    duplicates = [code for code, n in Counter(c.code for c in CATEGORIES).items() if n > 1]
    assert duplicates == []


def test_every_entry_has_the_shape_the_rules_and_providers_need() -> None:
    for entry in CATEGORIES:
        assert CODE.match(entry.code), entry.code
        assert 1 <= len(entry.label) <= 60, entry.code
        assert entry.group, entry.code
        assert 1 <= len(entry.hint) <= 40, entry.code
        assert 1 <= len(entry.terms) <= 3, entry.code
        assert all(1 <= len(term) <= 80 for term in entry.terms), entry.code
        # Bounded the way a run's own subreddit list is, and each one a name
        # the provider can put into a feed URL.
        assert 1 <= len(entry.subreddits) <= 10, entry.code
        assert all(SUBREDDIT.match(name) for name in entry.subreddits), entry.code
        assert all(1 <= len(alias) <= 40 for alias in entry.aliases), entry.code


def test_groups_are_contiguous_so_a_list_reads_in_sections() -> None:
    seen: list[str] = []
    for entry in CATEGORIES:
        if not seen or seen[-1] != entry.group:
            seen.append(entry.group)
    assert len(seen) == len(set(seen)), "a group appears in two places"
    assert len(seen) >= 15


def test_lookup_by_code() -> None:
    football = category_by_code("football")
    assert football is not None
    assert football.label == "Football (soccer)"
    assert "soccer" in football.subreddits
    assert category_by_code("not-a-category") is None
    assert category_by_code(None) is None
    assert category_by_code("") is None
