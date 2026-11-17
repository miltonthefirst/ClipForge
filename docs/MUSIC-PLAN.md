# Generated music: our own tracks, made on this PC

> ClipForge writes its own music on the local GPU: beds under clips and composed
> videos first, then tracks published to the connected YouTube channel as videos
> of their own. Every track is ours to use, so nothing on the channel is claimed
> by someone else's Content ID.

**Status:** planned 2026-11-15, from the owner's ask: "a feature for generating
music for our youtube channel that's already connected". The owner's answers the
same day: standalone tracks are wanted ("all that can make more views possible"),
and the M1 spike is approved. **M1 ran 2026-11-15: it fits, and it is fast** (§3,
M1 results). The first slice of M2 is built and live: the library
(`tracks/{id}`, `clipforge-worker import-tracks`) and a **Music page** that plays
each track from the PC (over the home wifi on a phone, through the loopback file
server in the desktop app) and records Keep or Discard. The twelve M1 tracks are
in it, waiting on the owner's verdict. Generating from the app (the job, the
broker class, the package proper) is the rest of M2.

**2026-11-17: "Make a track" is built (M2 and M5 together).** A
`GENERATE_MUSIC` job, from a form on the Music page: the words come from an
idea, a trend, the owner's notes, or none (an instrumental). LYRICS has the
local model write a title and lyrics (`clipforge/synth/lyrics.py`), SING runs
`packages/ngoma/generate.py` in its own process and imports each take as a NEW
track with its lyrics and where it came from. Vocals on the 6 GB tier were
spiked the same day (§3, M5 results).

## 1. Why

- **Today a track comes from YouTube.** The `MUSIC` job downloads a track with
  yt-dlp (`media/music.py`, `media/sources.py`). Scoring a clip with someone
  else's song is how a channel collects Content ID claims, and it is the one
  input to a published clip that ClipForge cannot vouch for.
