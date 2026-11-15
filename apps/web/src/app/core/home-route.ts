import {
  Injectable,
  InjectionToken,
  type Signal,
  computed,
  effect,
  inject,
  signal,
  untracked,
} from '@angular/core';
import type { AgentReport } from '@clipforge/contracts';

import type { Live } from './firestore/gateway';
import { WorkersRepository } from './data/workers';
import { SessionService } from './session';

/**
 * Playing a clip straight off the PC, over the home wifi.
 *
 * The machine agent runs a small HTTPS listener on the PC's Wi-Fi address when
 * an admin switches it on, and says where on its own document (`home.url`,
 * written only by the agent). This service asks that address whether it is
 * there — `GET /hello`, with a short limit — and, while it is, turns "play
 * clip X" into a link the `<video>` element can use. Away from home the address
 * leads nowhere, the check fails in a second and a half, and playback falls
 * through to the bucket copy exactly as before.
 *
 * Adapted from the home-route blueprint's `route.service.ts`
 * (P:/Experiments/blueprints/home-route). docs/HOME-NETWORK-PLAN.md has the
 * whole picture, including the one-time setup on the PC and the router.
 *
 * When it checks: when the app opens, when the phone comes back online, when
 * the app comes back on screen, every five minutes, and at once when the
 * advertised address changes. Each check is one local request and costs no
 * Firestore read: the agent document is already being listened to.
 *
 * Who checks: approved accounts only, and never inside the desktop shell. The
 * shell runs on the PC itself and plays from the loopback file server, which
 * comes first in playback's order anyway; and the listener answers only the
 * hosted app's origins, so a check from the shell would fail and label the PC
 * "Via cloud" while sitting on top of it.
 */

/** Mirrors `SERVICE` in apps/worker/clipforge/home/listener.py. */
export const HOME_SERVICE = 'clipforge-home';

/** On the home wifi the answer is back in milliseconds; anywhere else the
 * address leads nowhere, and this is how long that takes to find out. */
const HELLO_TIMEOUT_MS = 1_500;
const CHECK_EVERY_MS = 5 * 60_000;
/** A link is good for twelve hours; one with less than this left is renewed. */
const LINK_MARGIN_MS = 10 * 60_000;
/** Copying a long clip on the PC takes a moment before the link comes back. */
const LINK_TIMEOUT_MS = 15_000;
/** How long playback waits for the agent document on a cold start. */
const FIRST_DATA_WAIT_MS = 2_000;

/** What the PC will play over the route. Mirrors `KINDS` in clipforge/home/clips.py. */
export type MediaKind = 'clip' | 'track';

/** What playback needs from the route, kept narrow so it can be left out. */
export interface HomeMedia {
  /** A link to play this clip from the PC, or null to use another source. */
  clipUrl(clipId: string): Promise<string | null>;
}

/**
 * Provided in app.config.ts. Optional for {@link PlaybackService}, so a host
 * without the route (and every test that does not care about it) simply skips
 * the rung.
 */
export const HOME_MEDIA = new InjectionToken<HomeMedia>('HOME_MEDIA');

interface Link {
  readonly url: string;
  readonly expires: number;
}

@Injectable({ providedIn: 'root' })
export class HomeRouteService implements HomeMedia {
  private readonly session = inject(SessionService);
  private readonly workers = inject(WorkersRepository);

  private readonly held = signal<Live<AgentReport[]> | null>(null);

  /** The machine advertising a route, freshest first. */
  readonly agent: Signal<AgentReport | null> = computed(
    () =>
      this.held()
        ?.data()
        ?.find((agent) => !!agent.home?.url) ?? null,
  );
  /** Where the PC says it can be reached at home, for an account that would use it. */
  readonly url = computed(() => this.agent()?.home?.url ?? null);

  /** The last check reached the PC directly. */
  readonly home = signal(false);
  readonly checking = signal(false);
  readonly lastChecked = signal<string | null>(null);

  /** The header label: null when the route is not on offer at all. */
  readonly label = computed(() => (this.url() ? (this.home() ? 'Home wifi' : 'Via cloud') : null));

  private readonly links = new Map<string, Link>();
  private running: Promise<void> | null = null;
  /** A check was asked for while one was out: run once more after it. */
  private again = false;

