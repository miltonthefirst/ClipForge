# ADR-0027: Drawn cartoons as the first visual mode

**Status.** Accepted, 2026-09-19.

**Context.** The plan's synthesis track (Phase 13) was gated behind the public release and designed
around stock footage: a `StockProvider` port with Pexels first, generated stills second, video
generation ruled out on a 6 GB card, and avatars and lip-sync ruled out for good. The first day of
real trend research produced the case the track exists for — a trend the curator judged had no
clip in it, on which *Clip it* ran every stage and cut nothing — and the operator asked for videos
to be made for such trends anyway, by the local models, without signing up for a stock API, and
suggested stick figures.

Three facts settled the shape.

1. **Everything but the pictures already existed.** The Kokoro voice, the transcribe-our-own-
   narration alignment trick and the caption builder came from the remake stage; the title card,
   the concat filter, the poster, the blob store and the clip record came from compilations. The
   missing piece was a source of pictures that needs no key.
2. **A 6 GB card cannot generate video and can only just generate stills**, and both would contend
   with Whisper and the LLM for the same memory (the plan's own survey). Drawing on the CPU
   contends with nothing.
3. **A real person's face on a stick figure is the one thing this must not do.** Platforms label
   it manipulated media, it invites takedowns, and it looks cheap — the plan's own judgement about
   avatars.

**Decision.**

*A `COMPOSE` job with five stages* — `SCRIPT` (GPU), `NARRATE` (CPU), `ALIGN` (GPU), `DRAW`
(CPU), `ASSEMBLE` (CPU) — per the plan's D10 and D11. The GPU is held twice, briefly, and the time
goes to the CPU lane, so a drawn video does not queue behind a harvest for the card.

*The model chooses from a vocabulary; it does not describe a picture.* `LlmScriptResponse` is a
title and scenes, each a spoken line, up to three figures with a name, a `StickPose` and a
`StickMood`, up to three `StickProp`s, a `SceneMood` and an optional label. The enums are passed
to Ollama as a format constraint, so a pose the renderer has no code for is refused by the
runtime. A new pose is thirty lines in the renderer and one word in the contract.

*The renderer is procedural and deterministic.* `media/cartoon.py` draws every frame with Pillow
from the scene, the seed and the clock — a walk is legs on a sine, a celebration is arms up on a
bounce — supersampled twice, and pipes raw frames to ffmpeg. The same scene encodes to the same
bytes, which is what makes a composed clip reproducible from `AppliedCompose` and what lets a test
hash a frame. Pillow became a core dependency for it; it is a pure wheel.

*Two prompts, one schema.* Given a topic, an angle and the facts a trend carried, the model
**writes**; given a script a person typed, it only **stages** it, and the person's words are spoken
verbatim whatever it returned. The facts rule is in the prompt: use what is given, say what is not
known, never invent a name, a number or a quote. The curator's doubt about a trend travels as
context the script must respect, not as the angle it must follow.

*No likeness, by construction.* A figure is a head, a face with a mood and a name tag. The name is
a role or a first name from the script. Nothing in the renderer can draw a person, and the clip's
description says so.

*Scenes follow the voice.* `ALIGN` recovers word timings from the narration; each scene starts
where its first word is spoken, by count rather than by text, so a word Whisper misheard costs a
word at a boundary rather than a scene out of step. Without a transcriber the video is still made,
timed by word count, without captions, and the stage says so.

**Consequences.**

- The plan's Phase 13 gate is lifted for this slice only. Stock gathering, generated stills and the
  asset library stay where the plan puts them; the drawn mode is `ComposeStyle.STICK`, an enum with
  one value so a second style is an adapter.
- Drawing a 45-second video is one to two minutes of CPU at 30 fps with 2× supersampling.
  `CLIPFORGE_COMPOSE_FPS` and `CLIPFORGE_COMPOSE_SUPERSAMPLE` trade quality for time.
- A composed clip cannot be remade: there is no source to re-cut. The correction channel is the
  trend's page — make it again with different words, a different voice or a different seed — and
  the clip page says so.
- The scripts a 4B model writes will be plain. The plan names this as Phase 14's risk and its
  hedges (a critic loop, best-of-N, a larger judge on the CPU); none of them is built here. The
  person's own script is the hedge that is.
