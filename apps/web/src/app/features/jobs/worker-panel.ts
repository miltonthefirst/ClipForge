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
import type { AgentDesired, AgentReport, WorkerHeartbeat } from '@clipforge/contracts';

import { CLIPFORGE_CONFIG } from '../../core/firebase';
import { LocalApiService, type WorkerSelfReport } from '../../core/local-api';
import { SessionService } from '../../core/session';
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
 * - The **agent** in `agents/{machineId}` is visible from anywhere *and* keeps
 *   reporting when no worker is running, which neither of the others can do: a
 *   worker that is not running cannot send a heartbeat saying so, and a browser
 *   tab has no process table. It is also the only one of the three that can
 *   *act* from a phone.
 *
 * None is dropped in favour of another: on the desktop they are shown together,
 * and a disagreement between them is itself informative — a running process with
 * a stale heartbeat means the worker cannot reach Firestore.
 *
 * ## Which button the buttons press
 *
 * One pair of controls, two routes, chosen by what is actually available:
 *
 * - In the desktop shell, Tauri owns the child process, so it starts and stops
 *   it — instantly, and with no network at all.
 * - Everywhere else, and that includes the phone this was built for, the wish
 *   is written to the agent document and the agent does it.
 *
 * The desktop route *also* writes the wish, and that is not redundancy. The
 * agent reconciles what is running against what is wanted, so a worker started
 * behind its back under a standing `STOPPED` would be a worker it reports as
 * foreign rather than one it adopts. Writing both keeps the two in agreement
 * about what the person actually asked for.
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
  private readonly local = inject(LocalApiService);
  private readonly session = inject(SessionService);
  protected readonly control = inject(WorkerControlService);

  /** How many jobs are waiting, so the panel can say what that means. */
  readonly queued = input(0);

  private stopWatching: (() => void) | null = null;
  private stopHeartbeat: (() => void) | null = null;
  private ticker: ReturnType<typeof setInterval> | null = null;

  /** What the worker on this machine says about itself, if one answers. */
  protected readonly selfReport = signal<WorkerSelfReport | null>(null);

  /**
   * A worker is running, but against the other database.
   *
   * The app spawns workers with its own mode pinned, so this cannot happen by
   * that route — but a worker started from a terminal takes whatever .env says,
   * and then the app reads one database while the worker writes the other. The
   * heartbeat cannot report it: a worker writing somewhere else looks exactly
   * like no worker at all, which is the most misleading state the panel has.
   */
  protected readonly modeMismatch = computed(() => {
    const report = this.selfReport();
    if (!report) return null;
    if (report.useEmulators === this.config.useEmulators) return null;
    return {
      worker: report.useEmulators
        ? 'the local emulators'
        : `the live project (${report.projectId})`,
      app: this.config.useEmulators ? 'the local emulators' : 'the live project',
    };
  });

  protected readonly heartbeats = signal<WorkerHeartbeat[] | null>(null);
  protected readonly heartbeatError = signal<string | null>(null);
  protected readonly agents = signal<AgentReport[] | null>(null);

  /** Set between the click and the agent document catching up with it. */
  protected readonly submitting = signal<AgentDesired | null>(null);
  protected readonly wishError = signal<string | null>(null);
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

  /**
   * The machine this panel offers to start, when there is one.
   *
   * The most recently seen agent rather than a configured name: ClipForge's
   * premise is one machine with a GPU, and picking the freshest is right for
   * the one case and harmless for the two-machine case a later phase will have
   * to give a picker.
   */
  protected readonly machine = computed(() => {
    const agents = this.agents();
    if (!agents?.length) return null;
    return [...agents].sort((a, b) => Date.parse(b.lastSeenAt) - Date.parse(a.lastSeenAt))[0];
  });

  /**
   * The agent has stopped reporting, so the machine is off or asleep.
   *
   * Three minutes, against a heartbeat of one: one missed beat is a slow write,
   * three is a PC that is not there. Longer than the worker's ninety seconds
   * because this heartbeat is deliberately slower — it exists to say the machine
   * is on, not to hold a lease.
   */
  protected readonly machineStale = computed(() => {
    const agent = this.machine();
    if (!agent) return true;
    return this.now() - Date.parse(agent.lastSeenAt) > 180_000;
  });

  /** An agent that is listening right now, and could act on a request. */
  protected readonly reachable = computed(() => !!this.machine() && !this.machineStale());

  /** Whether this device can start the worker at all, by either route. */
  protected readonly canControl = computed(() => this.control.available || this.reachable());

  /** What the agent is doing about the last request, if anything. */
  protected readonly agentBusy = computed(() => {
    const state = this.machine()?.state;
    return !!this.submitting() || state === 'STARTING' || state === 'STOPPING';
  });

  /** True while a worker is running by any account this panel trusts. */
  protected readonly anyRunning = computed(() => {
    if (this.control.available) return this.running();
    const state = this.machine()?.state;
    if (state === 'RUNNING' || state === 'FOREIGN' || state === 'STARTING') return true;
    return this.live() !== 'offline';
  });

  /**
   * Whether pressing Start could do anything, as opposed to merely being there.
   *
   * The agent can always be asked — it is a document write, and the agent
   * reports what came of it. The shell knows more: it has looked for the
   * checkout and for `uv`, and says so before the button is pressed rather than
   * after.
   */
  protected readonly canStart = computed(() => {
    if (this.reachable()) return true;
    return !!this.control.status()?.canStart;
  });

  /**
   * One label for two routes and three sources of "in progress".
   *
   * Worth computing rather than nesting in the template: "Starting…" while the
   * agent is stopping is the sort of wrong that makes someone press the button
   * again.
   */
  protected readonly actionLabel = computed(() => {
    const state = this.machine()?.state;
    const asked = this.submitting();
    if (asked === 'RUNNING' || state === 'STARTING') return 'Starting…';
    if (asked === 'STOPPED' || state === 'STOPPING') return 'Stopping…';
    if (this.control.busy()) return this.anyRunning() ? 'Stopping…' : 'Starting…';
    return this.anyRunning() ? 'Stop' : 'Start worker';
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

    effect((onCleanup) => {
      const stop = this.store.watchAgents(
        (agents) => {
          this.agents.set(agents);
          // The agent has taken the request on board; the button can stop
          // saying "Starting…" on its own account and start reflecting what the
          // machine actually reports.
          const asked = this.submitting();
          if (asked && agents.some((agent) => agent.desired === asked)) {
            this.submitting.set(null);
          }
        },
        () => this.agents.set([]),
      );
      onCleanup(stop);
    });

    this.stopWatching = this.control.watch();
    void this.refreshSelfReport();
    this.ticker = setInterval(() => {
      this.now.set(Date.now());
      void this.refreshSelfReport();
    }, 5_000);
  }

  ngOnDestroy(): void {
    this.stopWatching?.();
    this.stopHeartbeat?.();
    if (this.ticker) clearInterval(this.ticker);
  }

  /** Silent on failure: no worker answering is the normal case here. */
  private async refreshSelfReport(): Promise<void> {
    try {
      this.selfReport.set(await this.local.workerSelfReport());
    } catch {
      this.selfReport.set(null);
    }
  }

  protected async start(): Promise<void> {
    await this.act('RUNNING', () => this.control.start(this.config.useEmulators));
  }

  protected async stop(): Promise<void> {
    await this.act('STOPPED', () => this.control.stop());
  }

  /**
   * Do it here if this is the machine, and record the wish either way.
   *
   * The local route runs first and is awaited, because when it works it is the
   * one the person is watching: the log pane fills in, the pid appears, and the
   * document write that follows is bookkeeping. When there is no local route the
   * wish *is* the action.
   */
  private async act(desired: AgentDesired, locally: () => Promise<void>): Promise<void> {
    this.wishError.set(null);
    if (this.control.available) await locally();

    const agent = this.machine();
    const uid = this.session.user()?.uid;
    if (!agent || !uid) return;

    this.submitting.set(desired);
    try {
      await this.store.wish(agent.agentId, uid, desired);
    } catch (error) {
      this.submitting.set(null);
      // Only worth surfacing when the wish was the whole action. In the desktop
      // shell the worker has already started or stopped, and an alarming red
      // box about a document write would describe a real but secondary problem.
      if (!this.control.available) {
        this.wishError.set(error instanceof Error ? error.message : 'Could not reach the machine.');
      }
    }
  }

  protected async saveRepo(): Promise<void> {
    const path = this.repoDraft().trim();
    if (path) await this.control.setRepo(path);
  }

  /**
   * What the agent is up to, in words rather than an enum.
   *
   * FOREIGN earns the longest phrase because it is the one that would otherwise
   * be read as a bug: the panel is reporting a running worker next to a machine
   * that says it did not start one, and the reason — somebody started it at the
   * PC, or from a terminal — is the whole explanation.
   */
  protected agentState(agent: AgentReport): string {
    switch (agent.state) {
      case 'STARTING':
        return 'starting the worker';
      case 'RUNNING':
        return 'worker running';
      case 'FOREIGN':
        return 'a worker it did not start is running';
      case 'STOPPING':
        return 'stopping the worker';
      case 'FAILED':
        return 'the worker would not stay up';
      default:
        return 'ready, worker stopped';
    }
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