- **The plan already allowed for this.** PLAN.md (synthesis track, "Generated
  music") marked generated music as licence-gated: MusicGen is out because its
  output is CC BY-NC, and Apache or MIT generators are in (D14). Its stated
  zero-risk default was a curated library, and a generator *is* that library,
  filled on demand.

## 2. Decisions (taken 2026-11-15, open to revision)

| Question | Decision | Why |
|---|---|---|
| What the ask means | **Both uses, in this order.** (1) Beds under clips and composed videos. (2) Tracks published as their own videos, full length and as Shorts. | The owner wants whatever brings more views (2026-11-15). (1) comes first because it reuses the existing `MUSIC` job. (2) is approved, inside the guardrails in §5. |
| Model | **ACE-Step 1.5**, the standard model (not XL). | MIT code and weights; the model card permits commercial use of what it makes. The standard model runs in under 4 GB of VRAM with offloading, inside the 5.4 GB budget (PLAN.md §2.1). The 4B XL needs at least 12 GB, so it is out. The licence is re-read for the exact checkpoint at install time and recorded on every track. |
| Where it runs | **Its own package and its own venv** (torch), started as a subprocess per batch. | ADR-0002 keeps torch out of the worker. A process that exits at the end of each batch also releases the GPU for certain, which is what the ModelBroker verifies through NVML. Sarungano's `izwi` package runs the same way. |
| GPU | A **fourth broker model class**, exclusive, on the GPU lane. | It cannot share the card with Whisper or the LLM. |
| Package | `packages/ngoma` ("drum" in Shona), **a working name**. No imports from `clipforge`. | It is built to be lifted out, like Sarungano's `izwi` and `ona`, and named in the same style. Sarungano's chapter videos have no music today and would be the second user. |
| Vocals | **Instrumental by default.** Lyrics are an explicit option. | Lyrics double the ways a track can go wrong, and most beds want none. |
| Imitation | **Prompts naming an artist, band or song are refused.** | The same line as "no likeness of real people" for the drawn videos. A style is described ("warm lo-fi, 80 bpm, Rhodes"), never attributed. |
| Disclosure | The uploader gains **`containsSyntheticMedia`**, set to true for any video carrying a generated track. | The field exists on `videos.insert` and `videos.update` (`status.containsSyntheticMedia`) and ClipForge never sets it today. YouTube lists AI-generated music among the content to disclose, and disclosure does not by itself limit monetisation. |
| Auto-publish | **No** (D15 stands). | A person approves every upload, a standalone track included. |

## 3. What gets built

### Phase M1: spike (half a day)

- Install ACE-Step 1.5 in `packages/ngoma/.venv`.
- On the RTX 3050, measure peak VRAM (NVML), wall time and RAM for 30 s, 60 s
  and 180 s instrumentals, cold and warm.
- Listen to five prompts that fit the channel.
- Record the numbers in this document and decide the length cap from them.
- **Stop here if** peak VRAM leaves no room under 5.4 GB with the desktop open,
  or if quality is not usable. Fallback: a curated CC0 or royalty-free library
  behind the same Track contract.

#### M1 results (2026-11-15)

ACE-Step 1.5 at commit `ca1e85f`, the 2B turbo DiT only (no LM), INT8
weight-only with `torch.compile`, full CPU offload, 8 steps, instrumental, one
seed. RTX 3050 6 GB, torch 2.7.1+cu128, Windows 11. Card baseline 624 MiB
(desktop plus an idle ComfyUI). Raw numbers: `packages/ngoma/out/measurements.jsonl`.

| Length | Generate (warm) | Faster than real time | torch peak | Card peak | RAM peak, fresh process |
|---|---|---|---|---|---|
| 30 s | 7.4 to 10.7 s | 2.8x to 4.1x | 4.6 to 4.8 GB | 5.4 to 5.6 GB | 11.4 GB |
| 60 s | 11.8 to 12.2 s | about 5x | 4.9 GB | 5.7 GB | not measured alone |
| 180 s | 24.2 to 26.4 s | about 7x | 4.9 GB | 5.7 GB | 11.5 GB |

Model load: 2 to 4 s. All five prompts at 30 s and 60 s, and two at 180 s, made audio.

What this settles:

- **It fits, with little to spare.** The card peaks around 5.7 of 6.1 GB, the
  §2.1 budget almost exactly. It must run alone on the GPU lane: ComfyUI or
  Ollama holding the card at the same time would not fit, so the broker's
  exclusivity is required, not optional.
- **INT8 with `torch.compile` works on Windows.** The plain-offload fallback
  was not needed.
- **One process per batch, then exit.** Eleven tracks in one process grew
  peak RAM from 11 GB to 23 GB, and the twelfth (180 s) died with a
  segmentation fault while offloading. The same 180 s track in a fresh process
  peaked at 11.5 GB and finished in 26 s. This was already the plan for
  releasing the GPU; it is now also the fix for memory. A batch is capped at a
  few tracks.
- **Length is not the constraint.** A 3-minute track takes under half a
  minute, so full-length uploads (M4) are cheap.
- **ACE-Step's start-up check wants the 1.7B LM**, and downloads the whole
  10 GB repository when it is missing. The spike replaces that one check with
  the three parts it loads and sets `HF_HUB_OFFLINE=1`; `ngoma` must do the
  same.

Still open: **quality, by ear.** The tracks are in `packages/ngoma/out/`
(`int8-30s/`, `int8-60s/`, `int8-180s/`, one per prompt; `measurements.jsonl`
maps each file to its prompt). M2 starts once the owner says they are usable.

### Phase M2: the generator and the library

- **`packages/ngoma`:** `generate(prompt, seconds, bpm?, key?, seed, lyrics?)` →
  WAV plus a sidecar JSON holding the model, checkpoint hash, licence, prompt,
  seed and timings. A CLI entry, and the package's own tests (no GPU in unit
  tests).
