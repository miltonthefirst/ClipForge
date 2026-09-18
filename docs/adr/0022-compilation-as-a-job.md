# ADR-0022 — A compilation is a `COMPILE` job, and its clip records every piece

- **Status:** Accepted
- **Date:** 2026-09-19
- **Phase:** 10b
- **Extends:** [ADR-0007](0007-checkpointed-stage-pipeline.md), decision D10 in
  [docs/PLAN.md](../PLAN.md) (a new pipeline is a new job type, not extra `CLIP` stages)

## Context

Every clip ClipForge has ever produced came from one source. The whole data
model says so: a `Clip` has a `candidateId`, a candidate has a `sourceId`, and
`REMAKE` re-cuts *the* source. That was the right shape for harvesting moments
out of long-form video and it is the wrong shape for the thing a trend list
invites — several videos about one thing, and a viewer who wants the best moment
of each rather than all of any.

The synthesis track (docs/PLAN.md, M6) plans an `ASSEMBLE` stage for stitching
generated shots, and its reasoning about a multi-input filtergraph applies here
unchanged. What it does not plan for is the *input* being harvested video: shots
that have to be fetched, transcribed and judged first. That is the `CLIP`
pipeline, folded over several sources.

## Decision

**A compilation is a `COMPILE` job** with three stages, and it produces one
`Clip` that carries an `AppliedCompile` record of every piece.

### It is a job type, not a `CLIP` variant

D10, again: a job's stage list is authoritative for its whole life, and a harvest
must not carry stages it can never run. A compilation is also a different
*intention* — the person had several videos and one theme, not one video and a
hope — and the theme is the thing every stage is told about.

### The three stages reuse the single-source machinery rather than copying it

| Stage | Lane | Does | Through |
| --- | --- | --- | --- |
| `GATHER` | CPU | Ingests every item, keeping its own checkpoint of which landed | `DownloadStage.ingest`, once per item |
| `SELECT` | GPU | Transcribes each source, then chooses one moment per source for the theme under one broker lease | `TranscribeStage.transcribe_source`, then `AnalyzeStage.propose_windows` with the theme in the prompt |
| `ASSEMBLE` | CPU | Renders each moment exactly as `RENDER` would, prepends a card, joins them | `render_clip` per segment, then `media/assemble.py` |

The two single-source stages grew a public method each so that `SELECT` could
call them per source. That is the whole of the refactor, and it is deliberate:
a compilation ingests, transcribes and judges *exactly* as a harvest does, with
one paragraph added to the prompt. Candidates chosen under that paragraph are
stamped `v1-theme` rather than `v1`, so they are never compared against a
harvest's under the same label — a theme changes what gets selected, and Phase
9's calibration must not silently mix the two.

### One moment per source, and the theme decides which

`SELECT` asks for `limit=1` per source with the segment budget as the maximum
duration, and the prompt says what the compilation is about and that a moment
strong on its own but unrelated to the theme should score lower. An item that
arrived with a window skips the model entirely. An item for which nothing stood
out gets the middle of its video and a record saying so — `chosenBy: "fallback"`
— because a compilation with a hole in it is worse than one with an honest
guess, and the honest guess is labelled.

### It tolerates losing a piece, and says which

A compilation of four with one dead link produces three and records the fourth
under `skipped` with the reason. The threshold is two: fewer than that is a clip,
not a compilation, and the job fails saying so rather than rendering something
that is not what was asked for. Where a segment is trimmed to fit the budget,
that is a warning on the clip.

### The result is a clip like any other, with one exception

It enters the review queue, plays on the phone, takes music, uploads and
publishes with no change to any of those stages: they operate on the file.
`candidateId` and `sourceId` name the first segment's, so the queue has a score
to show and nothing downstream has to learn a new shape; the truth is in
`Clip.compile`.

The exception is `REMAKE`. There is no single source to re-cut from, so the
review page says so and points at the Compile page, which is the correction
channel for a compilation: change the pieces and make it again.

### The join is the concat *filter*, and the card is a subtitle

Segments cut from different sources disagree on frame rate and sample rate —
25 fps at 44.1 kHz beside 60 fps at 48 kHz — and the concat demuxer wants them
to agree. The concat filter negotiates a common rate across its inputs on its
own; the one thing it will not do is resize, and every segment is already
1080×1920 because `render_clip` made it so. The integration test generates two
sources that differ in exactly this way and joins them.

The title card is two seconds of the theme as an ASS subtitle over a `color`
source, because libass is a proven dependency on every machine that renders a
caption and `drawtext` is not: it needs a font file path that differs per OS,
and the full ffmpeg build's fontconfig is the thing `doctor` does not check.

## Consequences

**`AppliedCompile` is provenance, not configuration**, in the way
`AppliedRemake` is. It answers *what is in this video and where did each piece
come from* after the job is gone, and it says whose idea each moment was —
which is what makes a compilation that came out wrong correctable rather than
merely regrettable.

**The budget is arithmetic.** The target length is divided evenly among the
items and capped per segment, never below five seconds. Six items into sixty
seconds is ten seconds each. The Compile page shows the division before the
render, so the numbers mean something.

**A retry is cheap.** `GATHER` keeps what it fetched, `TRANSCRIBE` is cached by
content hash, and `SELECT` checkpoints per item — so a second attempt re-asks the
model only about the items it had not reached. `maxAttempts` is 2: a third
attempt would re-run the model over the same inputs.

**Deleting a source deletes no compilation**, because the clip's `sourceId` is
null. The cascade in `deleteSource` keys on that field, and a compilation is
about several sources rather than belonging to one.

## Alternatives considered

**Extra `CLIP` stages behind a flag.** Refused by D10 before the question was
asked. A harvest would carry `GATHER` and `ASSEMBLE` it can never run, and the
pipeline's shape would depend on a field rather than a type.

**A `COMPILE` job that depends on several `CLIP` jobs.** Genuinely elegant —
every source would be a normal harvest, and the compilation would pick from
their candidates — and it needs a dependency between jobs, which the scheduler
does not have and [ADR-0007](0007-checkpointed-stage-pipeline.md) rejected as
"a workflow engine inside Firestore". Folding the pipeline over the sources
inside one job keeps every existing guarantee: one lease, one checkpoint chain,
one document.

**Concatenate with stream copy.** Instant, and requires every input to share
codec parameters, which segments from different sources do not. The re-encode
of a sixty-second vertical clip is seconds on NVENC.

**Crossfades with `xfade`.** Reads as more polished and needs every input's
frame rate to already agree, which is the thing the concat filter is being
asked to fix. Dip-to-black per segment composes with anything and reads as
deliberate where a hard cut between two unrelated shots reads as a glitch. The
transition is an option, and `CUT` is the other.

**Narration over the compilation.** The obvious next feature, and the one that
makes a compilation a *video* rather than a montage. Kokoro is already installed
for `REMAKE`, and `AppliedCompile` has room for it. Left for the synthesis
track, which is where scripts come from.
