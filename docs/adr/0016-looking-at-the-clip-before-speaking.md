# ADR-0016 — Looking at the clip before speaking about it

- **Status:** Accepted
- **Date:** 2026-09-13
- **Phase:** 8e
- **Extends:** [ADR-0013](0013-remake-as-a-job.md) (the correction channel)

## Context

A reviewer asked for English over French football commentary and got this, burnt
across the bottom of a finished clip:

> 7,000 flaps go. She is magnificent one and

The chain that produced it was working correctly at every step. Whisper heard
*"très mal à beaude glim cette frappe pure latérale gauche municois"* — ASR on
sports commentary mangles names and invents words. The translator did its job
faithfully and returned *"very bad beauty glim this pure left lateral munitions
shot"*. Kokoro read that aloud. Whisper transcribed the result back for
captions. Each component did what it was asked.

On a second clip the transcript was *good* — *"Pavlovitch, bonne passe, on a
franchi un premier rideau"* — and the translation came back as the same French,
which an English voice pronounced phonetically. `translation_landed` compares
source to result and only catches an unchanged one; re-punctuated French passes.

Meanwhile the clip's title was `"on a franchi un premier rideau kane peut
enroulé du plat"` and its description was *"This segment captures the
high-tension moment where Kane breaks through the defense, creating an immediate
visual hook for football fans."* Neither field was ever written to be read:
`Clip.title` was `Candidate.hook`, defined in the analysis prompt as *"the actual
opening line, quoted from the transcript"*, and `Clip.description` was
`Candidate.reason`, one sentence on why the window was **selected**. Both were
honest about what they held and neither was the thing its name implied.

Nothing in any of this could tell that the words were nonsense, because nothing
in the chain had seen a football.

## Decision

**Look at the clip, then write about it.**

### One look, shared

`clipforge.media.vision` extracts three stills across the cut and asks a
multimodal model what is in them: the subject, what happens, and the text
readable on screen. `mistral-small3.2` was already installed and, given one
frame of the reviewer's own match, returned the sport, the shirt colours, the
scoreline, the clock and the hoarding text. It is 15 GB against 6 GB of VRAM, so
it runs mostly on the CPU and takes about a minute — affordable once per remake,
and the reason the result is computed once and shared by everything downstream
that writes words.

It is description, never inference. Not who is about to score, and — after the
first attempt narrated a goal that may not have been scored — never an outcome
at all. A few stills prove something was attempted; the frames stop, the move
does not.

### Narration is written, not translated

`clipforge.analysis.narrate` replaces the translate step. The model is handed
the transcript **and** the picture description and asked for the spoken line
directly.

The ordering is the fix. A translator's job is fidelity, so given nonsense it
produces faithful nonsense — it has no licence to do otherwise. A writer given
the same nonsense plus a photograph of a pitch has both the licence and the
evidence to write the sentence the commentator was obviously saying.
`LlmNarration.transcriptUsable` is where that licence is granted and it comes
first in the schema, so the judgement is made before the script depending on it.

Measured on both real cuts: the word-salad one became *"A beautiful left-sided
cross from the corner. They're going to review this clearance. It's
magnificent."* and the good one became *"Pavlovitch makes a good pass to break
through the first line. Kane can curl it with his right foot."*

### The language is counted, not asked

`reads_as` scores function words that belong to exactly one of the six languages
a voice exists for. A model is not a witness to its own output — handed French
and asked for English, a 4B returns the French and reports success — so the
check is arithmetic. Overlap had to be removed first: `a` is English and
Portuguese, `on` is English and the commonest French pronoun, and with them
included a French sentence tied with English and was let through.

### A clip gets a name

`clipforge.analysis.metadata` writes the title, description and tags, in the
clip's own language, from what it shows and says. RENDER writes them from the
transcript; REMAKE rewrites them whenever a voice is produced, because a clip
re-voiced into English keeps a French title otherwise and goes to an
English-speaking feed under a line nobody there can read.

The prompt is written for reach: the hook in the first four words because that
is all a feed shows, the first description line carrying it again because it is
the only line most people read, hashtags on their own line, and an explicit ban
on the analyst register the old `reason` field was written in.

## Grounding, because prompting does not fix this

Asked for tags on a Champions League tie, qwen3.5:4b returned `laliga`,
`bayer leipzig`, `bayer leversen`, `lck` and `thiago diaz` — a league that is
not this one, two clubs that do not exist, an esports competition and a player
who is not playing. The prompt forbids every one of them in as many words.

So a tag survives only if **every** word in it is either grounded in the
material the model was shown or is one of a short list of sport-and-format words
that are true whatever else is. Every rather than any: `liverpool vs bayern`
contains one grounded word and names a fixture that never happened, and a tag
naming the wrong match is worse than no tag. The generic list deliberately holds
no competition names, because a competition is precisely what gets invented and
a list of leagues would launder the fabrication.

