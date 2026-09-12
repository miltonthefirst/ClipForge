# ADR-0013 — A reviewer's corrections are a job, and produce a new clip

- **Status:** Accepted
- **Date:** 2026-09-12
- **Phase:** Between 8 and 9
- **Extends:** [ADR-0007](0007-checkpointed-stage-pipeline.md) (the stage contract),
  decision D10 in [docs/PLAN.md](../PLAN.md) (`COMPOSE` is a new job type, not extra `CLIP` stages)

## Context

Until now the only things a reviewer could do to a finished clip were approve
it, reject it, retitle it, score it with music, and publish it. Every one of
those accepts the clip as rendered. There was **no way to say a clip was made
wrong and ask for a better one** — the feedback channel simply did not exist.

Two complaints made that untenable, both from real use on football footage.

**The framing loses the subject.** `RENDER` took a single 9:16 window, anchored
`centre`, `left` or `right` by the render profile, and held it for the clip's
whole duration. On a 1920-wide broadcast frame that window keeps 608 pixels —
measured at 31.6% of the source width in
`tests/integration/test_framing_renders.py` — and it never moves. A ball leaves
it several times a minute. Worse, `crop` was a field of the *profile*, and the
profile is one global setting (`CLIPFORGE_RENDER_PROFILE`), so it was not even a
per-clip choice: changing it for one football clip changed it for every talking
head as well.

Nothing in the system had ever looked at pixels. Selection reads the transcript
and picks *when* (`clipforge/analysis/prompts.py`); nothing picked *where*.

**The voice cannot be changed.** No speech synthesis, no translation. This was
planned for M6 and gated behind M5, which put it a long way off.

## Decision

**A correction is a `REMAKE` job, and it produces a new clip.**

Three consequences, in the order they matter.

### It is a job type, not a stage of `CLIP`

The same reasoning as `PUBLISH` and `MUSIC`, and the same as D10 gives for
`COMPOSE`. A job's `stages` array is written at creation and is authoritative
for its whole life; adding a `REMAKE` stage to `CLIP` would give every harvest
job a stage it can never run. And a correction begins on the far side of a human
watching something — which may be hours or days after the job that made it
finished, and that job cannot be reopened.

### It produces a new clip rather than editing one

`derivedFromClipId` relates the two, exactly as it already does for a scored
clip. The clip that was reviewed is the clip that was reviewed: a remake is a
second opinion, not an erratum, and the reviewer may well prefer the original
once they see the alternative. That only stays possible if both exist. The new
clip is always `PENDING` — the approval belonged to the clip it was given to.

### Framing becomes a per-clip decision with three answers

There is no single right answer, so the contract offers the three that are
actually different, and the review screen says what each trades away:

| Mode | What it does | What it costs |
| --- | --- | --- |
| `AS_RENDERED` | The fixed window, now overridable per clip | Nothing; it is today's behaviour |
| `FIT` | The whole frame scaled into the canvas, dead space filled | A smaller picture |
| `PAN` | A window moved between points the reviewer set | The reviewer's time, per clip |
| `TRACK` | The same, with the points found in the footage | A heuristic that can follow the wrong thing |

`TRACK` and `PAN` emit the identical structure — a list of `PanKeyframe` — and
that is deliberate. A tracked clip records the path it followed, so correcting a
tracker that chased a substitute warming up means *editing its answer* rather
than describing the whole path from nothing.

## What TRACK actually measures, and what it does not

**It does not detect a ball.** It measures motion and puts the window where the
most of it is. Stating that plainly here because it predicts the failure modes,
and because "tracking" invites a stronger reading than the implementation earns.

Two details make it work better than that description suggests, and both were
found by measurement rather than reasoning:

- **The window, not the centroid.** A motion centroid sits uselessly near 50% for
  most of a match — a camera pan moves the whole frame, and anything symmetrical
  pulls it to the middle. Scoring every candidate *window position* by how much
  motion it contains answers the question the crop actually poses. Where several
  positions tie — which is most frames, since the action is usually narrower
  than the window — the one nearest the motion's centre of mass wins. Taking the
  first, as `argmax` does, pinned a stationary subject to the edge of frame.
- **Quiet frames hold position rather than voting.** Football stops constantly,
  and a tracker that recentres on every pause spends the match sliding back to
  the halfway line and then chasing play again.

No new dependency: ffmpeg decodes a 160×90 greyscale copy to a pipe and numpy
does the arithmetic. A real detector would be better and would cost a model, a
VRAM budget and a place in the broker's queue. `plan_track` is the seam it goes
behind if that ever pays — its output is keyframes, and nothing downstream knows
how they were arrived at.

## Speech: Kokoro on ONNX, and what re-voicing is not for

The `kokoro` package on PyPI depends on PyTorch, which this project
deliberately does not have
([ADR-0002](0002-ctranslate2-without-pytorch.md)). Adding ~2.5 GB of torch
wheels for an 82M-parameter model that runs on a CPU would undo that decision
for the smallest model in the system. `kokoro-onnx` is the same Apache-2.0
weights on onnxruntime.

Translation reuses the Ollama model that `ANALYZE` already talks to. No second
model class.

