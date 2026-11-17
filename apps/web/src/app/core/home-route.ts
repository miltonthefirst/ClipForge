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

/**
 * On the home wifi the answer is usually back in milliseconds; anywhere else
 * the address leads nowhere, and this is how long that takes to find out.
 * Four seconds, and asked twice, because a phone's wifi waking from power
 * saving, plus the TLS handshake, took longer than the 1.5 s this used to be:
 * one slow answer labelled a phone at home "Via cloud" for five minutes, and
 * every clip offered an upload instead of playing (2026-11-18).
 */
const HELLO_TIMEOUT_MS = 4_000;
const HELLO_TRIES = 2;
/**
 * Believing it is away, playback asks again before giving up if the last
 * answer is older than this: opening a clip is when being wrong costs most.
 */
const RECHECK_AFTER_MS = 15_000;
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
  /**
   * The freshest machine, route or not. The app shell reads its version from
   * here, so showing which build the PC runs costs no listener of its own.
   */
  readonly machine: Signal<AgentReport | null> = computed(() => this.held()?.data()?.[0] ?? null);

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
    // Believed away, on an old answer: ask again, without making this caller
    // wait out a timeout away from home. A page showing a poster retries every
    // few seconds (clip-page.ts), and plays from the PC once it answers.
    if (!this.home() && this.url() && this.stale() && !this.running) void this.check(fetchImpl);
    const base = this.home() ? this.url() : null;
    if (!base) return null;

    const key = `${kind}:${id}`;
    const cached = this.links.get(key);
    if (cached && cached.expires - Date.now() > LINK_MARGIN_MS) return cached.url;

    const user = this.session.user();
    if (!user) return null;
    try {
      const token = await user.getIdToken();
      const response = await fetchImpl(bypassServiceWorker(`${base}/media-links`), {
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
      // The <video> or <audio> element fetches this too, ranges and all: past
      // the service worker as well.
      const playable = bypassServiceWorker(body.url);
      this.links.set(key, {
        url: playable,
        expires: Number.isNaN(expires) ? Date.now() + LINK_MARGIN_MS * 2 : expires,
      });
      return playable;
    } catch {
      // No answer to this one request. That alone does not mean the phone has
      // left the house (it used to be taken that way, and one slow reply
      // turned the whole app to "Via cloud"): ask the PC whether it is there,
      // and let that answer decide.
      void this.check(fetchImpl);
      return null;
    }
  }

  /** Whether the last answer about the route is too old to trust as "away". */
  private stale(): boolean {
    const last = this.lastChecked();
    return last === null || Date.now() - Date.parse(last) > RECHECK_AFTER_MS;
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
      for (let attempt = 1; attempt <= HELLO_TRIES; attempt++) {
        try {
          const response = await fetchImpl(bypassServiceWorker(`${url}/hello`), {
            cache: 'no-store',
            signal: AbortSignal.timeout(HELLO_TIMEOUT_MS),
          });
          const answer = response.ok
            ? ((await response.json()) as { service?: string; station?: string })
            : null;
          // Who answered, not merely whether anything did: another program on
          // that address must never be mistaken for the PC.
          ours = answer?.service === HOME_SERVICE && (!expected || answer.station === expected);
          break; // an answer, ours or not, is the answer
        } catch {
          // No answer: asked once more, since the first try after the phone
          // wakes is the slow one.
          ours = false;
        }
      }
    } finally {
      this.checking.set(false);
      // Only an answer about the address in use now counts.
      const current = url === this.url();
      this.home.set(current && ours);
      this.lastChecked.set(new Date().toISOString());
      if (!current) this.again = true;
    }
  }

  // ── Checking this device, by hand (the Worker page's Home wifi card) ───────

  /**
   * The browser's local network permission for this site.
   *
   * Chrome asks before a public page may reach a device on the local network
   * (Local Network Access); the permission's name has changed between
   * versions, so both are tried. A browser that knows neither (Safari, older
   * Chrome) says `unknown`, and only an actual request tells.
   */
  async localAccess(): Promise<LocalAccess> {
    const permissions = (globalThis.navigator as Navigator | undefined)?.permissions;
    if (!permissions?.query) return 'unknown';
    for (const name of ['local-network-access', 'local-network']) {
      try {
        const status = await permissions.query({ name: name as PermissionName });
        return status.state;
      } catch {
        // Not a permission this browser knows; try the other name.
      }
    }
    return 'unknown';
  }

  /**
   * Walk the route the way a clip will, one step at a time, and say which
   * step fails and what to do about it.
   *
   * The first request is also what makes Chrome ask its local network
   * question, so pressing "Check" (or "Allow access") is how a person who has
   * not answered it yet gets asked. A blocked permission cannot be asked
   * again from a page; the card says where to change it.
   *
   * The last step asks the PC for a clip that does not exist: "no such clip"
   * means the sign-in was checked and the account is approved, without
   * copying anything.
   */
  async diagnose(fetchImpl: typeof fetch = fetch): Promise<CheckStep[]> {
    if (inDesktopShell()) {
      return [
        {
          id: 'desktop',
          label: 'This is the desktop app on the PC',
          state: 'pass',
          detail:
            'It plays straight from the PC itself; the home wifi route is for phones and other devices.',
        },
      ];
    }
    const steps: CheckStep[] = [];
    const url = this.url();
    if (!url) {
      steps.push({
        id: 'advertised',
        label: 'The PC offers the route',
        state: 'fail',
        detail:
          'The PC is not offering it: it is switched off, still getting its certificate, or the PC is off. An admin switches it on above.',
      });
      return steps;
    }
    steps.push({
      id: 'advertised',
      label: 'The PC offers the route',
      state: 'pass',
      detail: url,
    });

    const started = Date.now();
    let reached = false;
    try {
      const response = await fetchImpl(bypassServiceWorker(`${url}/hello`), {
        cache: 'no-store',
        signal: AbortSignal.timeout(4_000),
      });
      const text = await response.text();
      let answer: Hello | null = null;
      try {
        answer = response.ok ? (JSON.parse(text) as Hello) : null;
      } catch {
        answer = null;
      }
      if (answer?.service === HOME_SERVICE) {
        reached = true;
        steps.push({
          id: 'reach',
          label: 'This device reaches the PC over the wifi',
          state: 'pass',
          detail: `Answered in ${Date.now() - started} ms (${answer.station}, ${answer.version}).`,
        });
        steps.push({
          id: 'secure',
          label: 'The connection is secure',
          state: 'pass',
          detail: "The PC's certificate checked out.",
        });
      } else {
        steps.push({
          id: 'reach',
          label: 'This device reaches the PC over the wifi',
          state: 'fail',
          // What actually came back, so a screenshot of this says what is in the way.
          detail:
            `Something answered at that address, but not ClipForge: ${response.status} ${response.statusText} ${text.slice(0, 120)}`.trim(),
        });
      }
    } catch {
      const access = await this.localAccess();
      steps.push({
        id: 'reach',
        label: 'This device reaches the PC over the wifi',
        state: 'fail',
        detail:
          access === 'denied'
            ? "This browser is blocking ClipForge from the local network. Allow it in the site's settings (steps below), then check again."
            : 'No answer. Is this device on the home wifi? Away from home this is expected, and clips play from the cloud.',
      });
    }
    if (!reached) {
      this.home.set(false);
      return steps;
    }

    const user = this.session.user();
    if (!user) {
      steps.push({
        id: 'signin',
        label: 'Your sign-in is accepted',
        state: 'fail',
        detail: 'Not signed in.',
      });
      return steps;
    }
    try {
      const token = await user.getIdToken();
      const response = await fetchImpl(bypassServiceWorker(`${url}/media-links`), {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind: 'clip', id: 'home-route-check' }),
        cache: 'no-store',
        signal: AbortSignal.timeout(8_000),
      });
      const verdicts: Record<number, CheckStep> = {
        404: {
          id: 'signin',
          label: 'Your sign-in is accepted',
          state: 'pass',
          detail: 'Signed in and approved: clips will play from the PC.',
        },
        403: {
          id: 'signin',
          label: 'Your sign-in is accepted',
          state: 'fail',
          detail:
            'The PC says this account is not approved. An admin approves it on the People page.',
        },
        401: {
          id: 'signin',
          label: 'Your sign-in is accepted',
          state: 'fail',
          detail: 'The PC could not check the sign-in. Sign out and in again, then check again.',
        },
      };
      steps.push(
        verdicts[response.status] ?? {
          id: 'signin',
          label: 'Your sign-in is accepted',
          state: 'fail',
          detail: `The PC answered ${response.status}.`,
        },
      );
    } catch {
      steps.push({
        id: 'signin',
        label: 'Your sign-in is accepted',
        state: 'fail',
        detail: 'The PC stopped answering part-way through. Check again in a moment.',
      });
    }
    // A full pass means playback can use the route from now on.
    await this.check(fetchImpl);
    return steps;
  }
}

