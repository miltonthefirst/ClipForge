# ADR-0025: A category catalogue as data, and a region that may be none

**Status.** Accepted, 2026-09-19.

**Context.** The Trends page asked two things of a research run beyond its topics: where to look,
from a list of fifteen countries in a `<select>`, and — nothing else. Two requests came in the
same breath: many more countries, searchable, and optional; and a category, also searchable,
also optional, "as many as possible".

Three facts shaped the answer.

1. **Google Trends' daily feed is per country and ignores category.** Probed on 2026-09-19:
   `?category=17` returns the same list as no category, an unknown `geo` returns 400, and the feed
   with no `geo` at all returns 400 too. So a category cannot be *asked for* from the one provider
   that knows what is trending, and "nowhere in particular" has to resolve to somewhere.
2. **124 of the 249 ISO 3166-1 codes answer.** Every code was tried; the rest return 400. Offering
   the full ISO list would offer 125 choices that fail the Google provider for the whole run.
3. **What a category can honestly steer is what the worker already parameterises:** which
   subreddits are read (the run's own, else a configured default), what is searched on YouTube (the
   run's own topics, else nothing), and what the curator is told the channel is about.

**Decision.**

*The region is nullable.* `ResearchOptions.region` accepts null, the page offers no default, and the
worker resolves null to a new setting, `CLIPFORGE_RESEARCH_REGION` (US out of the box). The page
offers exactly the 124 countries the feed answered for, with a note in `regions.ts` saying when
that was measured. The stage records the region it actually used on its checkpoint, so a list can
be read back against the question that was really asked.

*A category is a code from a catalogue, and the catalogue is data in the contracts package.*
`packages/contracts/data/categories.json` holds 224 entries in 17 groups. Each is a code (what
travels on `ResearchOptions.category`), a label and group (what the page shows), aliases (what the
page searches), and three things the worker acts on: a `hint` appended to a feed phrase when videos
are looked up for it, `terms` searched on YouTube when the run has no topics, and `subreddits` read
when the run names none. Both generators render it — `categories.ts` and
`clipforge_contracts.categories` — and both `--check` modes compare it, the same discipline
[ADR-0005](0005-single-source-contracts.md) applies to the schema. The rules check only the code's
shape (`^[a-z0-9-]{1,40}$`), and the worker ignores a code it does not know with a warning. That
split is deliberate: the catalogue can grow with a worker restart and a page deploy, and never
needs a rules deploy; a newer page against an older worker still gets its list, just unsteered.

*A category fills in what the run left blank and never overrides what it said.* The run's own
topics are searched and scored exactly as before; its own subreddits are read. The category's terms
are searched only when there are no topics, and they are *not* recorded as matched interests — the
score's "interest match" stays a match against the operator's words. The hint applies to every
feed-phrase lookup regardless, because a feed phrase is nobody's words and "starz" is a network
and a surname. The curator is told the category by label and terms and judges relevance against
topics, category, or both; the prompt is stamped `curate-v2`.

*One searchable combobox, used twice.* `shared/combobox.ts` is the ARIA combobox pattern — a text
box filtering a listbox, arrows, Enter, Escape, a clear cross — with the blur rules pinned by test:
one remaining match is the pick, an exact name is the pick, an erased box means none, and half a
word that matches nothing leaves the previous value alone. It is not a `ControlValueAccessor`; the
pages hold signals, and a value in and an event out is the whole contract.

**Consequences.**

- A schedule or a job written before this carries `region: "US"` explicitly, which still means US.
  One written now with no region means the worker's default, and a worker moved to another country
  moves those runs with it. That is the intended reading of "optional".
- The catalogue's subreddit names were chosen by hand and not all verified live; a subreddit that
  does not exist fails that one feed with a warning and the run carries on, which the Reddit
  provider already guaranteed for a run's own list. A wrong entry is a data fix.
- The catalogue is English, as the feeds are. A category is a hint to English-language search and
  English-language Reddit; it does not localise the run.
- Google Trends is not steered by category at all, so a category on a run with no topics still
  ranks whatever that country is searching for — now with videos looked up in the right corner and
  Reddit read from the right rooms. The honest description on the page is "what it is about", not
  "what to filter to".