**Worth recording because it is the wrong reason to reach for this:**
re-voicing changes the soundtrack and nothing else. On third-party footage the
picture is still the picture, and it is the picture a rights holder's matching
runs against — football rights holders most of all. This helps with a claim on
commentary or music, and it opens a clip to an audience that does not speak the
original language. **It does not make footage safe to publish.** That is what
the rights attestation from [Phase 8](../PLAN.md) is for, and the review screen
says so where the option is offered.

## Two rules that keep the note-reading honest

The reviewer's note is put to the local model, which fills in options they left
unset. Two properties make that safe enough to have on by default:

- **A stated setting is never overruled.** Enforced in `apply_interpretation`,
  not requested in the prompt — a prompt is a request and this needs to be a
  guarantee.
- **Only the declared topics are read.** `LlmRemakeNote` makes the model name
  what the note is *about* before it answers anything, and settings outside
  those topics are discarded.

That second rule exists because of a measured failure, and the shape of
`LlmRemakeNote` is the record of it. With nullable optional fields, qwen3.5:4b
writes a summary saying it chose `TRACK` and then emits null for the mode — null
always satisfies the schema, so it is always the easy path. With every field
required instead, it fills all of them, inventing a crop anchor and a language
for a note about timing; a remake that silently reframes a clip nobody
complained about is worse than one that does nothing. Declaring scope first
fixes both, and it is a question the model answers reliably.
`tests/gpu/test_remake_notes.py` pins all of it.

Whatever it decided is recorded on the clip as a `NoteInterpretation`. A remake
that came out wrong is nearly always either a misread note or a correctly-read
note badly executed, and without the reading written down those are
indistinguishable afterwards.

## Consequences

**Reframing needs the source; re-voicing does not.** A rendered clip has already
thrown the discarded pixels away, so any framing change re-cuts from the
original — which the workspace collector reclaims. Clips outlive sources, so
this fails on older clips, and it says so and names the alternative rather than
raising. Swapping the audio copies the video stream untouched, so a voice-only
remake costs seconds and works long after the source is gone.

**The picture is never retimed to fit the narration.** A translated script
routinely runs 20-30% longer than the original. Stretching the video or moving
the out-point would mean the published clip is not the clip anyone approved, so
an overrun is reported on the clip and shown in the UI, and the reviewer decides.

**`RENDER` and `REMAKE` share one crop implementation.**
`clipforge.media.render.build_filtergraph` now delegates to
`clipforge.media.framing` with no framing request, which is exactly the fixed
window it built before. Two crop expressions that are supposed to agree are two
crop expressions that will eventually not.

**One more thing that can fail late.** A remake is `maxAttempts: 1`, like
`MUSIC`: every failure is a property of its inputs and a retry reproduces it
exactly while spending the render time twice.

## What a review of this ADR's own implementation changed

Written down because three of the four were invisible in a rendered clip, and
all four were found by reading rather than by watching output.

- **The window was not clamped to the frame.** A full-height 9:16 window is
  `ih * 0.5625` wide, which exceeds the picture on any source *taller* than
  9:16 — 18:9 and 20:9 phone footage. ffmpeg refuses an oversized crop outright,
  so every PAN and TRACK remake of such a source failed at the render while
  AS_RENDERED and FIT of the same source succeeded. The clamp now lives in
  `_window`, which also made the special-case branch for tall sources redundant:
  the width becomes the full frame, the height falls out as `iw / 0.5625`, and
  the result is centred. `tests/unit/test_framing.py` asserts the geometry for
  six source shapes and the integration tier renders all of them.

- **The tracker scored a window 1.78x too wide.** `TARGET_RATIO` is a
  height-to-width ratio, and it was being multiplied by the analysis width as
  though it were a fraction of width — with the source aspect missing from the
  arithmetic entirely. The crop keeps 31.6% of a 16:9 frame; the tracker was
  scoring 56.25%, so it under-panned and could never frame the outer eighth of
  the picture at either edge. Action on the left touchline resolved to 28% when
  it should have been 16%. `plan_track` now takes the source aspect as a
  required argument, because the analysis image is force-scaled to 160x90 and
  nothing downstream can recover it. The unit test had baked in the same wrong
  constant, so it agreed with the bug and passed.

- **Nudges did not compose, and could truncate the picture.** `_cut` always
  resolved through `candidateId` — which is copied onto every derived clip — so
  remaking a remake silently discarded the previous trim. In the voice-only
  path, where the video stream is copied rather than re-cut, the mix was capped
  at the recomputed window, so a clip that had been *lengthened* was cut back
  and the extra footage was gone from the published result. `_cut` now prefers
  the parent clip's own recorded window, which is what `AppliedRemake.startSec`
  and `endSec` were being written for, and the mix takes its length from the
  picture it was handed.

- **The model could choose a mode it cannot specify.** A pan is defined by its
  keyframes and `LlmRemakeNote` gives the model no way to supply any, so an
  answer of PAN could only ever produce an empty keyframe list — which the
  render path refuses, turning a perfectly readable note into a failed job.
  `NoteFraming` no longer offers it. TRACK is the executable form of the same
  intent.

