import { Injectable, NgZone, inject, signal } from '@angular/core';

/**
 * Starting and stopping the worker on this machine.
 *
 * ## Why this is desktop-only, and why that is not a limitation
 *
 * ClipForge has no server-side executor. There are no Cloud Functions on the
 * Spark free tier, so the claim loop and the reaper both live inside the worker
 * process (docs/adr/0006-lease-based-job-claiming.md) and a job stays QUEUED
 * until something on the machine picks it up. A browser tab cannot spawn a
 * process, and no amount of API design changes that.
 *
 * So this service is *feature-detected rather than configured*, exactly like
 * {@link LocalApiService}: the same bundle ships to Firebase Hosting and into
 * Tauri, and on the web `available` is false and the panel explains why instead
 * of offering a button that could not work. A phone still sees everything the
 * heartbeat reports — it just cannot be the one to act.
 *
 * ## Why it does not talk to the worker directly
 *
 * `LocalApiService` reaches the worker's control API over loopback, and it could
 * carry a shutdown request too. It cannot carry a *start*: the API it would ask
 * lives inside the process that is not running. Nor can it tell "the worker is
 * gone" from "the worker is wedged and not answering", because both look like a
 * failed fetch. Rust owns the child process, so Rust is the only thing that
 * knows — and it is what turns a stop into a clean shutdown with a kill behind
 * it rather than a kill on its own.
 */

/** Mirrors `RunState` in apps/desktop/src-tauri/src/worker.rs. */
export type WorkerRunState = 'stopped' | 'running' | 'foreign' | 'exited';

/** Mirrors `WorkerStatus` in apps/desktop/src-tauri/src/worker.rs. */
export interface WorkerProcessStatus {
  readonly state: WorkerRunState;
  readonly pid: number | null;
  readonly startedAtMs: number | null;
  readonly exitCode: number | null;
  readonly note: string | null;
  readonly repo: string | null;
  readonly canStart: boolean;
  readonly controlPort: number;
  readonly activeJobIds: readonly string[];
  readonly log: readonly string[];
}

/** The event the Rust side publishes on every state change and log line. */
const UPDATE_EVENT = 'clipforge://worker';

type Invoke = (cmd: string, args?: unknown) => Promise<unknown>;
type Listen = (
  event: string,
  handler: (message: { payload: unknown }) => void,
) => Promise<() => void>;

interface TauriGlobal {
  core?: { invoke?: Invoke };
  event?: { listen?: Listen };
}

@Injectable({ providedIn: 'root' })
export class WorkerControlService {
  private readonly zone = inject(NgZone);

  readonly status = signal<WorkerProcessStatus | null>(null);
  readonly busy = signal(false);
  readonly error = signal<string | null>(null);

  /** Whether this build can control a worker at all — i.e. is it the shell? */
  get available(): boolean {
    return typeof this.tauri()?.core?.invoke === 'function';
  }

  private tauri(): TauriGlobal | undefined {
    return (globalThis as { __TAURI__?: TauriGlobal }).__TAURI__;
  }

  /**
   * Follow the worker until the caller unsubscribes.
   *
   * One `worker_status` call for the state right now, then events for every
   * change after it. Not polling: the interesting transitions — a stage
   * finishing, a start-up failing — arrive as log lines, and a poll interval
   * fast enough to feel live would spend most of its calls learning nothing.
   *
   * The event callback crosses from Tauri's IPC into Angular, which is outside
   * the zone. Without `zone.run` the signal updates and the view does not, which
   * is the failure that looks exactly like the worker having hung.
   */
  watch(): () => void {
    /** Nothing was subscribed, so there is nothing to unsubscribe. */
    const noop = (): void => undefined;

    if (!this.available) return noop;

    void this.refresh();

    let stop: (() => void) | null = null;
    let cancelled = false;

    const listen = this.tauri()?.event?.listen;
    if (listen) {
      void listen(UPDATE_EVENT, (message) => {
        this.zone.run(() => this.status.set(message.payload as WorkerProcessStatus));
      })
        .then((unlisten) => {
          // The caller may have torn down while the listener was registering.
          if (cancelled) unlisten();
          else stop = unlisten;
        })
        .catch(() => {
          /* No events; `refresh()` on each action still keeps the panel honest. */
        });
    }

    return () => {
      cancelled = true;
      stop?.();
    };
  }

  async refresh(): Promise<void> {
    await this.run('worker_status');
  }

  /**
   * Start a worker pointed at the same backend this app is.
   *
   * `emulators` comes from the app's own `window.__clipforge`, not from the
   * worker's `.env`. That file deliberately pins `CLIPFORGE_USE_EMULATORS=true`
   * so routine development cannot touch the real project — which means a worker
   * started without an override would poll an emulator while this app watched
   * production, and the queue would sit there looking broken. Passing the app's
   * own answer makes that mismatch impossible rather than merely unlikely.
   */
  async start(emulators: boolean): Promise<void> {
    await this.run('worker_start', { emulators });
  }

  async stop(): Promise<void> {
    await this.run('worker_stop');
  }

  /** Tell the shell where the checkout is, when it could not work it out. */
  async setRepo(path: string): Promise<void> {
    await this.run('worker_set_repo', { path });
  }

  private async run(command: string, args?: unknown): Promise<void> {
    const invoke = this.tauri()?.core?.invoke;
    if (!invoke) return;

    this.busy.set(true);
    this.error.set(null);
    try {
      this.status.set((await invoke(command, args)) as WorkerProcessStatus);
    } catch (error) {
      // Rust returns `Err(String)`, which arrives as a bare string rather than
      // an Error. Both shapes are handled because a panic would arrive as the
      // other one.
      this.error.set(typeof error === 'string' ? error : ((error as Error)?.message ?? 'Failed.'));
    } finally {
      this.busy.set(false);
    }
  }
}
