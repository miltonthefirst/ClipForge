# YouTube channel assets and copy

Everything needed to stand up the ClipForge channel and publish the first video.
The text is paste-ready, and every field that has a limit has been counted
against it.

---

## 1. Files, and where each one goes

| File | Size | Where it goes in YouTube |
| --- | --- | --- |
| `profile-800x800.png` | 800 x 800 | Customisation → Branding → **Picture** (renders as a circle at 98 px) |
| `banner-2560x1440.png` | 2560 x 1440 | Customisation → Branding → **Banner image** — use this one |
| `banner-2048x1152.png` | 2048 x 1152 | Fallback if the larger file is refused. Same design |
| `banner-safe-area-check.png` | 2560 x 1440 | **Not for upload.** Shows the 1546 x 423 phone-safe box in green |
| `watermark-badge-150.png` | 150 x 150 | Customisation → Branding → **Video watermark** — recommended |
| `watermark-white-150.png` | 150 x 150 | Alternative: white mark with a soft dark halo |
| `watermark-gradient-150.png` | 150 x 150 | Alternative: brand gradient, no plate. Over dark footage only |

All PNG, all far under the 6 MB banner and 1 MB watermark ceilings.

**Which watermark.** The badge one. A watermark sits over whatever the video
happens to be showing, and the badge carries its own dark plate, so it survives a
bright frame. The plain white mark disappears against snow, paper or a
whiteboard — the halo helps, but the badge is the one that never fails.

Set **Display time → Entire video** unless the clips are very short, in which
case *End of video* costs nothing and reads as less pushy.

---

## 2. Channel identity

| Field | Value |
| --- | --- |
| Name | `ClipForge` |
| Handle | `@ClipForge`, else `@ClipForgeAI`, `@ClipForgeHQ`, `@clipforge_dev` |
| Country | Wherever you are — it decides which trending and monetisation rules apply |
| Contact email | `support@bytepic.dev` — a public About page is a scraped page, so prefer a role address over a personal one |

Decide the handle before the first upload: changing it later breaks every link
anyone has already shared.

---

## 3. Channel description (About)

YouTube's limit is 1000 characters.

```text
ClipForge is a local-first AI content agent. Long-form video goes in; short vertical clips come out - transcribed, analysed, cut and encoded on one desktop GPU instead of a metered cloud API.

This channel is where that pipeline publishes. Every clip here was chosen by the analysis pass, reviewed from a phone, and uploaded by the worker itself - so the channel doubles as a build log: what the model picked, what it got wrong, and what changed afterwards.

Under the hood: Python, faster-whisper, Ollama, ffmpeg/NVENC, Angular and Firebase, on a 6 GB RTX 3050. MIT licensed.

New clips as the pipeline produces them. Build notes when something breaks.

Discover - Clip - Grow
```

Shorter variant, if the channel should read as a content channel rather than a
project channel:

```text
Short vertical clips, cut from long-form video by a local-first AI pipeline running on one desktop GPU. No cloud transcription, no per-minute billing. Open source: github.com/miltonthefirst/ClipForge
```

Only the first ~100 characters show before "...more" on mobile, so the opening
sentence has to stand alone. Both drafts are written that way.

---

## 4. Channel keywords

Settings → Channel → Basic info → Keywords, 500 characters total. Multi-word
terms need quotes or YouTube splits them at the spaces.

```text
ClipForge, "AI video editing", "local first AI", "self hosted AI", "video clipping", "shorts automation", "vertical video", "YouTube Shorts", "faster whisper", "whisper transcription", Ollama, "local LLM", ffmpeg, NVENC, "GPU video encoding", Python, Angular, Firebase, "open source", "build in public", "content automation", "AI agent", "video pipeline"
```

---

## 5. Links

Up to 14 in Customisation → Basic info; the first ones surface on the banner.

| Label | URL |
| --- | --- |
| Website | `https://getclipforge.web.app` |
| Source code | `https://github.com/miltonthefirst/ClipForge` |
| Bytepic | `https://bytepic.dev` |

---

## 6. Upload defaults — and the one that does nothing

YouTube Studio → Settings → **Upload defaults** applies to uploads made *through
Studio*. ClipForge uploads through the Data API, which sends a complete video
resource and inherits none of it.

