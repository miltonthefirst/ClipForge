# ADR-0026: What became of a trend

**Status.** Accepted, 2026-09-19.

**Context.** The first real use of the Trends page ended in confusion that the data explains
perfectly and the screens did not. A person pressed *Clip it* on a trend. The button said Queued.
The job ran every stage to completion — ingested a five-minute Euronews interview, transcribed a
thousand words, put seven windows to the model — and the model proposed no candidate, so RENDER
rendered nothing and the job finished COMPLETED. The Review page, which lists clips, listed none.
The job page would have said why: it has had a sentence for "every stage ran and there was nothing
to cut" since Phase 7. But nothing on the Trends page led to the job, and nothing on the job led
back to the trend. The trend's card said *sent*; the queue said nothing; the explanation sat on a
third page nobody was pointed at.

The curator had, in fact, called it: the trend's `worthClipping` was false, and the card said "the
model doubts there is a clip in it". So the outcome was consistent, and consistently invisible.

**Decision.**

*A job remembers the trend it came from.* `Job.trendId` — set by *Clip it* on a `CLIP` job and by
the Compile page on a `COMPILE` job whose basket a trend filled; null on a job somebody pasted. A
reference, not a foreign key: the rules check its shape and nothing else, because deleting a stale
list must not orphan-check every job it once produced, and the worker ignores it entirely. The
Python model gained the field through the usual regeneration, and because the generated models
forbid unknown fields the worker had to be restarted *before* the page that writes it was deployed.

*Every trend has a page.* `/trends/:id` shows what the card shows and what it could not fit — every
signal, every video, the angle — offers the same actions, and below them lists **what became of
it**: each job made from the trend with its live status and progress, the clips each produced with
their review state and a link into Review, and, for a job that completed and made nothing, the
sentence "finished, but nothing was worth cutting" with a link to the job page that explains it.
The card links to the page from its topic and from a one-line summary — "1 running · 2 to
review", or "nothing to cut" — so the state of a trend is legible from the list.

*The queries stay cheap and index-free.* One equality filter finds a trend's jobs; one `in` filter
finds the jobs for every trend in a run, and Firestore's cap of thirty values on `in` is exactly a
run's `maxTrends`; one more `in` finds the clips those jobs made. No `orderBy`, so no composite
index: the page sorts in memory, as the Jobs page already does.

*The jobs list says it too.* A COMPLETED clip job whose RENDER checkpoint lists no clips gets the
same sentence on its card, read off the checkpoint so the list needs no second query. The job
page keeps its fuller explanation.

**Consequences.**

- Jobs created before this carry no `trendId`, so the trend they came from lists nothing under
  "what became of it". The one such job in the live project is the one that prompted this; it is
  linked from the plan rather than back-filled.
- A job's lineage stops at the trend. Remakes, music and publishes of a clip that came from a trend
  are the clip's business and stay on the clip's page; the trend's page shows the clip and where it
  got to in review.
- The actions moved into one service, `TrendActions`, because two pages offering the same four
  verbs would otherwise drift — and because "Queued" has to mean the same thing on both.
