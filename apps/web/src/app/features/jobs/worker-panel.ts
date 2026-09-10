import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  computed,
  effect,
  inject,
  input,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import type { WorkerHeartbeat } from '@clipforge/contracts';

import { CLIPFORGE_CONFIG } from '../../core/firebase';
import { ClipForgeStore } from '../../core/store';
import { WorkerControlService } from '../../core/worker-control';

/**
 * Is anything actually going to run this queue?
 *
 * The question that makes this panel worth its space. A job sitting at QUEUED
 * has two very different explanations — "the worker is busy with the one in
 * front of it" and "there is no worker" — and the job document cannot tell them
 * apart. Before this, the second one looked like the first for as long as it
 * took someone to think of checking.
 *
 * ## Two signals, because each is blind where the other sees
 *
 * - The **heartbeat** in Firestore is visible from anywhere, phone included, and
 *   says what the worker believes it is doing. It is up to 30 seconds stale, and
 *   a worker that was killed leaves its last heartbeat behind saying ONLINE
 *   forever — which is why {@link stale} exists and is trusted over `status`.
 * - The **process state** from the desktop shell is instant and works with no
 *   network at all, but only describes this machine.
 *
 * Neither is dropped in favour of the other: on the desktop they are shown
 * together, and a disagreement between them is itself informative — a running
 * process with a stale heartbeat means the worker cannot reach Firestore.
 */
@Component({
  selector: 'app-worker-panel',
  imports: [FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './worker-panel.html',
})
export class WorkerPanel implements OnDestroy {
  private readonly store = inject(ClipForgeStore);
  private readonly config = inject(CLIPFORGE_CONFIG);
  protected readonly control = inject(WorkerControlService);

  /** How many jobs are waiting, so the panel can say what that means. */
  readonly queued = input(0);

  private stopWatching: (() => void) | null = null;
  private stopHeartbeat: (() => void) | null = null;
  private ticker: ReturnType<typeof setInterval> | null = null;

  protected readonly heartbeats = signal<WorkerHeartbeat[] | null>(null);
  protected readonly heartbeatError = signal<string | null>(null);
  protected readonly logOpen = signal(false);
  protected readonly repoDraft = signal('');

  /**
   * A clock, so "last seen 40s ago" keeps counting up.
   *
   * Without it the panel freezes at whatever the last Firestore delivery said,
   * and a worker that died two minutes ago still reads "last seen 2s ago" — the
   * precise lie this panel exists to stop telling.
   */
  private readonly now = signal(Date.now());

  protected readonly heartbeat = computed(() => this.heartbeats()?.[0] ?? null);

  /**
   * Whether the heartbeat has stopped arriving.
   *
   * Ninety seconds, matching `Settings.lease_seconds` rather than the 30-second
   * heartbeat interval: one missed beat is a slow write, three is a worker that
   * is no longer there — and 90s is also the point at which the scheduler itself
   * stops believing the worker owns anything.
   */
  protected readonly stale = computed(() => {
    const beat = this.heartbeat();
    if (!beat) return true;
    return this.now() - Date.parse(beat.lastSeenAt) > 90_000;
  });

  /** What to show as the headline state, heartbeat and process reconciled. */
  protected readonly live = computed<'busy' | 'online' | 'offline'>(() => {
    const beat = this.heartbeat();
    if (!beat || this.stale() || beat.status === 'OFFLINE') return 'offline';
    return beat.status === 'BUSY' || (beat.activeJobIds?.length ?? 0) > 0 ? 'busy' : 'online';
  });

  /** Jobs are waiting and there is nothing to run them. The actionable case. */
  protected readonly stalled = computed(() => this.queued() > 0 && this.live() === 'offline');

  protected readonly running = computed(() => {
    const state = this.control.status()?.state;
    return state === 'running' || state === 'foreign';
  });

  /** Newest line first, for the `flex-col-reverse` log pane. */
  protected readonly newestFirst = computed(() => {
    const log = this.control.status()?.log ?? [];
    return [...log].reverse();
  });

  constructor() {
    effect((onCleanup) => {
      const stop = this.store.watchWorkers(
        (workers) => {
          // Newest first, so a machine that was replaced does not outrank the
          // one actually beating.
          this.heartbeats.set(
            [...workers].sort((a, b) => Date.parse(b.lastSeenAt) - Date.parse(a.lastSeenAt)),
          );
          this.heartbeatError.set(null);
        },
        (error) => this.heartbeatError.set(error.message),
      );
      this.stopHeartbeat = stop;
      onCleanup(stop);
    });

    this.stopWatching = this.control.watch();
    this.ticker = setInterval(() => this.now.set(Date.now()), 5_000);
  }

  ngOnDestroy(): void {
    this.stopWatching?.();
    this.stopHeartbeat?.();
    if (this.ticker) clearInterval(this.ticker);
  }

  protected async start(): Promise<void> {
    await this.control.start(this.config.useEmulators);
  }

  protected async stop(): Promise<void> {
    await this.control.stop();
  }

  protected async saveRepo(): Promise<void> {
    const path = this.repoDraft().trim();
    if (path) await this.control.setRepo(path);
  }

  /** "4s", "3m", "2h" — short enough to sit inline without wrapping. */
  protected since(iso: string | null | undefined): string {
    if (!iso) return '—';
    return this.ago(Date.parse(iso));
  }

  protected sinceMs(ms: number | null | undefined): string {
    return ms ? this.ago(ms) : '—';
  }

  private ago(at: number): string {
    const seconds = Math.max(0, Math.round((this.now() - at) / 1000));
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
    return `${Math.round(seconds / 3600)}h`;
  }

  protected vram(beat: WorkerHeartbeat): string | null {
    if (!beat.gpu) return null;
    const free = (beat.gpu.vramFreeMb / 1024).toFixed(1);
    const total = (beat.gpu.vramTotalMb / 1024).toFixed(1);
    return `${beat.gpu.name} · ${free}/${total} GB free`;
  }
}
