# ADR-0021 — Trend research is a job, its providers sit behind a port, and it decides nothing

- **Status:** Accepted
- **Date:** 2026-09-19
- **Phase:** 10
- **Extends:** [ADR-0007](0007-checkpointed-stage-pipeline.md) (the stage contract),
  [ADR-0013](0013-remake-as-a-job.md) (a job type per human decision)

## Context

Everything ClipForge could do until now started with a URL somebody already had.
The pipeline harvests clips out of video that exists; finding the video was the
operator's job, done in a browser tab beside the app. docs/PLAN.md Phase 10 has
always named the next step — *the system proposes what to work on, with a human
still deciding* — and gated it behind Phase 9 producing evidence that the scoring
was worth automating.

Phase 9 has produced its evidence, and the honest version of it is "we cannot
tell yet" ([ADR-0019](0019-reporting-a-result-we-do-not-have-yet.md)). That
settles the gate in a way the plan did not anticipate: the calibration will not
be able to say anything for months, and the thing it is waiting on is *volume*.
A trend list does not automate the scorer. It feeds it.

Three constraints shaped what was built:

- **No API keys.** An open-source project has to be usable from a clean clone,
  and a feature that needs a Google Cloud project and a Reddit app registration
  before it says anything is a feature most people never turn on.
- **No autonomy.** Phase 10 is explicit that automating an uncalibrated scorer
  produces bad clips faster. Nothing here may run without a press.
- **The scorer becomes an input.** docs/PLAN.md §8 asks that the opportunity
  scorer be kept behind a port, because what it ranks for a person to promote is
  the same signal the synthesis track's `PLAN` stage will later consume, and a
  YouTube-search-only scorer would have to be rewritten for it.

## Decision

**A trend run is a `RESEARCH` job** with two stages, and its result is a list of
`Trend` documents a person acts on.

### It is a job type

The same reasoning as `PUBLISH`, `MUSIC` and `REMAKE`: it begins with a human
pressing a button, it runs on the worker because the worker is the only thing
with the network, the tools and the model, and the queue is already the one
channel that reaches the worker from a phone. It touches no media, so it needs
none of the CLIP stages — and D10's rule that a job's stage list is authoritative
for its whole life means it cannot borrow them either.

### Two stages, split by what they cost

`RESEARCH` runs on the CPU lane. It asks the providers, clusters what they said
by topic, scores each cluster, and writes the list. It is network-bound and
needs no model. `CURATE` runs on the GPU lane and puts each row to the local
model for the one thing arithmetic cannot supply: a sentence saying what a clip
about this would *be*, and a judgement of whether it belongs on a channel about
the run's topics. The split means a run with curation turned off never waits
behind a transcription, and a machine with no model still gets the list — ranked,
just not explained. `CURATE` reports `SKIPPED` rather than failing when Ollama is
not there.

### Providers behind one port

`clipforge.research.signals.TrendProvider` is the port; three adapters implement
it, and each knows a different thing:

| Provider | What it says | What it cannot say | How |
| --- | --- | --- | --- |
| Google Trends | What people are searching for, with an approximate volume | Anything about video | The daily trending-searches RSS feed, per region |
| Reddit | What people are sharing, and very often the video itself | How many upvotes — the feed carries none, so position in the top-of-day list stands in | A subreddit's Atom feed. The JSON API refuses unauthenticated scripts with a 403 (measured, September 2026); the feed does not |
| YouTube | What has been uploaded about a topic, and how fast it is being watched | Upload times, from a flat search — the top few hits are looked up in full to get one | A search URL with the view-count sort and an upload-date filter, through yt-dlp, which is the closest thing to "trending" YouTube still exposes since it removed its Trending page in 2025 |

No single one of these is a trend. **Agreement between them is**, and that is
what the scorer weights most heavily.

### The ranking is arithmetic, and it is explainable by its parts

`clipforge.research.scoring` is pure: the same signals rank the same way every
time, and a row that ranked oddly can be explained by reading the components off
it rather than by asking a model why. Signals are clustered by topic tokens —
Jaccard for the general case, containment for the case the feeds actually
produce, which is a two-word trending phrase inside a twelve-word post title —
and the cluster is scored on how many providers agree (30), whether it is
about something the channel covers (20), how loudly the loudest one said it
(20), how recently (10), whether there is any video at all (10), and how good
the best one looks (10).