Prose cannot be filtered the same way without leaving holes in sentences, so the
description is published as written and the words nothing stood behind are
**reported** next to it. A real run produced *"the description mentions Borussia,
Dortmund, Munich — nothing in the clip or its source says so, so check before
publishing"*, on a clip whose opponent was Bodø/Glimt.

## Consequences

**A remake that changes the voice now costs about two minutes** — a vision pass,
a narration call and a metadata call, each taking a lease so they queue rather
than thrash 6 GB between them. A remake that only reframes costs what it did.

**Vision is optional everywhere.** `look` returns None on any failure and every
caller has a path that works without it. `CLIPFORGE_VISION_MODEL=""` turns it
off. Losing the look must never lose the clip.

**RENDER does not look.** A harvest produces a dozen clips and a minute each is
not affordable; the transcript and the source title are enough for a first name,
and the reviewer who corrects a clip gets the grounded version then.

**The model still gets things wrong.** It guessed Borussia Dortmund from "BOD"
on the score bug. What changed is that it no longer does so silently: the tag is
dropped, the description carries a warning naming the words, and the reviewer
can still overrule the title at publish time.

## The bug this shipped with, and what it cost

`_metadata` called `_look` from inside `broker.acquire`. The broker's lock is a
plain `threading.Lock`, so that is a thread waiting for a lock it already holds
— forever.

It looked safe. The narration looks first and `_look` caches, so by the time
metadata is written there is normally nothing to acquire. *Normally.* A clip
that already speaks the language being asked for skips the translation branch
entirely and never looks, and metadata is then the first caller.

That case arrived within the day: a remake of an English clip into English ran
for fifty-five minutes, had its lease expire, was reclaimed by the next worker
and deadlocked in the same place. It never errored and never timed out. From
outside it was a job that was simply always RUNNING.

Two fixes, because the first one alone would leave the trap armed. The call was
hoisted out of the block, and `ModelBroker.acquire` now **raises**
`NestedLeaseError` when the calling thread already holds a lease. "Do not call a
brokered function from inside a brokered block" is an invisible rule about code
somebody else wrote, and it was broken within a day of the broker gaining a
second caller.

## Amended 2026-09-21: accurate, and not worth listening to

Two clips reached Review narrated like this:

> This is a dramatic anime moment with characters showing concern, determination,
> distress, and shock against a dark background.

> After that, how selfish is my brother? [...] Then the scene cuts. We see humans
> lying there.

Both are true. Both are the description this ADR added, above in the same prompt,
read back to somebody already looking at it. The operator's verdict was that the
narration is what makes the video not worth watching, and they were right: a
viewer can see the picture faster than a voice can describe it, so a line that
only describes it is a line that tells them to leave.

The prompt caused it. "If it is not usable, write from the pictures instead.
Describe what is happening as a commentator would" is, to a 4B model, an
instruction to paraphrase the one paragraph of prose in front of it. So the
prompt now says the opposite — write *about* what is happening, never a
description *of* it; open with the most interesting thing inside six words;
never mention the clip, the scene or the camera — with the invention fence
left standing, because asking for stakes is asking a model to make them up.

A prompt alone does not fix this, for the reason the language gate exists: a
model is not a witness to its own output. `echoes_the_picture` counts, the way
`reads_as` counts. Two arithmetic tests — a phrase list for talking about the
footage, and the share of the line's content words that came from the
description — and a line that trips either is sent back once, told exactly what
it did. Clean on the retry, it is used; still reciting, the clip is made anyway
and the reviewer is told the narration is dull. A failed job would be worse
than a boring one.

## Amended 2026-09-21: whose frames were those?

The same two clips arrived carrying an identical warning **twice**, which is how
the second bug was found. `RemakeStage` kept five per-run attributes on `self`
— the notes, the refusals, the corner to search, and the cached look at the
footage — while the worker runs `cpu_lane_depth` jobs at once (three) through a
registry built once. One stage instance, three concurrent remakes, one set of
fields.

The duplicated warning was the harmless half. `_visual` is cached behind
`_looked` so a minute of vision model is paid once per run, and shared, the
second job skipped its own look and narrated **its clip from the other clip's
frames**. Both of these were cuts of the same source, so nothing about the
result looked wrong — which is the only reason it survived this long.

The state moved into a `_Run` record held in a `threading.local`, reset at the
top of `run`. Thread-local rather than an argument threaded through a dozen
private methods: the pool gives each job a thread, and the reset already existed
in the right place. `_note` also dedupes now, because the same sentence twice was
never information.

## Alternatives considered

**Fix the transcription instead.** Whisper `large-v3-turbo` is already the model
in use and the audio is stadium commentary over crowd noise. A better ASR would
raise the floor and would not have caught the second failure at all, where the
transcript was correct and the translation was the problem.

**Refuse to narrate when the transcript looks rough.** Honest, and it makes the
feature useless on exactly the footage it was built for: sport is where ASR is
worst and where re-voicing is most wanted.

**Ask the model to check its own language and its own facts.** Free, and
worthless — a model that has just produced French reports that it produced
English, and one that has just invented a league confirms the league. Both
checks are arithmetic here for that reason.