/** What the PC's `/hello` says. */
interface Hello {
  readonly service?: string;
  readonly station?: string;
  readonly version?: string;
}

/** The browser's local network permission, where it has one. */
export type LocalAccess = 'granted' | 'prompt' | 'denied' | 'unknown';

/** One step of {@link HomeRouteService.diagnose}. */
export interface CheckStep {
  readonly id: 'desktop' | 'advertised' | 'reach' | 'secure' | 'signin';
  readonly label: string;
  readonly state: 'pass' | 'fail';
  readonly detail: string;
}

/**
 * The same address, marked for Angular's service worker to leave alone.
 *
 * The phone app's service worker sees every request the page makes, the PC's
 * included, and fetches it itself. Chrome on Android refused those to the
 * home address (the PC saw the phone connect, and the page was handed the
 * service worker's own "504 Gateway Timeout"), while the page itself is
 * allowed. `ngsw-bypass` is Angular's documented opt-out; as a query
 * parameter it adds no header, so no CORS preflight, and the listener ignores
 * it. Sarungano's app has no service worker, which is why it never met this.
 */
export function bypassServiceWorker(url: string): string {
  return `${url}${url.includes('?') ? '&' : '?'}ngsw-bypass=true`;
}

function inDesktopShell(): boolean {
  const tauri = (globalThis as { __TAURI__?: { core?: { invoke?: unknown } } }).__TAURI__;
  return typeof tauri?.core?.invoke === 'function';
}