So set Studio's defaults for the days you upload something by hand, and set the
real ones in **ClipForge → Settings → YouTube**, which is what the worker reads.

### In ClipForge (Settings → YouTube) — the ones that matter

| Field | Value | Why |
| --- | --- | --- |
| Label | `ClipForge` | Free text; only you see it |
| Privacy | `unlisted` | A public upload cannot be taken back — the link may already be scraped. Promote per clip |
| Category | `28` Science & Technology | `22` People & Blogs is the app's default and the better pick if the clips are talking-head content rather than build content |
| Title suffix | ` · ClipForge` | 12 characters, reserved before the title is trimmed, so a long hook loses its own words rather than the series marker |

Tags — the cap is 20, and 500 characters across all of them:

```text
clipforge, ai video, local ai, shorts, vertical video, video automation, whisper, ollama, ffmpeg, nvenc, python, angular, firebase, open source, build in public
```

Description template, appended to every description, against a 4900 limit:

```text
---
Made with ClipForge, a local-first AI content agent. Transcription, clip selection and encoding run on one desktop GPU; the cloud handles only auth, small state and reaching my phone.

Source (MIT): https://github.com/miltonthefirst/ClipForge

#ClipForge #LocalFirst #AIvideo
```

YouTube promotes the first three hashtags above the title, which is why there are
exactly three and why they are the three worth having.

---

## 7. The first video

Titles, all inside the 100-character limit once ` · ClipForge` is appended:

1. `The first clip my AI picked, cut and uploaded by itself`
2. `I built an AI that clips my videos. This is upload #1`
3. `Long-form in, vertical clip out - on one 6 GB GPU`

Description — the template above gets appended automatically:

```text
This clip was not edited by hand. A local pipeline transcribed the source video, scored every candidate segment, cut this one, encoded it vertically, and pushed it to YouTube once I approved it from my phone.

Nothing was sent to a paid API. Transcription is faster-whisper, analysis is a local model through Ollama, encoding is ffmpeg on an RTX 3050 with 6 GB of VRAM - which is the constraint the whole architecture is built around, since Whisper and the language model cannot both sit on that card at once.

What is coming: the clips the pipeline picks, and the build notes for when it picks badly.

Chapters
0:00 The clip
```

Pinned comment:

```text
Built this because cloud video pipelines bill per minute and per token, and I already own a GPU. Everything expensive runs locally; the cloud only does auth, state and notifications. Code is MIT: github.com/miltonthefirst/ClipForge - happy to answer anything about the pipeline.
```

---

## 8. Everything else worth setting, once

- **Trailer for new viewers** and **featured video for returning subscribers** —
  the first video can be both until there is a second.
- **Sections** on the home tab: `Clips`, `Build log`, `How it works`.
- **Playlists** matching those sections. Cheap now, tedious after 40 uploads.
- **Made for kids: No**, set at the channel level so it stops being asked per
  upload — the API refuses a publish that leaves it unanswered.
- **Comments: hold potentially inappropriate for review**, the default worth
  keeping on a new channel.
- **Two-step verification** on the Google account — it also unlocks custom
  thumbnails, which the channel will want by video two.
- A **1280 x 720 thumbnail** is not generated here; a frame the pipeline already
  produced is a better starting point than anything drawn blind.

---

## 9. How the images were made

Every image here is generated from `logo.png` by
[`tools/brand-assets.py`](../tools/brand-assets.py), which also produces the
favicons, OAuth consent logo and Open Graph card for
[`apps/site`](../apps/site/README.md). Run it from the repository root:

```bash
python tools/brand-assets.py
```

Do not edit the outputs by hand — the next run overwrites them. Two decisions in
there are worth knowing before changing anything:

- **Watermarks** are the film frame, sprocket rail and play triangle drawn at 8x
  and downsampled, so the 150 px file still has clean edges at the ~64 px the
  player actually renders. The full mark's orbit and waveform turn to mush that
  small, which is why small sizes get a simplified mark rather than a scaled one.
- **The banner** composites the logo with `ImageChops.lighter` over the brand
  glows, so its near-black field can never show as a dark rectangle, and every
  text line is measured and shrunk to fit inside the 1546 x 423 safe box rather
  than trusted to fit.
