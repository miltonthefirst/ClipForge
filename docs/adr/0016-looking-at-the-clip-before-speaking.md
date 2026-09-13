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
