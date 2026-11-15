import { DatePipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  computed,
  inject,
  signal,
} from '@angular/core';

import type { HomeRouteDesired, HomeRouteState } from '@clipforge/contracts';

import { WorkersRepository } from '../../core/data/workers';
import { CLIPFORGE_CONFIG } from '../../core/firebase';
import { HomeRouteService } from '../../core/home-route';
import {
  LocalApiService,
  type WorkerSelfReport,
  type WorkerSettingsGroup,
} from '../../core/local-api';
import { SessionService } from '../../core/session';
import { WorkerPanel } from '../jobs/worker-panel';

/**
 * The worker, as a place rather than a strip above a queue.
 *
 * The panel on Jobs stays where it is: "nothing is running" is the explanation
 * for the list underneath it, and an explanation on another page is one nobody
 * reads. This page is for the other half — what the process is, what it is
 * configured with, and whether it is even pointed at the same database as the
 * window you are reading this in.
 *
 * It reuses that panel rather than growing a second set of Start/Stop controls,
 * so the two surfaces cannot disagree about what the worker is doing.
 */
@Component({
  selector: 'app-worker-page',
  imports: [DatePipe, WorkerPanel],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './worker-page.html',
})
export class WorkerPage implements OnDestroy {
  private readonly local = inject(LocalApiService);
  private readonly config = inject(CLIPFORGE_CONFIG);
  private readonly workers = inject(WorkersRepository);
  private readonly session = inject(SessionService);
  private readonly homeRoute = inject(HomeRouteService);

  // ── The home wifi route (docs/HOME-NETWORK-PLAN.md) ──────────────────────
  // Read from the agent document rather than the loopback API, so this card
  // works from a phone too: that is where it matters.
  private readonly agents = this.workers.agents();
  protected readonly machine = computed(() => this.agents.data()?.[0] ?? null);
  protected readonly route = computed(() => this.machine()?.home ?? null);
  protected readonly routeWanted = computed(() => this.machine()?.homeRouteDesired === 'ON');
  protected readonly isAdmin = this.session.isAdmin;
  protected readonly atHome = this.homeRoute.home;
  protected readonly routeBusy = signal(false);
  protected readonly routeError = signal<string | null>(null);
  protected readonly copied = signal(false);

  // ── Firebase usage (clipforge/usage.py) ───────────────────────────────────
  private readonly usageLive = this.workers.usage();
  protected readonly usage = this.usageLive.data;
  protected readonly today = computed(() => this.usage()?.days?.[0] ?? null);

  /** "1.2 MB", "340 KB". */
  protected bytes(value: number | null | undefined): string {
    const n = value ?? 0;
    if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(2)} GB`;
    if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(1)} MB`;
    if (n >= 1024) return `${Math.round(n / 1024)} KB`;
    return `${n} B`;
  }

  /** Share of the free daily allowance, as a whole percent. */
  protected share(used: number, free: number | undefined): string {
    if (!free) return '';
    const percent = (used / free) * 100;
    return percent < 1 && used > 0 ? '<1%' : `${Math.round(percent)}%`;
  }

  protected readonly routeStateLabel = computed(() => {
    const state = this.route()?.state;
    return state ? ROUTE_STATES[state] : null;
  });

  protected readonly report = signal<WorkerSelfReport | null>(null);
  protected readonly groups = signal<WorkerSettingsGroup[] | null>(null);
  protected readonly error = signal<string | null>(null);
  protected readonly checking = signal(true);

  /** The desktop shell is the only place this API is reachable at all. */
  protected readonly inDesktopShell = this.local.inDesktopShell;

  /** This app's own database, to sit beside the worker's. */
  protected readonly appDatabase = this.config.useEmulators ? 'local emulators' : 'live';

  protected readonly workerDatabase = computed(() => {
    const report = this.report();
    if (!report) return null;
    return report.useEmulators ? 'local emulators' : 'live';
  });

  protected readonly mismatched = computed(() => {
    const worker = this.workerDatabase();
    return worker !== null && worker !== this.appDatabase;
  });

  private ticker: ReturnType<typeof setInterval> | null = null;

  constructor() {
    void this.refresh();
    // Slow on purpose: this page is read, not watched, and the panel above it
    // already polls the things that change second to second.
    this.ticker = setInterval(() => void this.refresh(), 10_000);
  }

  ngOnDestroy(): void {
    if (this.ticker) clearInterval(this.ticker);
    this.agents.release();
    this.usageLive.release();
  }

  /** Admins only, by rule: switching it on opens a port on the home network. */
  protected async setRoute(on: boolean): Promise<void> {
    const machine = this.machine();
    const uid = this.session.uid;
    if (!machine || !uid) return;
    const desired: HomeRouteDesired = on ? 'ON' : 'OFF';
    this.routeBusy.set(true);
    this.routeError.set(null);
    try {
      await this.workers.homeRoute(machine.agentId, uid, desired);
    } catch (err) {
      this.routeError.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.routeBusy.set(false);
    }
  }

  protected async copyFirewallCommand(): Promise<void> {
    const command = this.route()?.firewallCommand;
    if (!command) return;
    try {
      await navigator.clipboard.writeText(command);
      this.copied.set(true);
      setTimeout(() => this.copied.set(false), 2_000);
    } catch {
      // No clipboard (an insecure context, or permission refused): the command
      // is on screen to select by hand.
    }
  }

  /** "12 Feb 2027, 61 days left". */
  protected expiry(iso: string | null | undefined): string | null {
    if (!iso) return null;
    const at = new Date(iso);
    if (Number.isNaN(at.getTime())) return null;
    const days = Math.floor((at.getTime() - Date.now()) / 86_400_000);
    const date = at.toLocaleDateString(undefined, {
      day: 'numeric',
      month: 'short',
      year: 'numeric',
    });
    return days >= 0 ? `${date}, ${days} days left` : `${date}, expired`;
  }

  protected async refresh(): Promise<void> {
    this.checking.set(true);
    try {
      const [report, settings] = await Promise.all([
        this.local.workerSelfReport(),
        this.local.workerSettings(),
      ]);
      this.report.set(report);
      this.groups.set(settings?.groups ?? null);
      this.error.set(null);
    } catch (err) {
      // A worker that is not running is the ordinary case here, not a fault.
      this.report.set(null);
      this.groups.set(null);
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.checking.set(false);
    }
  }

  /** "4s", "3m", "2h" — the same shape the panel uses. */
  protected uptime(seconds: number): string {
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
    return `${Math.round(seconds / 360) / 10}h`;
  }
}

/** What each state means, in words a phone can show. */
const ROUTE_STATES: Record<HomeRouteState, string> = {
  OFF: 'Off',
  UNAVAILABLE: 'Not set up on the PC',
  CERTIFICATE: 'Getting the certificate',
  SERVING: 'On',
  FAILED: 'Not working',
};