  constructor() {
    if (inDesktopShell()) return;

    // Listen to the agents only while the account may read them; the rules
    // refuse anyone else, and a refused listener is noise in the console.
    effect(() => {
      const approved = this.session.approved();
      untracked(() => {
        if (approved && !this.held()) {
          this.held.set(this.workers.agents());
        } else if (!approved && this.held()) {
          this.held()?.release();
          this.held.set(null);
          this.home.set(false);
          this.links.clear();
        }
      });
    });

    // A route switched on, off or moved is checked at once; the value only
    // changes when the address does, not on every agent report.
    effect(() => {
      const url = this.url();
      untracked(() => {
        this.links.clear();
        if (url) void this.check();
        else this.home.set(false);
      });
    });

    window.addEventListener('online', () => void this.check());
    window.addEventListener('offline', () => this.home.set(false));
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') void this.check();
    });
    setInterval(() => void this.check(), CHECK_EVERY_MS);
  }

  /**
   * Ask whether the PC answers directly. Resolves once the answer is in — a
   * check already under way included — so playback can wait for it.
   */
  check(fetchImpl: typeof fetch = fetch): Promise<void> {
    if (this.running) {
      // The address may have just changed: the answer under way is about the
      // old one, so this one is asked again once it is in.
      this.again = true;
      return this.running;
    }
    this.running = (async () => {
      do {
        this.again = false;
        await this.ask(fetchImpl);
      } while (this.again);
    })().finally(() => (this.running = null));
    return this.running;
  }

  /** A link to play a clip from the PC, while home; null otherwise. */
  clipUrl(clipId: string, fetchImpl: typeof fetch = fetch): Promise<string | null> {
    return this.mediaUrl('clip', clipId, fetchImpl);
  }

  /**
   * A link to play something from the PC, while home; null otherwise.
   *
   * `clip` is a rendered video, `track` a piece of generated music: the PC
   * serves each only from its own folder (apps/worker/clipforge/home/clips.py).
   */
  async mediaUrl(
    kind: MediaKind,
    id: string,
    fetchImpl: typeof fetch = fetch,
  ): Promise<string | null> {
    await this.settled(fetchImpl);
    const base = this.home() ? this.url() : null;
    if (!base) return null;

    const key = `${kind}:${id}`;
    const cached = this.links.get(key);
    if (cached && cached.expires - Date.now() > LINK_MARGIN_MS) return cached.url;

    const user = this.session.user();
    if (!user) return null;
    try {
      const token = await user.getIdToken();
      const response = await fetchImpl(`${base}/media-links`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind, id }),
        cache: 'no-store',
        signal: AbortSignal.timeout(LINK_TIMEOUT_MS),
      });
      // The PC answered and said no — the file is gone, or this account is
      // not approved. The route is fine; this item just is not on it.
      if (!response.ok) return null;
      const body = (await response.json()) as { url?: string; expiresAt?: string };
      if (!body.url) return null;
      const expires = Date.parse(body.expiresAt ?? '');
      this.links.set(key, {
        url: body.url,
        expires: Number.isNaN(expires) ? Date.now() + LINK_MARGIN_MS * 2 : expires,
      });
      return body.url;
    } catch {
      // No answer at all: the phone has left the house, or the PC has gone to
      // sleep. Treat the route as gone until the next check says otherwise.
      this.home.set(false);
      return null;
    }
  }

  /**
   * Wait for what a cold start has not delivered yet: the agent document (a
   * moment after the app opens) and the first check. Bounded, so a page never
   * waits on the route for longer than it would have waited on the bucket.
   */
  private async settled(fetchImpl: typeof fetch): Promise<void> {
    const held = this.held();
    if (!held) return;
    const deadline = Date.now() + FIRST_DATA_WAIT_MS;
    while (held.data() === null && held.error() === null && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    if (!this.url()) return;
    if (this.running) await this.running;
    else if (this.lastChecked() === null) await this.check(fetchImpl);
  }

  private async ask(fetchImpl: typeof fetch): Promise<void> {
    const url = this.url();
    const expected = this.agent()?.agentId ?? null;
    if (!url) {
      this.home.set(false);
      return;
    }
    this.checking.set(true);
    let ours = false;
    try {
      const response = await fetchImpl(`${url}/hello`, {
        cache: 'no-store',
        signal: AbortSignal.timeout(HELLO_TIMEOUT_MS),
      });
      const answer = response.ok
        ? ((await response.json()) as { service?: string; station?: string })
        : null;
      // Who answered, not merely whether anything did: another program on that
      // address must never be mistaken for the PC.
      ours = answer?.service === HOME_SERVICE && (!expected || answer.station === expected);
    } catch {
      ours = false;
    } finally {
      this.checking.set(false);
      // Only an answer about the address in use now counts.
      const current = url === this.url();
      this.home.set(current && ours);
      this.lastChecked.set(new Date().toISOString());
      if (!current) this.again = true;
    }
  }
}

function inDesktopShell(): boolean {
  const tauri = (globalThis as { __TAURI__?: { core?: { invoke?: unknown } } }).__TAURI__;
  return typeof tauri?.core?.invoke === 'function';
}
