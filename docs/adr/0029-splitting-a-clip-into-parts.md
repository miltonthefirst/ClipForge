# ADR-0029: Splitting a clip into parts

**Status.** Accepted, 2026-09-27.

**Context.** A reviewer asked a remake to *"split this into two, 1 video from beginning to 35s then
the 2nd video fro 35s to the end"* on a 51-second clip. No control could express that, so the note
reader used the nearest one. It moved the start 30 seconds earlier, recorded the note as understood,
and one 81-second clip came back. The prompt already told the model not to bend a request into the
nearest control, but with no way to say "split" there was nothing else to do with it. The reviewer
then asked for splitting as a feature, both in plain English and as explicit start and end times.

**Decision.**

*Parts are ranges, not cut points.* `RemakeOptions.splits` is a list of `SplitRange {startSec,
endSec}`. The times are seconds on the clip as the reviewer watches it, because that is the only
timeline they can see. Ranges cover everything cut points can, and more: overlapping parts, a dull
middle left out, or a single range as a precise trim. A part may reach up to 30 seconds past either
end of the clip, the same reach a trim has.

*The finished remake is what gets cut.* The stage applies every other correction first: framing,
captions, voice, the soundtrack and hidden marks. It then cuts the finished file into parts
(`media/split.py`, re-encoded so the cut is frame-exact). Each part therefore keeps everything the
reviewer approved about the whole. A split with nothing else in it copies the reviewed file and
needs only the rendered clip, not the source. The window is widened only when a part reaches
outside the clip, and that case needs the source, which is checked and explained before anything
renders.

*Two or more parts are separate clips.* Each part starts its own lineage (`lineageId` = its own id,
`version` 1). If parts shared the parent's lineage they would be versions of one clip, and the
review queue shows only the newest version of each lineage: the other parts would vanish, and
approving one would supersede the rest. Each part carries `derivedFromClipId` back to the clip it
was split from, `AppliedRemake.split {index, count, startSec, endSec}`, its own source window in
`AppliedRemake.startSec/endSec`, and " (Part i/n)" at the end of its title. The window matters
because a later correction of one part then re-cuts that part and nothing else. **A single part is
the same clip made shorter**: it stays in the lineage as the next version, with no part label.

*The records are cut down to fit.* Keyframes are sliced to the part and re-based to its first frame,
with the position at each edge interpolated. The music record's `musicStartSec` moves by the
part's offset. The narration record drops its spoken text and length, so a later correction of one
part does not re-speak the whole script over a few seconds.

*Parts replace a trim.* Two ways of saying where a clip starts cannot both be honoured. When parts
are set, the stage derives the trim from them. A trim sent alongside is ignored and a warning says
so. The form hides the trim controls while parts are set.

*The note reader has a SPLIT topic and a `splits` answer.* The system prompt now says that
splitting is not trimming. `apply_interpretation` skips any trim the model answers alongside a
split, because it still sometimes files a split under TIMING as well. Parts typed into the form beat
parts read from the note, and the disagreement is recorded. This is the reverse of language, where
the note wins because a form cannot tell a choice from an untouched default; the parts list is
empty until somebody types times into it. The prompt gives the clip's length to one decimal, and the
stage treats an edge within a second outside the clip as the clip's own edge. Otherwise the model
writes "the end" as a rounded 51 on a 50.7-second clip, and the clip would be re-cut from the source
for 0.3 seconds of footage.

Measured against the real model (`tests/gpu/test_remake_notes.py`). The original note comes back as
0 to 35 and 35 to 50.7 with no trim. "Three equal parts" comes back as thirds. "Up to 0:25, then
0:30 to the end" keeps the gap. Timing and framing notes are unaffected.

*Limits.* At most 20 parts, enforced by `firestore.rules` and by the stage. The cap is not a
`maxItems` in the schema, for the same reason keyframes have none there: a bounded array generates a
union of 21 tuple types that the page could not build up one part at a time. Each part must be at
least a second long.

**Consequences.**

- The clip that was split stays in the review queue until someone decides it. Approving a part does
  not supersede it, because the parts are not versions of it.
- `REORDER_OR_CUT_MIDDLE` stays a refusal, now worded to point at splitting. Keeping the pieces
  either side of a cut is possible; joining them back together is not.
- One job can now make many clips, so its outcome records `clipIds` as RENDER's does, as well as
  `clipId` for the first.
- Cutting re-encodes with libx264 at CRF 20, as every other re-encode of a finished clip does. It is
  a second generation, but frame accuracy is the point of the feature.
