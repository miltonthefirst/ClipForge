# Home network: clips play on the phone straight from the PC

> At home, the phone plays a clip from the worker machine's disk over the wifi:
> no `UPLOAD` job, no bucket egress and no 5-day expiry. Away from home nothing
> changes, and the phone uses the bucket copy as it does today (ADR-0018). The
> PC is never reachable from the internet.

**Status:** BUILT 2026-11-15, phases 1 and 2 (§4), following the shared
**`home-route` blueprint 1.1.0** (`P:/Experiments/blueprints/home-route`),
which was extracted that day from Sarungano's home wifi route (live on this PC
since 2026-11-01) and then extended with what ClipForge needed (§5b, media by
id). The owner's setup (§3) was done and checked the same day, and the route
went live at 11:42 UTC: certificate from Let's Encrypt (expires 2027-02-13,
renewed by the agent), agent 0.0.3 serving `https://clipforge-home.bytepic.dev:8768`. The decision
record is [ADR-0031](adr/0031-the-home-wifi-route.md).

Where it lives:

| Part | Where |
| --- | --- |
| Listener, certificate, sign-in, media links | `apps/worker/clipforge/home/` (`listener`, `certificate`, `auth`, `media` from the blueprint) |
| Which file a clip id means, and who may have it | `apps/worker/clipforge/home/clips.py` |
| The thread that keeps it all running | `apps/worker/clipforge/home/service.py`, started by the agent (`agent.py`, `cli.py`) |
| Settings | `CLIPFORGE_HOME_ROUTE_*` and `DUCKDNS_TOKEN` in `.env` |
| The wish and the report | `AgentReport.homeRouteDesired` (admins, by rule) and `AgentReport.home` (the agent) |
| The phone | `apps/web/src/app/core/home-route.ts`, the `home` rung in `core/playback.ts`, the header label, the Worker page card | Read the blueprint's `BLUEPRINT.md` before building:
it holds the setup steps, the contracts and the gotchas. This document covers
only where ClipForge differs.

## 1. Why, for ClipForge

- **Review from the sofa costs an upload today.** The phone can only play a
  clip that has a bucket copy, so every clip reviewed on the phone first needs
  an upload (worker running, Storage write, egress on every play). Copies
  expire after 5 days, so an older clip shows only its poster.
- **On the home wifi none of that is needed.** The file is already on the PC.
- **Commands are not the point here.** Unlike Sarungano, ClipForge's phone
  creates jobs in Firestore, and the worker claims them under a lease
  (ADR-0006). The agent's start and stop wish is already near-instant through
  its listener (ADR-0012). A direct command route would save a second or so on
  work that takes minutes. It is left out (blueprint §5b, media only).

## 2. Decisions (taken 2026-11-15, open to revision)

| Question | Decision | Why |
|---|---|---|
| Which process listens? | **The machine agent** (`clipforge/agent.py`), not the worker. | The agent is always on (logon task). Clips then play while the worker is stopped, and the agent already holds firebase-admin. |
| Name | **`clipforge-home.bytepic.dev`** → `192.168.100.13` | A sibling of `clipforge.bytepic.dev`, not under it, so it never sits next to the Hosting CNAME. |
| DuckDNS | **`clipforge-home.duckdns.org`**, its own name. | DuckDNS keeps one TXT per name, so sharing Sarungano's would let the two renewals overwrite each other. |
| Port | **8768**, on the Wi-Fi address. | 8766 is Sarungano's home route (and ClipForge's loopback OAuth redirect), and 8767 and 8770 are ClipForge's loopback servers. See the blueprint's port table. |
| Who may get a link | A Firebase ID token whose `users/{uid}` is **`APPROVED`**, cached 60 s. | ClipForge's access rule is approval, not email verification (`firestore.rules`, `isApproved`), so a verified email is not required here. |
| Away from home | **The bucket copy stays.** | Sarungano's owner chose "home only" to save cost. ClipForge's phone review must work anywhere, and the copy is short-lived anyway. |
| Switching it on | An **admin-only wish** on the agent document (`homeRouteDesired: ON/OFF`), carried out by the agent. Settings (hostname, address, port) come from `.env`. | The same pattern as starting the worker (ADR-0012). The DuckDNS token stays in `.env` and never reaches Firestore. |
| Where it is advertised | `agents/{agentId}.home = {url, certificateExpiresAt}`, written by the agent. | The rules already make everything on that document read-only to clients except the wish fields, so a phone never probes an address someone typed. |
| New dependency | `fastapi` + `uvicorn` in a new **`home` extra**. | The blueprint's reference listener is FastAPI, and copying it beats rewriting it on `http.server`. `uv sync` must name every installed extra (`--extra gpu --extra home`), or it removes the others. |

