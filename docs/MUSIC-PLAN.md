# Generated music: our own tracks, made on this PC

> ClipForge writes its own music on the local GPU: beds under clips and composed
> videos first, then tracks published to the connected YouTube channel as videos
> of their own. Every track is ours to use, so nothing on the channel is claimed
> by someone else's Content ID.

**Status:** planned 2026-11-15, from the owner's ask: "a feature for generating
music for our youtube channel that's already connected". The owner's answers the
same day: standalone tracks are wanted ("all that can make more views possible"),
and the M1 spike is approved. M1 started 2026-11-15.

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
