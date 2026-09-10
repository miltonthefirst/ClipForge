# Connecting a YouTube channel

Every field ClipForge needs, where it comes from, and why it lives where it does.

Budget about fifteen minutes the first time. Most of it is waiting for Google
Cloud to enable an API.

---

## Where each value lives, and why

This is the part worth understanding before you start, because it explains why
some fields are typed in the app and others are not.

| Value | Lives | Reachable from a phone? |
| --- | --- | --- |
| OAuth **client id** | Worker machine, `.clipforge/youtube-client.json` | ✗ |
| OAuth **client secret** | Same file | ✗ |
| **Refresh token** | Worker machine, encrypted (`youtube-token.enc`) | ✗ |
| Channel label, defaults, privacy, tags, category | Firestore, `channels/{id}` | ✓ |
| Connection state, quota, last error | Firestore, written by the worker | ✓ (read-only) |

The split is deliberate and is [ADR-0010](adr/0010-worker-held-publishing-credentials.md):
a refresh token can upload to *and delete from* your channel indefinitely, and
Firestore is a shared, replicated, remotely-readable store whose security rules
the worker's Admin SDK bypasses anyway. So the credential stays on the machine,
and what travels is a description of it.

You still type it into a form rather than a terminal. The desktop app runs on the
same machine as the worker and hands the secret over on loopback
([ADR-0011](adr/0011-local-control-api.md)) — it goes from the form to a file on
that machine and no further.

**This means connecting a channel is something you do at the desktop, not from
your phone.** Everything afterwards — approving clips, choosing privacy, editing
titles, publishing — works from anywhere.

---

## 1. Create a Google Cloud project

Any project will do, including the one Firebase made for you
(`bytepic-clipforge`). Using the Firebase one keeps everything in one place;
using a separate one keeps YouTube quota away from anything else. Either is fine.

→ <https://console.cloud.google.com/projectcreate>

## 2. Enable the YouTube Data API v3

→ <https://console.cloud.google.com/apis/library/youtube.googleapis.com>

Select your project, press **Enable**. This is the step that occasionally takes a
few minutes to propagate; if step 5 fails with "API not enabled", wait and retry
rather than assuming something is wrong.

## 3. Configure the OAuth consent screen

→ **APIs & Services → OAuth consent screen**

| Field | What to put | Notes |
| --- | --- | --- |
| User type | **External** | "Internal" needs a Google Workspace organisation |
| App name | `ClipForge` | Shown on the consent screen you will see in step 6 |
| User support email | Your email | Required; not shown publicly |
| Developer contact | Your email | Same |
| Scopes | Leave empty here | ClipForge requests them at run time |
| Test users | **Add your own Google account** | Without this, sign-in fails with `access_denied` |

Leave it in **Testing**. Publishing the consent screen means Google verification,
which the upload scope triggers because it is *sensitive*.

> **The 7-day catch.** While the consent screen is in Testing, Google expires
> refresh tokens after seven days. ClipForge is built for that: `doctor` reports
> the token's age, a rejected refresh says exactly which command to re-run, and
> re-authorising takes about ten seconds. It is an annoyance, not a blocker.

## 4. Create the OAuth client

→ **APIs & Services → Credentials → Create credentials → OAuth client ID**

| Field | Value |
| --- | --- |
| Application type | **Desktop app** |
| Name | `ClipForge worker` (only you see this) |

Press create. You get two values:

- **Client ID** — ends in `.apps.googleusercontent.com`
- **Client secret** — a shorter opaque string

Keep the tab open, or download the JSON. Either works.

> Desktop app, not Web application. A Desktop client permits the loopback
> redirect ClipForge uses (`http://127.0.0.1:8766/`) without registering redirect
> URIs by hand. If you pick Web application you must add that URI yourself, and
> the mismatch surfaces as `redirect_uri_mismatch` at the last step.

## 5. Give the worker the client

Two equivalent routes — they write the same file, so downstream nothing knows
which you used.

**From the desktop app** (Settings → YouTube): paste the client id and secret,
press Save. It travels over loopback to the worker on this machine.

**From a terminal**, if you downloaded the JSON:

```dotenv
CLIPFORGE_YOUTUBE_CLIENT_SECRETS=C:/Users/you/.clipforge/youtube-client.json
CLIPFORGE_PUBLISHING_ENABLED=true
```

## 6. Authorise

```bash
uv run --project apps/worker clipforge-worker youtube-auth
```

A browser opens, you pick the Google account that owns the channel, and you will
see an "unverified app" warning — that is your own app in Testing mode. Choose
**Advanced → Go to ClipForge**.

The redirect lands on `http://127.0.0.1:8766/`, the worker exchanges the code,
and the refresh token is written encrypted to `youtube-token.enc`.

## 7. Check it

```bash
uv run --project apps/worker clipforge-worker quota
uv run --project apps/worker python -m clipforge.diagnostics --skip-gpu
```

`doctor`'s `publishing` line reports connected/not, and the token's age.

---

## Publish settings

Not secret, so these live in Firestore and are editable from anywhere.

| Setting | Default | Notes |
| --- | --- | --- |
| Privacy | `unlisted` | Publishing to the world by accident is not recoverable the way an unlisted upload is — the link may already be scraped |
| Category | `22` (People & Blogs) | See the table below |
| Tags | none | Up to 20; YouTube ignores the rest |
| Title suffix | none | Appended to every title. Titles are still truncated at 100 characters, which YouTube enforces outright |
| Description template | none | Appended to every description — a standing credit, licence note or link block |

Common category ids: `1` Film & Animation · `10` Music · `17` Sport ·
`20` Gaming · `22` People & Blogs · `23` Comedy · `24` Entertainment ·
`25` News & Politics · `26` Howto & Style · `27` Education ·
`28` Science & Technology

---

## Quota, and why it decides your day

The default YouTube Data API allowance is **10,000 units per day** and an upload
costs **1,600** — about **six uploads**, and the ceiling resets at midnight
Pacific.

ClipForge budgets this rather than discovering it: a publish that cannot afford
its quota is refused *before* the upload starts, not after the encode, the review
and your expectation. `clipforge-worker quota` reports what is left.

---

## When it goes wrong

| Symptom | Cause | Fix |
| --- | --- | --- |
| `access_denied` at the consent screen | Your account is not a test user | Add it under OAuth consent screen → Test users |
| `redirect_uri_mismatch` | Client created as *Web application* | Recreate as **Desktop app**, or register `http://127.0.0.1:8766/` |
| `REAUTH_REQUIRED`, or publishing stopped after a week | Testing-mode refresh tokens expire after 7 days | Re-run `clipforge-worker youtube-auth` |
| `QUOTA_EXCEEDED` | More than ~6 uploads today | Wait for midnight Pacific, or request more quota |
| "API not enabled" | Step 2 has not propagated | Wait a few minutes and retry |
| Settings → YouTube cannot save the client | The worker is not running, or you are not on the desktop app | Start the worker; connect from the desktop app |

---

## Adding more channels

The data model is a `channels/{channelId}` collection from the outset, and a
publish records which channel it went to — so a second channel is a document
plus a second authorisation, not a migration.

The UI manages one today. See **Phase 12** in [PLAN.md](PLAN.md) for what the
rest involves: per-channel token files, a channel picker at publish time, and
per-channel quota tracking, since the 10,000 units are counted per Google Cloud
project rather than per channel.
