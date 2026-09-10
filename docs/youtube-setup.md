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

### Branding

The **Branding** page asks for links, and it validates them against the
authorised domains rather than merely storing them. They point at
[`apps/site`](../apps/site/README.md), which exists for this:

| Field | Value |
| --- | --- |
| App logo | `apps/site/img/consent-logo-120.png` — square, 120 px, 7 KB |
| Application home page | `https://getclipforge.web.app/` |
| Application privacy policy link | `https://getclipforge.web.app/privacy` |
| Application terms of service link | `https://getclipforge.web.app/terms` |
| Authorised domains | `bytepic-clipforge.firebaseapp.com` **and** `getclipforge.web.app` |
| Developer contact | Your own address — Google notifies you here, and it is not shown publicly |

> **The logo is optional, and not free.** Uploading one puts the app in the
> branding verification queue; leaving it blank does not, and while the consent
> screen is in Testing the logo is not shown to anyone anyway. Add it when you
> submit for verification, not before.

The privacy policy is not boilerplate: the YouTube API Services Developer
Policies require it to name YouTube API Services, link to the YouTube Terms of
Service and the Google Privacy Policy, and tell users they can revoke access at
`myaccount.google.com/permissions`. It does all four, and a verification review
checks for them.

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
| Title suffix | none | Appended to every title. YouTube enforces 100 characters outright, so the suffix reserves its room and the title is what gives way — see below |
| Description template | none | Appended to every description — a standing credit, licence note or link block |

Common category ids: `1` Film & Animation · `10` Music · `17` Sport ·
`20` Gaming · `22` People & Blogs · `23` Comedy · `24` Entertainment ·
`25` News & Politics · `26` Howto & Style · `27` Education ·
`28` Science & Technology

---

## Changing one upload without changing the channel

**Publish → Options**, on the card for the clip. Title, description, privacy,
category, tags and — once there is more than one — which channel. It applies to
that upload and nothing else; the channel's own settings are untouched.

The panel is collapsed by default and the summary line above it states what is
about to happen (`unlisted · People & Blogs · YouTube`), so publishing with the
channel's settings stays one tap and *what those settings are* is still visible
without opening anything. The privacy is repeated on the button itself, because
`public` is the one choice that cannot be taken back.

### Which value wins

Three layers, most specific first:

| Layer | Where it lives | Applies to |
| --- | --- | --- |
| This upload | `jobs/{jobId}.publishOptions`, written when you press Publish | One clip |
| The channel | `channels/{channelId}.defaults` — Settings → YouTube | Every publish to that channel |
| The install | The clip's own title and description; `CLIPFORGE_YOUTUBE_DEFAULT_PRIVACY` | Everything else |

Resolved by `apps/worker/clipforge/publish/metadata.py`, which is the only place
that decides. Two consequences worth knowing:

- **Only what you changed is recorded.** Fields you did not touch stay empty on
  the job, so correcting a channel default later still affects every publish
  that did not override it — including one you scheduled for next week.
- **Once an upload starts, its metadata is fixed.** The publication record is
  written before the first byte moves, and a retry sends what that record says.
  Editing the channel's defaults mid-upload cannot change a video that is
  already going out under the old ones.

### Tags, and the one field where empty means something

The tag box is **prefilled from the channel**. Leaving it alone publishes the
channel's tags; clearing it publishes with **no** tags. That distinction needs
the prefill to exist at all — an always-empty box could not tell "I did not
touch this" apart from "I want none of them", and `PublishOptions.tags`
therefore carries `null` for the first and `[]` for the second.

### The title suffix and the 100-character limit

A channel's `titleSuffix` is reserved *before* the title is trimmed, so a long
hook loses its own words rather than its series marker — `(title + suffix)[:100]`
would cut the suffix off exactly the titles working hardest. The trim prefers a
word boundary when one is close to the cut. If the suffix is already at the end
of the title you typed, it is not added twice.

### What gets recorded

`clips/{clipId}/publications/{pubId}` holds the **resolved** values — title,
description, tags, privacy, category and channel — not the request. "What did we
actually send" is the question that record exists to answer, and a field that
only sometimes reflected the upload would answer nothing. It is readable from
the phone.

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
