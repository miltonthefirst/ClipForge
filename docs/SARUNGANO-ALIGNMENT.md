# ClipForge and Sarungano: what to copy, what to share

Written 2026-11-15. Both projects run on the same PC, publish to YouTube, run
local models on the same 6 GB card, and have a phone app on Firebase that talks
to a Python process on the workstation. Each has built things the other lacks.
This is the list, ranked, with where each one goes.

Shared designs live in `P:/Experiments/blueprints`. A project records which ones
it implements in `.blueprints.json`. ClipForge has none yet.

## 1. Copy from Sarungano into ClipForge

| # | Feature | Sarungano has | ClipForge today | Value | Where it goes |
|---|---|---|---|---|---|
| 1 | **Home wifi route** | Phone talks to the PC directly at home (`app/home/`), live since 2026-11-01 | Phone plays only bucket copies, which need an upload and expire after 5 days | High | `docs/HOME-NETWORK-PLAN.md`; blueprint `home-route` (extracted today) |
| 2 | **Push notifications** | FCM to the phone, each kind switchable per person, quiet hours, a list of what was sent (`app/notify/`, NOTIFICATIONS-PLAN.md) | None. A job that finishes, fails, or sits QUEUED because no worker is running goes unnoticed until someone looks | High | Kinds for ClipForge: job failed; clip ready to review; nothing has claimed a queued job for N minutes; YouTube token needs re-authorising; a scheduled publish went out |
| 3 | **Grouped error monitoring** (logging blueprint 1.2.0) | A root logging handler plus excepthooks feeding grouped errors and a triage page (`app/logs/`) | Structured logs and `~/.clipforge/worker.log`, but no grouping and no page | Medium | Adopt the blueprint. Sarungano's Python adaptation (`app/logs/capture.py`) is the model for the worker and the agent |
| 4 | **Firebase usage meter** | Counts every Firestore read and write by purpose, beside Google's own count (`app/cloud/usage.py`) | Nothing, and ClipForge is on Blaze, so overage is real money | Medium | A Usage card on the Worker page, extended with Storage bytes and egress, which are ClipForge's real cost |
| 5 | **Release script** | Bumps the version, stamps the build, checks the release (`scripts/release.py`, `check-release.mjs`, `stamp-build.mjs`) | `version.py` still says 0.0.2 after 105 commits. The desktop embeds its own PWA copy, so a rules deploy can break it alone | Medium | One `tools/release.ps1` that bumps worker, web and desktop together and refuses to finish without a `tauri build` |
| 6 | **Token encrypted with DPAPI** | Planned in Sarungano (PUBLISH plan, phase 1) | Encrypted with a machine-local key (`publish/credentials.py`) | Low | Swap the key derivation for DPAPI when the YouTube blueprint is written (§2) |

## 2. Make shared: blueprints and packages

| Feature | Both have it? | Proposed form | Notes |
|---|---|---|---|
| **Home route** | Sarungano built, ClipForge planned | **Blueprint `home-route` 1.0.0, done today** | Reference files, the owner's setup, and this PC's port table. Sarungano is recorded as the reference adopter |
| **YouTube publishing** | Yes, both | Blueprint `youtube-publishing` | ClipForge brings PKCE, the worker-held token, the quota check, three-layer defaults and `notBefore` scheduling. Sarungano brings playlists, DPAPI and the private-until-audit notice. Sarungano's planned `tumira` package was never built, so the design should be shared before any code is |
| **Workstation agent and presence** | Yes, both (ClipForge `agent.py`, Sarungano `app/launcher/`; both have `presence.ts`) | Blueprint `workstation-agent` | Start and stop from the phone as a wish; pings instead of timed heartbeats (ADR-0030); a worker's log that outlives its parent |
| **Trend research** | Yes, both (ClipForge `research/`, Sarungano `packages/ongorora` `reach`) | First a blueprint of provider gotchas, then perhaps one package | Hard-won fixes: Reddit JSON returns 403, so use the Atom feed; yt-dlp has no `ytsearchdate`, so use the results URL with `sp=CAMSAggD` |
| **Notifications** | Sarungano only | A blueprint once ClipForge adopts it | Two users is the threshold |
| **Firebase usage** | Sarungano only | A blueprint once ClipForge adopts it | |
| **Music generation** | Neither | Package `ngoma`, standalone from day one | `docs/MUSIC-PLAN.md`. Sarungano's chapter videos would be its second user |
| **Images and video** | Sarungano's `ona` names ClipForge as its intended second user | Not now | ClipForge chose deterministic drawn cartoons (ADR-0027), and the RTX 3050 cannot generate video |

## 3. Already aligned, nothing to do

- **Ports.** Both projects listen on 8766: Sarungano's home route on
  `192.168.100.13`, ClipForge's YouTube OAuth redirect on `127.0.0.1`, only
  during `youtube-auth`. Different addresses, so both bind. Tested 2026-11-15
  while Sarungano's listener was up. The port stays, because it is registered
  on the OAuth client. Every new listener should take a free port from the
  table in the `home-route` blueprint.
- **Machine setup for the home route.** The Private profile, the fixed MAC and
  the router reservation were done once for this PC and serve both projects.

## 4. Suggested order

1. Home route, phase 1 (needs the owner's DNS and DuckDNS steps first).
2. Music, phase M1 spike (can start now, independent of everything else).
3. Notifications (biggest everyday win after those two).
4. Logging blueprint, usage meter, release script.
5. The YouTube publishing and workstation agent blueprints, written when
   either project next changes those features.