## 3. The owner's setup

Done already for this PC (Sarungano, 2026-11-01): the wifi is Private, random
hardware addresses are off, and the router reserves `192.168.100.13`.

Still to do, in the blueprint's §3 order, with these values:

1. **DuckDNS:** add the domain `clipforge-home` under the same account. Put
   the account token in ClipForge's root `.env` as `DUCKDNS_TOKEN=`. It is the
   same token as Sarungano's (one per account), so if it is ever recreated,
   update both `.env` files.
2. **DNS for `bytepic.dev`, at Squarespace** (confirmed by the owner
   2026-11-15; the `ns-cloud-e*.googledomains.com` nameservers are left over
   from Google Domains). Under **Custom records**, add:

   | Host | Type | Data |
   |---|---|---|
   | `clipforge-home` | `A` | `192.168.100.13` |
   | `_acme-challenge.clipforge-home` | `CNAME` | `clipforge-home.duckdns.org` |

   Don't change any other record: they carry `clipforge.bytepic.dev` and email.
3. **Firewall** (the Worker page shows it, ready to copy):
   `New-NetFirewallRule -DisplayName "ClipForge home wifi" -Direction Inbound -Protocol TCP -LocalPort 8768 -Profile Private -RemoteAddress LocalSubnet -Action Allow`
4. **Check** with the blueprint's §3 step 6 commands, using
   `clipforge-home.bytepic.dev`.
5. **Each phone:** allow local network access once, as for Sarungano. Each
   site asks separately.

## 4. What gets built

### Phase 1: clips over the wifi

1. **`clipforge/home/`**: `certificate.py`, `auth.py`, `media.py` and
   `listener.py` copied from the blueprint's `reference/`, with the `ADAPT`
   points set:
   - `SERVICE = "clipforge-home"`;
   - origins `bytepic-clipforge.web.app` and `bytepic-clipforge.firebaseapp.com`;
   - `CLIP_DIR` in `clipforge-home-media`;
   - tools in `tools/lego/` and certificates in `~/.clipforge/home/`, outside
     the repo.

   Plus `settings.py` (from `.env` through `Settings`) and `service.py` (one
   thread: renew daily, keep the listener up, advertise or withdraw).
2. **`POST /media-links {kind, id}`** (blueprint §5b). Kinds `clip`, `poster`
   and `preview`, resolved through the workspace and the clip's own record, and
   confined with the same resolved-path check and suffix allowlist as
   `localserver.py`. A source video is never served.
3. **The agent** starts the service when the wish is ON and stops it on OFF and
   at shutdown, including a lego run in progress (`certificate.cancel`).
4. **Contracts:** `Agent.home` and `Agent.homeRouteDesired`, plus the listener
   state for the card (running, error, certificate expiry, firewall rule present).
   Regenerated for both sides.
5. **Rules:** `homeRouteDesired` is writable by admins only. A rules test
   covers it.
6. **PWA:**
   - `HomeRouteService`, adapted from the blueprint's `route.service.ts`:
     probes only for approved accounts, and only when the agent advertises.
   - `PlaybackService` gains a rung: `playbackUrl` → loopback file server →
     **home route** → bucket → poster.
   - A "Home wifi / Via cloud" label in the header.
7. **Worker page card:** on/off (admins), name, address and port; whether
   `DUCKDNS_TOKEN` is set (never its value); certificate expiry; the firewall
   command and whether the rule exists; and a self-test.
8. **Tests:** the blueprint's §10 checklist, plus the playback ordering, the
   approval check and the workspace confinement of `/media-links`.
9. **Release:** rules deployed with an explicit `--project bytepic-clipforge`,
   then the desktop rebuilt with `tauri build`, because the desktop embeds its
   own copy of the PWA.

### Phase 2: no needless uploads

- While home, the clip page does not offer "Upload for phone".
- A clip whose bucket copy expired says "plays at home" rather than showing
  only its poster.

### Not planned

- Commands over the wifi (see §1).
- Live data over a WebSocket (Sarungano's phase 2). Firestore reads are not
  ClipForge's cost problem.

## 5. Open points

- Whether an approved **non-admin** should see the on/off switch at all, or only
  its state. The plan above shows only the state.