Two smaller ones worth the same treatment: the "never raises" error path in
`interpret_note` could itself raise, because it interpolated an untruncated
Ollama error into a field the contract caps at 600 characters — and the likeliest
error to arrive there carries a whole validation report. And a VRAM shortfall
while reading the *optional* note aborted the entire stage; it now degrades, and
records that it did, while the same shortfall during translation still refuses,
because speaking one language's words with another's phonetics is a wrong result
rather than a degraded one.

## What the first five real remakes changed

All five completed successfully. None was usable. They are worth listing
individually because the common cause is not what it looks like:

| Note | What came out |
| --- | --- |
| *"change voice to English and follow the ball"* | A voice reading `"Here is a nice musical instrumental for you. [Instrumental music plays here] Thank you."` — Whisper's boilerplate from music-only audio, stage direction included |
| *"change **commentary voice to English**"* | Spanish |
| (no note, English requested) | *"Very bad beauty glim this pure left lateral munitions shot"* |
| (no note, English requested) | French |
| (feedback typed in the script box) | A voice reading *"Cut the Canal plus caption or watermark in the upper right corner."* |

Not one of these is a component doing the wrong thing. Whisper produced its
documented failure output for speechless audio; the translator gave a defensible
reading of unpunctuated ASR text; the synthesiser said what it was given; the
form supplied the value it held. **Every stage did its job, and there was no
predicate anywhere between a bad input and a finished artefact.** A pipeline of
individually reasonable steps with no gate between them produces confident
nonsense, and it does it at the speed of the fastest step.

So the response is a layer whose entire purpose is to refuse:
`clipforge.analysis.feedback`. Its checks are deliberately **deterministic and
outside the model** — a model cannot be the thing that decides whether to trust
a model's output. It refuses recogniser boilerplate, refuses a script that reads
like an instruction rather than narration, refuses a window with too few words,
and warns when the recogniser's own confidence was low. `tests/unit/test_feedback.py`
pins each of the five strings above.

Three companion changes, each a direct consequence:

- **A translation now has to prove it translated.** Given lowercase ASR French
  and asked for English, the local model restores punctuation and returns the
  same language. It satisfies the schema, so the clip was recorded as
  translated. `translation_landed` compares normalised text and rejects a result
  too similar to its input.
- **The note beats the form on language.** The old rule — never override
  anything the reviewer stated — sounds right and is wrong, because a form
  cannot tell a choice from a default. A note saying *"commentary voice to
  English"* lost to a dropdown nobody had touched. The note is written last,
  while watching the clip, and is the more specific statement; the disagreement
  is now recorded and shown rather than resolved silently.
- **A remake of a re-voiced clip starts from what that clip says**, via
  `AppliedVoice.spokenText`, rather than resolving back through `candidateId` to
  the footage. The lineage used to reset every generation, so translating "this
  clip" translated something the reviewer had already replaced.

## Saying no is a feature

`UnsupportedAsk` gives the reading layer somewhere to put a request it
understood and cannot meet — remove a watermark, change the music, zoom on a
person, slow motion — and `AppliedRemake.refusals` carries the wording to the
review screen.

This closes the worst hole in the original design. A reviewer who asks for a
watermark to be removed and gets back a clip with the watermark still on it
cannot distinguish *refused* from *misunderstood* from *broken*, and all three
call for a different next move. Being told is also the only honest answer,
because the request was perfectly clear.

It is worth the schema real estate for a second reason: the list is written by
the person who wanted the feature, at the moment they wanted it, which makes it
a better backlog than any amount of speculation. Two of the five notes above
asked for something outside every control the system has.

The model is now also told, in the prompt, not to bend an impossible request
into the nearest control that exists — it had been answering *"remove the
watermark in the top right"* with a framing change, which produces a clip that
is differently wrong and still has the watermark. A deterministic guard backs
this up: a crop anchor is only accepted when the note actually names a side, and
only for the one framing mode that has a fixed window. The model volunteered
`crop: right` on notes about following the ball, and it reached two real clips'
recorded summaries as a change that had never been applied.

## Superseded in one respect

A remake still produces a new clip and still never alters the one that was
reviewed — but it no longer occupies its own row in the review queue. A clip and
every correction of it share a `lineageId` and are one thing to decide about,
with the history a click away. See [ADR-0014](0014-learning-from-feedback.md),
which also adds the loop that learns from the corrections this ADR introduced.

## Alternatives considered

**Make the render profile per-clip and stop there.** Cheapest by far, and it
fixes the "crop is global" half of the complaint. It does not fix the half that
matters: a still window in a different place is still a still window, and the
ball still leaves it.

**A real object detector (YOLO or similar) for tracking.** Better answers,
certainly. It is a model, a VRAM budget and a third broker-managed class, for a
stage that currently runs on the CPU lane and competes with nothing. Deferred
behind `plan_track`, which is the seam it would go behind.

**Let the reviewer scrub and crop by hand, only.** Exact, never wrong, and
unusable at the volume this project targets — it is per-clip work on a phone.
Kept as `PAN`, for the cases where the automatic answers are wrong, and
seedable from what `TRACK` decided so it starts from something.
