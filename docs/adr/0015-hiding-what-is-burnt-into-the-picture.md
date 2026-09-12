# ADR-0015 — Hiding what is burnt into the picture

- **Status:** Accepted
- **Date:** 2026-09-13
- **Phase:** 8d
- **Extends:** [ADR-0013](0013-remake-as-a-job.md) (the correction channel),
  [ADR-0014](0014-learning-from-feedback.md) (what gets remembered)

## Context

A reviewer asked three times, in three separate remakes, for the Canal+ bug to
be blurred. Every time the note was read correctly, classified as
`UnsupportedAsk.REMOVE_WATERMARK`, and answered with a clip that still had the
logo on it and a sentence explaining that ClipForge could not do that.

The refusal was honest and useless. Broadcast footage has the broadcaster's mark
burnt into it; that is not an unusual property of one video, it is a permanent
property of every video that channel will ever publish. A pipeline that cuts
shorts from broadcast footage and cannot cover a channel bug is a pipeline whose
output cannot be published, which makes this less a missing feature than a
missing floor.

Two of those three remakes then failed outright on an unrelated version skew,
so the reviewer's actual experience was: ask three times, get told no twice and
get an error twice.

## Decision

**Regions of the source frame can be hidden, they are found automatically, and
once found they can become a property of the channel.**

### One rectangle, three ways to get one

`ObscureRegion` is a rectangle in percentages of the **source** frame, and
`ObscureFound` records which of three routes produced it:

- **AUTO** — measured from the footage by `clipforge.media.obscure`.
- **MANUAL** — sent in the request.
- **REMEMBERED** — held against the source and applied to everything cut from
  it.

They are the same kind of thing by the time anything renders, and that is the
design rather than a convenience: correcting a box the detector got wrong is an
ordinary remake carrying a rectangle, not a second code path.

### Source coordinates, in percent

The output is cropped, panned and scaled. A box in output coordinates would have
to be transformed by whatever the framing did, and under TRACK that is a
different transform on every frame — a blurred square chasing a logo that never
moved. So the box is in source coordinates and the hiding happens at the head of
the filtergraph, before the crop. A mark the crop then discards has been hidden
for nothing, which costs one filter and no correctness.

Percentages rather than pixels, so a box drawn against a 1920-wide poster
survives a source that turns out to be 1280 wide.

### Detection keys on stillness, relative to the frame

Nothing here knows what a logo looks like. It measures **what holds still while
the rest of the frame does not**, which for broadcast footage is very nearly the
same question: the camera pans, players run, the crowd moves, and the bug in the
corner does not.

The comparison being *relative* is what makes it safe. A cell qualifies when it
is far stiller than the frame's own median, so on a locked-off shot — where
everything is static — nothing stands out and detection correctly finds nothing
rather than blurring half the picture. It says so in words, because "nothing was
found" and "nothing could be found" call for different next moves.

On the reviewer's own football source it found the Canal+ bug at 0.98 confidence
on one cut and 0.96 on another, plus the score bar and the competition clock,
with no false positives.

## Three things measured rather than reasoned about

**Reconstruction is superb and then suddenly is not.** `delogo` rebuilds a
region from the band of pixels around it, so its quality falls off with distance
from an edge of the box. Over the Canal+ mark on a crowd background it left
nothing to see at all. Over the score bar in the same frame — twenty percent of
the frame wide — it drew a horizontal smear more conspicuous than the graphic
had been. So the limits are on the sides as much as the area, and anything wider
than about a sixth of the frame is blurred instead: honest rather than
invisible, which is the correct trade once invisible is off the table.

**A clock is half a graphic.** A match clock's digits change every second, so
the only static part of it is the competition badge beside them. Detection came
back with a box over the badge and left "34:37" sitting in the open next to a
blurred square, which reads as a fault rather than as a decision. Broadcast
graphics sit on plates and plates sit in bands, so two finds at the same height
with a small gap between them are treated as one plate and the gap is swallowed
— covering whatever was moving in between without having to detect it.

