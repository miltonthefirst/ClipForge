😂 **Now we're cooking.** This is actually a fantastic portfolio project because it demonstrates **AI agents + local inference + Firebase + Angular/PWA + APIs + automation + data/analytics**, rather than just another CRUD app.

And two weeks is aggressive but **very achievable for an MVP** if we resist the temptation to build Skynet on day one. 😂

I’d structure it as an open-source project from the beginning, with a clean separation between the **PWA**, **cloud control plane**, and **local worker**.

## 🚀 Project: Local AI Content Agent

Working name for now: **ClipForge**. We can rename it later.

### The core idea

> **A self-hosted AI content agent that discovers trends, analyses videos, identifies high-potential short-form clips, automatically edits them, and learns from publishing performance — while keeping AI processing local.**

---

# 1. Architecture

This is the architecture I'd commit to:

```text
                         ┌──────────────────────┐
                         │       PWA            │
                         │ Angular + Tailwind   │
                         │                      │
                         │ Dashboard            │
                         │ Trends               │
                         │ Videos               │
                         │ Clips                │
                         │ Analytics            │
                         │ Settings             │
                         └──────────┬───────────┘
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │       FIREBASE       │
                         │                      │
                         │ Firebase Auth        │
                         │ Firestore            │
                         │ Storage              │
                         │ FCM Notifications    │
                         └──────────┬───────────┘
                                    │
                          Jobs / state / metadata
                                    │
                                    ▼
                   ┌─────────────────────────────────┐
                   │        LOCAL WORKER             │
                   │                                 │
                   │ Python                          │
                   │                                 │
                   │ ┌─────────┐ ┌───────────────┐ │
                   │ │ Whisper │ │ Local LLM     │ │
                   │ └─────────┘ └───────────────┘ │
                   │                                 │
                   │ ┌─────────┐ ┌───────────────┐ │
                   │ │ OpenCV  │ │ FFmpeg        │ │
                   │ └─────────┘ └───────────────┘ │
                   │                                 │
                   │ RTX 3050 / i9 / 32GB RAM       │
                   └──────────────┬──────────────────┘
                                  │
                    ┌─────────────┼──────────────┐
                    ▼             ▼              ▼
                 YouTube       TikTok        Local Files
```

---

# 2. Don't build everything in two weeks

This is important.

### 🎯 Version 0.1

Your first goal should simply be:

> **YouTube video → AI identifies clips → generates Shorts → I approve them from my phone.**

Nothing more.

No autonomous trend hunting.

No automatic publishing.

No machine learning.

No fancy AI video generation.

Get **that** working first.

---

# 3. MVP feature set

### 📱 PWA

#### Dashboard

```text
Today's jobs
Pending reviews
Generated clips
Published clips
Performance
```

#### Video submission

Paste:

```text
YouTube URL
```

Then:

**Analyze**

---

### 🤖 Local worker

The worker receives the job.

### Step 1

Download video.

### Step 2

Extract audio.

### Step 3

Whisper:

```text
00:00:00 → ...
00:00:04 → ...
00:00:09 → ...
```

### Step 4

Local LLM analyses transcript.

Ask:

> Identify self-contained moments that would make compelling short-form videos.

Return structured JSON:

```json
{
  "clips": [
    {
      "start": 142.4,
      "end": 181.7,
      "score": 91,
      "reason": "Strong hook and clear payoff",
      "hook": "Most developers don't realize..."
    }
  ]
}
```

### Step 5

FFmpeg cuts the clip.

### Step 6

Generate:

* 9:16 version
* captions
* basic title
* description

### Step 7

Upload generated clip to Firebase Storage.

### Step 8

PWA displays it.

You:

**Approve / Reject**

🔥

---

# 4. Week 1

I'd break it down like this.

### Day 1 — Foundation

Repository:

```text
clipforge/
│
├── web/
│   └── Angular PWA
│
├── worker/
│   └── Python local agent
│
├── functions/
│   └── Firebase Functions
│
├── docs/
│
├── .github/
│   ├── workflows/
│   └── ISSUE_TEMPLATE/
│
├── docker/
│
├── LICENSE
├── README.md
└── CONTRIBUTING.md
```

Set up:

* GitHub
* Angular
* Tailwind
* Firebase
* Firestore
* Auth
* Storage
* Python worker

---

### Day 2 — Job system

Create:

```text
jobs/
```

with statuses:

```text
PENDING
PROCESSING
COMPLETED
FAILED
CANCELLED
```

Worker:

```text
Firebase
   ↓
PENDING JOB
   ↓
Worker picks it up
   ↓
PROCESSING
   ↓
COMPLETED
```

This is the **heart of the architecture**.

---

### Day 3 — YouTube ingestion

Build:

```text
YouTube URL
     ↓
Video metadata
     ↓
Download
     ↓
Local workspace
```

Store metadata in Firestore.

---

### Day 4 — Whisper

```text
video
 ↓
audio
 ↓
Whisper
 ↓
timestamped transcript
```

Save transcript.

---

### Day 5 — Local LLM

This is where you start playing with your RTX 3050.

