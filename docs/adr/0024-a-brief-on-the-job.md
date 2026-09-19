# ADR-0024 — A clip job carries a brief, and the brief is a filter

- **Status:** Accepted
- **Date:** 2026-09-19
- **Phase:** between 10 and 12
- **Extends:** [ADR-0013](0013-remake-as-a-job.md) (a reviewer's words reach the
  model), [ADR-0021](0021-trend-research-as-a-job.md) (the model's angle on a trend)

## Context

A `CLIP` job carried a URL and nothing else. What the pipeline looked for, how
many clips it made and how long they were came from constants in the worker:
up to five, fifteen to seventy-five seconds, chosen against a rubric that knows
nothing about the channel. That was right for the first months — the rubric
was the experiment — and wrong the moment somebody wanted *the goals* from a
match and got the pundit's best sentence instead.

The compilation work had already taught `ANALYZE` to take a theme, a count and
a duration budget per call. What was missing was a way for the person
submitting a video to say the same things.

## Decision

**A `CLIP` job may carry `ClipOptions`: instructions, a count, and a length
range.** Null means the pipeline's own judgement with its defaults, which is
every job written before the field existed.

### The brief is a filter, not a hint

The model is told to return **only** moments that fit the instructions, and to
return nothing from a window where nothing does. A job whose brief matches
nothing therefore finishes with zero clips, and the job page says *no moment
matched the brief* rather than leaving an empty queue to explain.

The alternative — bias the selection towards the brief and fall back to the
strongest moment when nothing fits — was refused because it is indistinguishable,
from the review queue, from a brief that was ignored. A person who asked for
the goals and got a press conference cannot tell whether the model disagreed
about what a goal is or never read the request. An empty result with a sentence
on it is a worse outcome and a better signal.

### The brief reads the transcript, not the picture

Selection has always read words and never pixels ([ADR-0013](0013-remake-as-a-job.md)
records that nothing looked at a frame until `TRACK`). "The goals" works because
commentary says *goal*; "where the striker is offside" works only if somebody
says so. The prompt tells the model it cannot see the picture and must not
guess at it, and the job page states the limit beside the brief so an empty
result is explainable rather than mysterious.

### Candidates chosen under a brief are stamped as such

`promptVersion` is `v1-brief` on them, exactly as `v1-theme` marks a
compilation's. A brief changes what gets selected, and Phase 9's calibration
must never compare a filtered set against an unguided one under one label.

### The analysis window follows the longest clip

Windows are 120 seconds with a 30-second stride. A model cannot propose a
150-second moment out of a 120-second window, so when the brief asks for clips
longer than the window holds, the window widens to fit — the stride with it —
and the boundary snapping is unchanged.

### The Trends page writes briefs too

*Clip it* on a trend passes the model's angle — one sentence on what a clip
about this would show — as the brief, when the model thought there was a clip
in it. It is the best instruction anyone has written for that video, and until
now it was read once and lost.

## Consequences

**A count of twenty is twenty renders.** The contract and the rules cap it
there. The default stays five.

**Lengths are five seconds to three minutes**, and the rules refuse a range
that is upside down. The presets on the form are the lengths people mean;
custom is there for the rest.

**A job with an empty brief object is the same as a job with none.** Every
field defaults in the contract, and the form writes null when nothing was
touched, so an untouched panel is indistinguishable from a submission made
before the panel existed.

**Titles still ignore the brief.** `write_metadata` names a clip from its
words and its source; the brief would be a better hint for the title than the
source title is, and it is left for the next pass because a wrong title costs
a retype and a wrong selection costs a render.

## Alternatives considered

**Per-window instructions in the prompt only, with no filter.** See above:
a hint that silently falls back looks like a brief that was ignored.

**A vision pass over the candidates to check the brief against the picture.**
The right answer for "the goals", eventually, and a minute of a 15 GB model per
candidate today. Deferred behind the same seam `REMAKE` uses for its three
frames.

**Put the brief on the source rather than the job.** A channel-level standing
instruction — "this channel is about goals" — is a preference, and preferences
already exist ([ADR-0014](0014-learning-from-feedback.md)). A brief is about
one submission and belongs on it.
