# ADR-0019 — Reporting a result we do not have yet

- **Status:** Accepted
- **Date:** 2026-09-14
- **Phase:** 9

## Context

Phase 9 exists to find out whether the scores predicted anything. Everything
before it produces judgements; this is the first part of the system that can say
those judgements were worthless.

It was built against two published clips and no metrics at all. Two clips
reached the channel on 2026-09-11 and 2026-09-12 — that part of Phase 8 is
closed, and this ADR said otherwise for its first few hours because it was
written from the plan's stale record rather than from the database. Both are
**unlisted**, which is the right default and also means near-zero organic
traffic, and YouTube withholds the retention curve entirely below a privacy
threshold of a few hundred views.

That creates a specific hazard, and it is not the obvious one. The obvious risk
is that the machinery is untested until data arrives. The real risk is that when
data does arrive it will be **thin**: two clips now, ten, forty over some
months. Every statistic in this phase produces a number at n=2 exactly as
readily as at n=400, and the number at n=2 looks precisely as convincing.

## Decision

**Sample size gates the claim, not the computation.**

Correlations are always computed and always shown. What changes with `n` is what
the report is willing to say about them. Below `MIN_N_FOR_CONCLUSION` (20) every
interpretation states that the sample cannot support a conclusion, whatever the
coefficient happens to be — including when it is 1.0, which is a case the test
suite asserts directly. Below `MIN_N_FOR_FIT` (40) no weights are fitted at all
and the report says why.

Both thresholds are module constants rather than parameters. Moving them is a
legitimate decision; moving them *after seeing a result* is not, and a parameter
is an invitation to do exactly that.

**Rank statistics throughout.** Views are violently skewed — one clip that gets
picked up does more traffic than the rest together — and Pearson over that
sample reports the outlier as the finding. Spearman asks the question actually
being asked: did the clips we scored highly do better than the ones we scored
poorly. Confidence intervals carry the usual 1.06 inflation on Spearman's
standard error, because ranking discards information and an interval that
ignored that would overstate confidence in the one direction this phase must
not.

**Absent is not zero.** A withheld retention curve, a missing watch percentage,
a clip never polled — each is dropped from the relevant statistic and `n` reports
what actually survived. A report over 40 clips of which 12 have curves says 12
against retention and 40 against views. Zero-filling any of them would
manufacture measurements.

**The report is written to read well when it says nothing.** That is the
expected output for a long time, and the failure mode to design against is a
template that only looks right once it has a finding — because such a template
is read as broken in every month before it does.

**Fitted weights are proposed, never applied.** This follows
[ADR-0014](0014-learning-from-feedback.md) exactly: `calibrate` writes a
`CalibrationReport` and changes nothing. Adopting a weighting is a deliberate
edit to configuration. `rescore` will re-rank every historical candidate under
new weights with no inference at all — the promise decision D5 made in Phase 5 —
but it requires `--apply` and prints the movement first.

**Nothing overwrites the attribution.** `rescore` writes `total` and touches
nothing else. `subScores` are what the model said and `modelVersion` and
`promptVersion` are what said it; a recalibration that overwrote any of them
would destroy the only evidence this phase exists to gather.

## Consequences

The phase can be built, tested and reviewed before it has any data, and the
tests that matter most — that a planted relationship is found, and that planted
noise is reported as noise — run in CI with no account, no network and no
published video.

What is not closed is exit criterion 1: that a published clip accrues daily
snapshots without gaps. It is no longer blocked on publishing, only on a scope.
The stored token holds `youtube.upload` and `youtube.readonly`, so `poll-metrics`
refuses by name until somebody re-runs `youtube-auth` once. Everything on this
side of that is tested, including the gap arithmetic and the refusal to rewrite
a settled day, and `MetricStore.missing_days` exists precisely so the criterion
is answerable rather than asserted.

The cost of the thresholds is that the first several months of reports will say
"we cannot tell yet" in large print. That is the correct output, and a report
that said anything else would be the actual failure.

## The queries were scoped by uid, and real data is what said so

Every analytics query filtered on the reader's own account. This repository had
already made and corrected that exact mistake in the review queue, where the
note reads *"filtering here was what made one system look like two"* — and it
was made again three screens later.

Nothing in the test suite could have caught it, because every fixture used one
uid: the tests shared the bug's assumption. What caught it was reading the live
database, where **the two clips published so far went out under two different
accounts.** A uid-scoped poll would have measured one, skipped the other, and
rendered a dashboard that looked complete.

The rule this project keeps relearning is not about uids. It is that a
workspace-wide system needs workspace-wide queries, and that a test fixture
which only ever constructs one of something cannot tell you what happens with
two.

## Two more things the build surfaced

**Firestore has no date type.** `MetricSnapshot.date` is a calendar day — the
day a channel reported, in its own reporting timezone — and the client raises
`TypeError` on a `datetime.date`. Promoting it to a timestamp would attach a
midnight and a timezone the value does not have. It is converted to its ISO
string at the store boundary, which is what the contract already said it was,
what snapshot ids are built from, and what the range queries compare. The
`datetime` check has to come first, because `datetime` is a `date` and without
it every timestamp in the system would flatten to a day. Only the emulator could
find this; every unit test passed throughout.

**"first" and "last" are not superlatives.** The hook classifier put *"Pavlovitch
breaks the first line"* in the SUPERLATIVE bucket. In football commentary
"first half", "last man" and "the first line" are positional language, and
including those words made a plain description of a pass look like a hyped one.
The words are gone, and the classifier remains English-only — which is a real
limitation now that Phase 8b makes clips narrated in Spanish and French, and the
report says so wherever it prints that breakdown rather than letting it read as
a finding about hooks.