Initially I'd use a **small quantized model**, not something huge.

The LLM receives transcript chunks and returns candidate clips.

---

### Day 6 — Video processing

FFmpeg:

```text
source
 ↓
[start:end]
 ↓
crop 9:16
 ↓
captions
 ↓
output.mp4
```

Don't bother with fancy effects yet.

---

### Day 7 — PWA review screen

You get:

```text
┌────────────────────────────┐
│ Candidate #1               │
│                            │
│        ▶ VIDEO             │
│                            │
│ Score: 91                  │
│                            │
│ "Most developers..."       │
│                            │
│ [ REJECT ]     [ APPROVE ] │
└────────────────────────────┘
```

🎉

At that point you have an actual product.

---

# 5. Week 2

Now make it **good**.

### Day 8 — Better clipping

Add:

* silence detection
* better start/end boundaries
* speaker detection
* automatic framing
* caption styling

---

### Day 9 — Content scoring

Create your first scoring engine.

Something like:

```text
Hook              25
Curiosity         20
Standalone        20
Emotional impact  15
Pacing            10
Shareability      10
---------------------
TOTAL            100
```

---

### Day 10 — Trend discovery

Now introduce:

```text
TREND
 ↓
SEARCH
 ↓
RELATED VIDEOS
 ↓
ANALYSE
```

Initially keep this **manual trigger**.

Don't make it run continuously.

---

### Day 11 — YouTube publishing

Connect your YouTube account.

PWA:

```text
[Approve]

      ↓

[Publish Now]
[Schedule]
```

---

### Day 12 — Analytics

Pull performance data.

Store:

```text
views
likes
comments
watch time
retention
subscribers
```

Now your system has a feedback loop.

---

### Day 13 — Learning

Don't train an AI model yet.

Just calculate:

```text
Average performance by:

hook type
duration
topic
posting time
caption style
clip score
```

This already gives you valuable intelligence.

---

### Day 14 — Polish + open source

This is where your GitHub portfolio starts looking 🔥.

Add:

* README
* architecture diagram
* installation guide
* `.env.example`
* Docker setup
* screenshots
* demo video
* contribution guide
* issue templates
* roadmap

And tag:

```text
v0.1.0
```

---

# 6. Version roadmap

This is where I'd resist scope creep.

### v0.1

**AI Clip Generator**

```text
YouTube
 ↓
Whisper
 ↓
LLM
 ↓
FFmpeg
 ↓
Shorts
```

### v0.2

**Publishing**

```text
             ┌→ YouTube
Short →──────┤
             └→ TikTok
```

### v0.3

**Trend Hunter**

```text
Trends
 ↓
Opportunities
 ↓
Videos
 ↓
Clips
```

### v0.4

**Performance Intelligence**

```text
Published
 ↓
Analytics
 ↓
Insights
 ↓
Strategy
```

### v0.5

**Autonomous Agent**

```text
TREND
 ↓
RESEARCH
 ↓
CREATE
 ↓
SCORE
 ↓
PUBLISH
 ↓
MEASURE
 ↓
LEARN
 ↓
REPEAT
```

😈

---

# 7. The GitHub part

This is actually important for your portfolio.

Don't just dump the source code there.

Make the repository **tell the story**.

Your README should eventually show:

> ### Why?

Cloud AI video processing is expensive.

> ### Solution

A local-first architecture that uses your own hardware for AI inference.

> ### Architecture

PWA → Firebase → Local Worker.

> ### Features

* Local LLM
* Local transcription
* AI clip detection
* Automated editing
* Trend discovery
* Publishing
* Analytics
* Performance feedback

Then:

### Tech Stack

```text
Frontend
Angular
Tailwind
PWA

Cloud
Firebase
Firestore
Storage
Authentication

AI
Local LLM
Whisper
Computer Vision

Media
FFmpeg
OpenCV

Worker
Python

Platforms
YouTube
TikTok
```

That looks **much stronger on a CV/GitHub profile** than:

> "I made an AI that cuts YouTube videos."

😂

---

# 8. One thing I'd change from our earlier discussion

**Don't make Firebase responsible for constantly polling the local PC.**

Instead, eventually give the worker a small local service:

```text
localhost:8765
```

and have a lightweight **worker heartbeat** registered in Firestore.

Something like:

```text
workers/
   my-pc/
      status: ONLINE
      capabilities:
        whisper: true
        llm: true
        video: true
      gpu:
        vram: 6GB
      lastSeen: ...
```

That opens the door later to:

```text
             Firebase
                 │
       ┌─────────┴─────────┐
       ▼                   ▼
    PC #1                 PC #2
   RTX3050              RTX4090
```

😂 **You accidentally built the beginnings of a distributed local-AI worker platform.**

But **don't build that now**.

---

## 🎯 Your actual two-week target

Forget "autonomous viral content company" for the moment.

Your milestone is:

> **Paste a YouTube URL → wait → receive 3 AI-selected vertical Shorts → review them from your phone → approve one → publish it.**

If we get that working in two weeks, **you've won.**

Then we unleash the trend hunter. 😈
