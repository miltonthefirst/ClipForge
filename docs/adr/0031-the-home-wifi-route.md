# ADR-0031 — The home wifi route: clips play off the PC at home

- **Status:** Accepted
- **Date:** 2026-11-15
- **Related:** [ADR-0009](0009-spark-tier-local-artefacts.md) (the playback precedence this adds a
  rung to), [ADR-0018](0018-blaze-and-a-short-lived-bucket-copy.md) (the bucket copy, still used away
  from home), [ADR-0011](0011-local-control-api.md) (the loopback API this is deliberately not),
  [ADR-0012](0012-machine-agent.md) (the agent that hosts it)
- **Plan:** [docs/HOME-NETWORK-PLAN.md](../HOME-NETWORK-PLAN.md). **Blueprint:** `home-route` 1.1.0
  in `P:/Experiments/blueprints`, drawn from Sarungano's route, which has run on this PC since
  2026-11-01.

## Context

A phone can only play a clip that has a bucket copy. Getting one costs an `UPLOAD` job (so a running
worker), a Storage write, egress on every play, and it expires after five days, after which the
phone shows a poster. Most phone review happens on the same wifi as the PC, where the file is a few
metres away.

A phone's browser cannot simply call the PC: the PWA is served over HTTPS, and every browser refuses
a secure page's request to a plain `http://192.168…` address, with no setting to allow it.

## Decision

The machine agent runs a small HTTPS listener on the PC's Wi-Fi address when an admin switches it
on, and a phone at home plays clips from it.

- **A real certificate for a private address.** `clipforge-home.bytepic.dev` is a public DNS name
  (a Squarespace A record) pointing at `192.168.100.13`, which means nothing outside the house.
  Let's Encrypt issues it a certificate by DNS-01: one CNAME hands `_acme-challenge` to DuckDNS
  (`clipforge-home.duckdns.org`), which lego updates with the token in `.env`. Squarespace has no
  DNS API, so without the handoff every renewal would be manual.
- **Hosted by the agent**, so a clip plays while no worker is running, and its socket dies with the
  process that owns it.
- **Media by id, nothing else.** `GET /hello` (no sign-in) says which PC answered;
  `POST /media-links {kind: "clip", id}` with a Firebase ID token returns a 12-hour link;
  `GET /media/{token}` serves a *copy* of the clip with range requests. No command route: jobs go
  through Firestore as before.
- **Approval is the gate**, as in `firestore.rules`: `users/{uid}.status == APPROVED`, cached for a
  minute. A confirmed email is not required.
- **Only files under the worker's `workspace/clips`**, found through the clip's own `localPath`
  and confined by resolved path. Never a source video.
- **Switched on by admins only.** `homeRouteDesired` on `agents/{id}` is the one wish the rules
  check the role for: starting the worker is any member's call, opening a port on the owner's
  network is the owner's.
- **Advertised by the agent alone.** `agents/{id}.home.url` is set only while the listener serves
  with a production certificate. Clients cannot write `home`, so a phone never probes an address
  someone typed.
- **Playback gains a rung:** `playbackUrl` → loopback file server → **home** → bucket → poster.
  Away from home the probe fails in 1.5 s and the bucket copy is used exactly as before.

## Consequences

- At home, reviewing on a phone needs no upload, no running worker, no egress, and works for clips
  whose bucket copy has expired.
- A new optional dependency (FastAPI and uvicorn, the `home` extra), installed only where the route
  runs.
- The agent now holds an inbound socket on the LAN. It is bound to the Wi-Fi address only, refuses
  any non-private caller, answers CORS only for the app's own Hosting origins, and the Windows
  firewall rule is Private-profile, local-subnet, one port.
- A rules deploy and an agent restart are both needed to take effect, and the desktop app embeds its
  own copy of the PWA, so it needs a `tauri build` to show the card.
- Each phone or browser answers the local-network permission question once.

## Alternatives considered

- **Serve the loopback file server on `0.0.0.0`.** Plain HTTP, so no browser would call it from the
  hosted PWA, and it has no authentication at all.
- **A tunnel (Cloudflare, Tailscale Funnel).** A public endpoint for something that only ever needs
  to work in one house, and a third party in the path of every play.
- **Make the bucket copy permanent.** Pays storage and egress for every clip forever, to avoid
  walking three metres.
- **Sarungano's rule, media only over the home wifi.** Right for Sarungano's costs; wrong here,
  where reviewing from anywhere is the point of the phone.