**Pixelation has to average.** The first implementation scaled down with
`flags=neighbor`, which *samples* one pixel per cell rather than averaging it,
so a mosaic over six dark bars on a white plate kept every bar it happened to
land on and the mark stayed perfectly legible. The filtergraph looked right and
the render succeeded. Only a test that measured the *structure left inside the
plate* caught it — which is why the integration fixture is six thin bars rather
than one solid block: a single large block survives any amount of pixelation, so
a fixture built from one measures nothing.

## `Source.obscure` is what makes it stop being a chore

A channel bug is in the same place on every video that channel publishes. A
reviewer who has to ask for it on each clip is doing the system's bookkeeping.
So the rectangles can be promoted onto the **source**, where RENDER reads them
as it cuts — and the next clip arrives clean rather than arriving wrong and
costing a correction.

Two routes reach it, both requiring a human press:

- **Directly**, from the clip page, once a remake has hidden something the
  reviewer is happy with.
- **Through the learning layer**, which proposes it as a `Preference` after any
  remake that hid something new; accepting the proposal writes the source.

The second is the one ADR-0014 argued for, and this is the first preference
whose value is its *coordinates* rather than its sentence. It is therefore also
the only lesson written without consulting the model at all: a 4B asked where a
channel puts its logo produces plausible coordinates, and plausible coordinates
blur the crowd and leave the logo. The numbers come from the detector that
measured them. It follows that this one lesson fires on a remake with **no
note**, unlike the rest of learning — ticking a box and getting three marks
found is exactly the moment to ask whether they should always be hidden, and no
sentence was involved anywhere in it.

## Consequences

**The PWA has no drawing tool, and cannot have one yet.** The regions are
percentages of the source frame, and the app never sees a source frame: media
does not leave the worker (decision D3), and the poster it does see is the
finished 9:16 clip, already cropped out of those coordinates. So the app offers
the asking, the record of what was found, and the keeping. Giving it a real
rectangle editor means giving it a source still to draw on, which is a new blob
and a new field, and is worth doing only if auto-detection turns out to miss.

**Detection does not catch changing burnt-in subtitles.** They are fixed in
position and not in content, so nothing that keys on stillness sees them. That
is exactly the case `Source.obscure` exists for: set the box once, and every
clip from that channel is cut with it already applied.

**The refusal is now the honest one.** `REMOVE_WATERMARK` and
`REMOVE_OVERLAY_TEXT` stay in `UnsupportedAsk` because stored readings contain
them, but they are **absorbed** rather than refused: either one turns into a
request to look for the mark and hide it. That matters more than it sounds,
because all three of the reviewer's real notes came back filed that way, and a
build that only read the new `OBSCURE` topic would have left the feature
unreachable from the sentences people actually write. What remains as a refusal
is "nothing fixed enough to hide was found, and here is why".

**The security rule is written against an evaluation budget.** A Firestore rule
may evaluate a thousand expressions; validating eleven named keys and a typed
range on every field, unrolled over eight regions, exceeded it — denying a
perfectly valid write with an error naming neither the field nor the reason. So
the rule checks the two things only it can check, the **key set** and the
**geometry**, and leaves enums and ranges to the worker's generated model, which
rejects them with a message that says so. The cap is six in the schema, in the
rule and in the worker, and detection never returns more than four.

## Alternatives considered

**Crop the logo out instead of covering it.** Free, and it works whenever the
mark is outside the 9:16 window anyway — which it often is. It fails exactly
when it matters: FIT keeps the whole frame by design, and a bug in the top right
of a wide pitch view is inside the window of any crop that also contains the
play. Cropping to avoid it would be letting the broadcaster choose the framing.

**Ask a vision model where the logo is.** More accurate in principle, and it
costs a model, a VRAM budget and a place in the broker's queue for a stage that
runs on the CPU lane. Measured against the alternative it was not close: the
statistics found the mark at 0.98 confidence with no false positives on real
footage, and `detect_static_regions` is the seam a detector goes behind if that
ever stops being true — its output is a list of rectangles, and nothing
downstream knows how they were arrived at.

**Apply detection on every render, without being asked.** Tempting, since it is
cheap and the answer is nearly always right. It is wrong for ADR-0014's reason:
a clip silently reframed by a rule nobody agreed to looks exactly like a clip
that came out wrong, and the correction loop that should catch it is the loop
that produced it. Detection runs when asked; what runs unasked is only ever a
rectangle a human accepted.
