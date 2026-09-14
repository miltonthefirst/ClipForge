import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  computed,
  inject,
  signal,
} from '@angular/core';

import { CLIPFORGE_CONFIG } from '../../core/firebase';
import {
  LocalApiService,
  type WorkerSelfReport,
  type WorkerSettingsGroup,
} from '../../core/local-api';
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
  imports: [WorkerPanel],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './worker-page.html',
})
export class WorkerPage implements OnDestroy {
  private readonly local = inject(LocalApiService);
  private readonly config = inject(CLIPFORGE_CONFIG);

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