Clustering by tokens rather than by a model is a deliberate choice about which
mistakes to make. Two rows that should have been one are two rows, which a
person reading the list will notice and can merge in their head. A model that
merged them for you would also merge the two Arsenal stories that are not the
same story, invisibly. One shared word is therefore never one topic; a two-word
phrase contained in a title is.

Videos are scored separately, on velocity, recency, whether their length is
something the pipeline will accept at all, and total views — with every unknown
scoring in the middle rather than at zero, so a provider that knows less about a
video does not make the video look worse.

### The model's opinion is blended in, not substituted

`CURATE` rewrites `rank` from a blend that keeps most of the weight on the
arithmetic: the model is asked whether a topic belongs on this channel, not
whether it is trending, and it must not be able to promote a dead topic on
enthusiasm alone. A row the model says is not worth clipping is pushed down, not
removed — the person may know better, and the row still says why it sank. The
unblended `score` stays on the document.

Relevance is recorded only when the run named topics. Relevance to nothing is a
number the model made up, and `LlmTrendVerdict` makes every field required for
the reason [ADR-0013](0013-remake-as-a-job.md) records: with nullable fields a
small model writes the summary and emits null for the number.

### A person decides, and the decision is the only thing a person may write

`trends/{trendId}` is worker-written. The score, the rank, the videos and what
the model said are evidence, and a client that could rewrite them could make the
list say anything. The rules let a client change exactly three fields — `status`,
`decidedAt`, `decidedBy` — under its own name, and delete a row. Promoting a
video is submitting it: the Trends page creates an ordinary `CLIP` job from the
URL, which is exactly what the person would have pasted.

## Consequences

**Every run writes its own set.** Keyed by `jobId`, so yesterday's list is still
readable and today's cannot be mistaken for it. A list from Monday is not stale
the way a queue is; the point of asking what was trending on Monday is that
Monday's answer was different.

**The worst case is a known number of requests, not a crawl.** Every axis of
`ResearchOptions` is bounded in the contract and in the rules: twelve topics,
ten videos per topic, thirty trends, a week of lookback. Video lookups are done
for the rows most likely to make the list rather than for every cluster.

**A provider failing is recorded, not fatal.** One feed refusing is noted on the
checkpoint and in the stage's detail; the run fails only when nothing at all
answered, and then retryably, because the failures worth retrying are network
ones.

**The feeds are the fragile part**, and they are treated the way yt-dlp is
treated in [`media/sources.py`](../../apps/worker/clipforge/media/sources.py):
every parser is pinned against a captured response, so a feed that changes shape
fails a unit test rather than a 6am run. The Google Trends namespace URI has
already moved once; the parser matches local tag names for that reason.

**Reddit is the weakest signal by construction.** No upvote count means position
is all it can offer, and a subreddit's top-of-day is fifty posts however quiet
the day was. It earns its place by being the one provider that links the video.

**The first real run rewrote two numbers.** Asked about the Premier League from
GB, it returned one football row and seven American political posts from
r/videos: a position of 1 read as strength 1.0 — louder than a million searches
— and a topic match was worth ten points. Position now tops out at 0.7 and a
match is worth twenty, which is the same as the loudest provider. The same run
showed every Reddit-linked video scoring as "unknown views", below anything a
search returned; the finder now looks those up, a bounded number per row, and
the row still records that Reddit surfaced them.

## Alternatives considered

**The YouTube Data API's `mostPopular` chart.** Real trending data, with view
counts and upload times in one call. It needs an API key, it draws on the same
10,000-unit daily quota publishing already budgets, and the chart is not
searchable — it says what is popular, not what is popular *about the thing this
channel is about*. Deferred behind the port: it is one adapter if a key ever
becomes worth asking for.

**Google Trends' interest-over-time API, via an unofficial client.** Richer than
the RSS feed and breaks whenever Google changes a cookie. The daily feed is a
published artefact with a stable shape.

**Let the model cluster and rank.** It would produce a better-looking list and
an unexplainable one. The value of a ranked list a person is about to act on is
that they can disagree with it, and they cannot disagree with a number that has
no parts.

**Run it on a schedule.** The obvious next step and the one Phase 10 forbids.
Nothing in this ADR prevents it; nothing in this ADR does it. A scheduled run
would need a reason to believe the ranking is worth acting on unattended, and
that is Phase 17's question.