- **Contracts:**
  - a `Track` (id, title, prompt, seconds, bpm, key, instrumental, seed, model,
    licence, status, the file's path on the worker, an optional bucket copy);
  - a `GENERATE_MUSIC` job type, a new job type rather than new stages
    (as D10 did for `COMPOSE`).
- **Worker:**
  - the stage takes the broker lease, runs the package and verifies the release;
  - beats are analysed with the existing `media/beats.py`, so a generated track
    lands on the beat like a downloaded one;
  - loudness is normalised to the clip target (-14 LUFS).
- **Rules:** clients may create a `GENERATE_MUSIC` job and keep, rename or
  discard a track. Nothing else.
- **PWA, a Music page:**
  - generate (mood, tempo, length, instrumental or lyrics, a free prompt with
    the artist rule enforced on both sides);
  - listen (through the home route at home, otherwise the bucket copy);
  - keep or discard; regenerate with the same seed or a new one.

### Phase M3: scoring clips with our own tracks

- `MusicOptions` gains `trackId` as the alternative to a URL. The clip's music
  picker lists kept tracks first.
- The track's provenance goes onto the clip's `synthesis` record (already
  planned in PLAN.md Phase 15).
- When the track is ours, publishing sets `containsSyntheticMedia: true`.

### Phase M4: tracks as videos (approved 2026-11-15)

- A `COMPOSE`-style job turns a kept track into two videos:
  - **full length** (16:9), a drawn loop or a waveform visual from the
    deterministic Pillow renderer in `media/cartoon.py`;
  - **a Shorts cut** (9:16, under 60 s), from the track's strongest section.
    The beat analysis already finds where the pulse lands. Shorts are where a
    new channel's views come from, and ClipForge already renders vertical.
- Both carry the title and a prompt-derived description, and go through the
  same review and `PUBLISH` path, with no new upload code.
- Tracks are grouped into **series** (a mood or theme, a playlist each), so the
  channel reads as curated rather than mass-produced (§5).

**Built 2026-11-17 (0.0.9), M4a.** A `MUSIC_VIDEO` job with one CPU stage,
`VISUALIZE` (`stages/music_video.py`, `media/visualizer.py`). ffmpeg draws the
picture rather than Pillow: an animated radial gradient in the series' colours,
the track's own waveform with a soft glow, and the title, about a minute for a
three-minute track. It makes a 1920x1080 full video and a 1080x1920 Short of the
loudest 55 seconds (RMS), faded and normalised to -14 LUFS. Each becomes a clip
carrying `trackId`, PENDING review; publishing declares `containsSyntheticMedia`
for any clip with a `trackId`. The rules let a client start one only for a
KEPT track. The Music page has a "Make videos" button on each kept track.

**Still to do, M4b: playlists per series.** The channel was authorised with
`youtube.upload` and `youtube.readonly`; creating playlists and adding to them
needs the `youtube` scope, so the channel has to be authorised once more on the
YouTube page first. Tracks already carry `series` for it.

### Phase M5: lyrics, from a trend or from notes (asked 2026-11-17)

The owner's asks: songs with lyrics written from what is trending, "so that it
becomes interesting", and, later, songs made from lecture notes.

- **One writer, two sources.** The local model (`qwen3.5:4b`, already brokered)
  writes lyrics in ACE-Step's tagged form (`[verse]`, `[chorus]`, `[bridge]`)
  from either a trend (its topic and angle, from `trends/{id}`) or text the
  owner pastes (lecture notes). Then a style prompt, as now, and the track is
  generated with vocals.
- **Lecture notes:** a study song. The writer is told to keep the facts
  right and make the chorus the thing to remember; the notes stay on the PC.
- **Guardrails, from the trend side:** no real person's name or likeness in a
  lyric (the drawn-video rule), nothing defamatory, no quoting another song's
  words. A trend about a person is written about the event, not the person.
- **The open risk: vocals on the 6 GB tier.** M1 measured instrumentals only,
  with no LM. ACE-Step's DiT does take lyrics without the LM, but vocal quality
  at INT8 on this card is unmeasured. A spike first (a few lyric songs, judged
  by ear on the Music page, as M1 was); if vocals are poor, the fallback is the
  0.6B LM in the 6-8 GB tier settings, measured for memory.
- **Where it lands:** the same library and the same Keep/Discard, then the same
  Make videos. A lyric video can show the words in time later; the first
  version uses the M4 look.

#### M5 results (2026-11-17)

Four lyric songs, 150 s each, written by hand to ACE-Step's lyric guide
(`vendor/ACE-Step-1.5/docs/en/Tutorial.md`, "About Lyrics"): an amapiano song
("Sunday Light") and a photosynthesis study song ("Turning Light Into Life"),
two takes each with different seeds and voices, no LM, INT8, 8 steps. In the
library for the owner to judge by ear.

- **Speed on an idle card: 16 s for 60 s of vocals**, in line with M1.
- **Sharing the card is ruinous.** The first run started while Sarungano's
  ComfyUI held 2.4 GB for a Flux job: 4.9 GB of torch did not fit, spilled
  into shared memory, and each song took 8 to 13 minutes instead of about 30
  s. So SING waits for ComfyUI's queue to empty, asks it to unload
  (`POST /free`, the call Sarungano makes itself before training), and only
  then takes the broker's lease for `ngoma_vram_mb` (5000). A run past
  `ngoma_timeout_s` (30 min) is killed and retried later.
- **Trends are mostly politics and named people.** The writer is told to
  write the feeling under a headline, never the headline; every capitalised
  word in the trend is checked for in the result, a song that keeps one is
  asked for again once and then refused.

#### Notes as files, and waiting for the card (2026-11-18)

The owner's asks, after the first songs: "I normally get them in pdf, word or
PowerPoint... the word count should not be limited", and, after a song failed
on a busy card, "it should wait for it to be free instead of failing".

- **Files.** The form uploads the notes to `notes/{uid}/{id}/{name}` (PDF,
  .docx, .pptx, .txt, .md, at most 100 MB; storage.rules). Typed notes over
  8,000 characters go up the same way as a text file. LYRICS fetches the file,
  reads the words out of it (`clipforge/synth/notes.py`: pypdf, python-docx
  with tables, python-pptx with speaker notes), and deletes the bucket copy
  once the song is written; a lifecycle rule collects any left after two days.
  Scanned PDFs have no text and say so; old .doc and .ppt say to save as the
  new format.
- **No length limit.** Notes over 9,000 characters are read in pieces of
  9,000; each gives its key facts, and those are cut down to what the song's
  length can teach (6, 10 or 14 points), in rounds if there are many. An
  optional *focus* keeps only one part. Measured: 22,500 words in 17 pieces,
  4.5 minutes, before the song is written.
- **Waiting, not failing.** The failed song asked for 5000 MiB with 4924 in
  budget on a card holding only the desktop: the ask was wrong (now 4600, i.e.
  5300 free, from the 5.2 GB measured). And a busy card is now waited for: the
  stages ask again every 30 s, free an idle ComfyUI each time, show
  "Waiting for the card: X MiB free, Y needed", and give up only after
  `song_gpu_wait_s` (6 hours).

#### Background pictures, and length from the words (2026-11-18)

The owner's asks: "a very nice background image in relation to the context of
the music", and "the duration should be determined by the lyrics... and a max
length as well because sometimes I might need long videos songs".

- **ARTWORK** (GPU lane) ends a GENERATE_MUSIC job and starts a MUSIC_VIDEO
  one when the track has no picture. LYRICS now also returns a *scene* (one
  place, its light, weather, colours and objects; never a person or writing,
  the drawn-videos rule) and an art style; an older track or an instrumental
  has a scene written from its title, sound and words. FLUX.2 klein 4B
  (Apache 2.0) in Sarungano's ComfyUI paints it at 1536x864 and 864x1536
  (measured: 22 s and 17 s), using ona's txt2img graph, copied in
  `clipforge/media/comfy.py`. The model has no negative prompt and naming a
  thing to avoid can draw it, so the scene says only what is there. ComfyUI
  is asked to unload afterwards unless Sarungano has work queued. No ComfyUI:
  no picture, and the video is drawn on the gradient as before.
- **Two lessons from the first real song.** Told "only places and things", the
  model still wrote "children running, adults watching"; so every sentence
  with a person in it is dropped before painting (`people_free`). And
  "album cover art" or "fine art painting" in the prompt drew a logo and a
  signature in a corner; the prompt now asks for the place filling the frame,
  and the video trims 4% off each edge of the picture.
- **The video** drifts the picture slowly (a crop with a moving origin,
  about 3x real time at 1080p), with the wave a quieter, half-height band low
  in the frame and the song's title as the big text. A song's YouTube
  description now carries its lyrics (up to the clip's 2,000 characters) and
  credits the picture.
- **Length.** `maxSeconds` (1 to 8 minutes, default 4) replaces the fixed
  length. The writer aims for it (longer targets get `extended` and `epic`
  shapes with a third and fourth verse and instrumental breaks), and the song
  is made as long as its words take: two bars a sung line, four for an intro
  or outro, eight for a section with no words, at the song's tempo, rounded up
  to 5 s. An instrumental is `maxSeconds` long. **Eight minutes measured:** 67 s
  to make, card peak 5,975 of 6,144 MiB, so 8 is the cap rather than the
  model's own 10.

The writer's prompt (`lyrics-v1`): section tags, six to ten syllables a line,
one image per song, a repeated hook, abbreviations spelled out (A T P), no
real people, brands, songs or sides in politics; a notes song uses only facts
from the notes, accuracy over rhyme.

## 4. What it costs the rest of the system

- GPU time only. 3 to 7 videos a week is a handful of tracks.
- Disk: a WAV at 44.1 kHz is about 10 MB a minute, kept in `workspace/music/`
  under the existing quota and collector.
- A one-off download of the model weights, a few GB, into the package's own
  folder.

## 5. Guardrails for Phase M4

The owner chose reach (2026-11-15). These keep the reach from costing the
channel its monetisation:

- **YouTube's inauthentic-content policy** (renamed from "repetitious content"
  on 2025-07-15) targets mass-produced, templated output. A run of
  similar-sounding generated tracks with the same visual is that pattern. So:
  - each upload is a track the owner kept, not every track made;
  - tracks go out in themed series with their own playlists;
  - the visual varies by series;
  - the cadence stays inside D15's 3 to 7 videos a week, Shorts included.
- **The channel description** (`youtube assets/README.md`) presents ClipForge as
  a build log and clip feed. Update it to mention the music when M4 ships, so
  new viewers know what they're subscribing to.
- **Disclosure stays on** (`containsSyntheticMedia: true`) for every track video.
- **The model card's commercial-use statement** is the developers' claim
  about their own model. It is not a clearance of what the model learned
  from. That is the same residual risk every generator carries. It is
  recorded here so it is a known choice, not a surprise.
