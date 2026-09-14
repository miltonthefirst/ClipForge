# ADR-0014 — The system learns from corrections, and never applies what it learns

- **Status:** Accepted
- **Date:** 2026-09-13
- **Phase:** Between 8b and 9
- **Extends:** [ADR-0013](0013-remake-as-a-job.md) (the correction channel this learns from)
- **Borrows from:** the `sarungano` project's lesson loop, which solves the same
  problem for adapted prose

## Context

A reviewer who writes *"follow the ball"* on every football clip is teaching the
same thing every time. The remake channel reads that note well now, and then
forgets it completely — so the next clip from the same channel arrives framed
exactly the way the last one was wrong, and the reviewer types the sentence
again.

The first five real remakes make the pattern concrete: two of them carried the
same two asks (English narration over a French source, a window that follows
play), written out longhand both times. Nothing in the system was capable of
noticing that.

There is a second, quieter problem. The review queue listed every clip, a remake
is a new clip, and a remake is always `PENDING` — so one football clip corrected
three times occupied four slots, and the reviewer had to work out which was the
newest before they could judge any of them. Thirteen rows in the live queue were
nine actual clips.

## Decision

**Two changes, both about treating a clip and its corrections as one thing.**

### A lineage is one row in the queue

Every clip carries `lineageId` — the id of the clip `RENDER` originally made —
and a `version`. A remake inherits the id and increments the number, so the
chain stays flat however many corrections it takes. The queue groups on it and
shows the highest version present; the clip page offers the rest as history,
with the note, the reading and the refusals recorded at each step.

Grouping happens in the client rather than in the query, and that is deliberate:
the queue is already filtered to one review state, so a rejected attempt is not
in the result set at all, and the highest version present is by construction the
latest one still awaiting a decision. Rejecting a remake therefore brings its
parent back on the next snapshot with no extra bookkeeping and nothing to undo —
which is exactly what happened in practice, when a reviewer rejected one remake
and immediately made another from the same parent.

`MUSIC` joins the same lineage. A scored clip was already linked by
`derivedFromClipId`; joining the lineage is what stops it occupying a second
slot. The original is still never altered and is still one click away.

### What the system learns is proposed, never applied

After a remake that carried a written note, the local model is asked one
question: *if a different clip arrived from this source tomorrow, is there
anything here it should already know?* The answer is stored as a `Preference`
with status `PROPOSED`, and **it does nothing at all until a human accepts it**.

The asymmetry is the entire argument. A wrong standing rule silently reframes
every future clip and the reviewer has no reason to suspect it is there; a
missed one costs one more sentence in a note. Given that, the correct bias is
obvious, and it is the same bias `sarungano` arrived at for the same reason.

An accepted preference does two things. Its sentence goes into the note-reading
prompt as standing context, so a reviewer who has already taught something does
not have to repeat it to be understood. Its optional settings pre-fill the
remake form — but only settings this particular request said nothing about,
because a default that overrides a choice is not a default.

## Three rules that keep it from becoming a nag

- **Never propose the same thing twice.** The model is shown what has been
  accepted *and* what has been rejected, and `dedupe` drops anything already
  held in any status. Deduping against accepted preferences alone would let a
  turned-down suggestion return on every correction of the same kind of clip.
  Rejections are kept rather than deleted for exactly this reason, and the
  security rules forbid deleting one.
- **Scope defaults to the source.** Footage differs more than people do, and a
  rule learned from football should not reframe a talking head. `EVERYTHING` is
  available and is rarer than it feels while writing one.
- **Count what fires.** `timesApplied` says whether a preference is earning its
  place: one that never fires is noise, and one that fires on everything is a
  default the pipeline should adopt outright rather than keep consulting.

The client's write surface is one field wide — the decision, plus who made it
and when. A preference cannot be created from the PWA, its wording cannot be
edited after acceptance (a rule whose text could be rewritten afterwards is a
rule nobody agreed to), and it cannot be deleted.

## The shape of the question is the feature

The first version of `LlmPreferenceProposal` asked only for a list of
preferences. Measured against qwen3.5:4b on four cases — including a note that
plainly taught two things — it returned an empty list **every single time**.

An empty array satisfies an array schema trivially. It is the cheapest answer
available, and a small model takes the cheapest answer. Rewording the prompt
toward encouragement changed nothing, because the problem was never the wording.

Making the model answer `recurring` — a required boolean — and justify it in one
sentence *before* the list exists turns the same model into one that answers
correctly: it learns the language preference from the real note, stays silent on
*"it cuts in three seconds late on this one"*, and re-derives things it already
knows only for `dedupe` to catch them.

This is the second time this exact lesson has been paid for. `LlmRemakeNote`
records the first: nullable fields let the model write a summary claiming it had
chosen `TRACK` and then emit null for the mode. **Where a schema offers a lazy
path that satisfies it, a small model takes that path**, and no amount of
prompting fixes what the shape permits. Both are pinned by tests against the
real model, because a scripted one would have been perfectly happy with either
design.

## Consequences

**Learning runs after the clip exists, never before.** It is a bonus pass over a
finished result: a model that is unreachable, slow or unhelpful costs the lesson
and nothing else, and every failure path returns an empty list rather than
raising.

**It only runs on a remake that carried a written note.** A correction made
entirely with the controls teaches nothing a default could not already express.

**Proposals surface in the review queue, not a settings page.** They arrive
moments after the remake that taught them, while the reviewer is still looking
at the queue and still remembers why they asked. A settings page nobody opens
would make this a feature nobody uses.

## Alternatives considered

**Apply what it learns automatically, and let the reviewer correct it.** This is
what "learning" usually means, and it is wrong here for a reason specific to the
medium: the reviewer would have to *notice* a silent reframe to correct it, and
a clip framed by a rule they never agreed to looks exactly like a clip framed
badly. The correction loop that is supposed to catch it is the same loop that
produced the rule.

**Infer preferences from accepted and rejected clips rather than from notes.**
Tempting, and it needs no model at all — but a rejection says only *no*, not
*why*, and every failure recorded so far had several plausible causes. Phase 9's
calibration work is where that kind of statistical inference belongs, over
published performance rather than over a handful of review decisions.

**One `Preference` document per setting rather than a sentence plus settings.**
Cleaner to apply, and it loses the half that matters: plenty of what a reviewer
teaches is a judgement no control can hold — *"this channel's wide shots are
unusable cropped"* — and it is worth carrying into the prompt even when it fills
in no box.
