import { Injectable, signal } from '@angular/core';

/**
 * Talking to the worker running on this machine.
 *
 * Only the desktop shell can: the worker's control API answers on 127.0.0.1 and
 * demands a bearer token that a browser tab has no way to read. That is the
 * point of it — a YouTube client secret can be typed into a form and still never
 * leave the machine (docs/adr/0011-local-control-api.md).
 *
 * So this service is *feature-detected rather than configured*. The same bundle
 * ships to Firebase Hosting and into Tauri; on the web `available()` is false
 * and the settings page says why, instead of offering a form that could not
 * possibly work.
 */

export interface LocalApiHandshake {
  readonly available: boolean;
  readonly origin?: string;
  readonly token?: string;
  readonly reason?: string;
}

export interface YouTubeStatus {
  readonly publishingEnabled: boolean;
  readonly connection: 'NOT_CONFIGURED' | 'NEEDS_AUTH' | 'CONNECTED' | 'ERROR';
  readonly message: string | null;
  readonly clientSecretsPath: string;
  readonly hasClient: boolean;
  readonly hasToken: boolean;
  readonly tokenAgeDays: number | null;
  readonly defaultPrivacy: string;
  /** True while a browser window is open on the consent screen. */
  readonly authorising: boolean;
  /** Why the last authorisation attempt failed, if it did. */
  readonly authError: string | null;
}

export interface WorkerSelfReport {
  readonly pid: number;
  readonly workerId: string;
  readonly version: string;
  readonly startedAt: string;
  readonly uptimeSeconds: number;
  readonly activeJobIds: readonly string[];
  /** Which database this worker is talking to, as the worker sees it. */
  readonly useEmulators: boolean;
  readonly projectId: string;
}

export interface WorkerSettingsGroup {
  readonly title: string;
  readonly rows: readonly { readonly label: string; readonly value: string }[];
}

export interface AuthoriseResult {
  readonly ok: boolean;
  /** Already authorised, so nothing was opened. */
  readonly already?: boolean;
  /** A browser is open and the worker is waiting for the redirect. */
  readonly waiting?: boolean;
  readonly url?: string;
  readonly openedBrowser?: boolean;
  readonly timeoutSec?: number;
  readonly message?: string;
}

interface TauriGlobal {
  core?: { invoke?: (cmd: string, args?: unknown) => Promise<unknown> };
}

/** One file on the worker's disk. */
export interface StorageFile {
  path: string;
  name: string;
  kind: 'clip' | 'source' | 'other';
  recordId: string;
  sizeBytes: number;
  modifiedAt: number;
}

/** One item in the bin, and where it came from. */
export interface TrashItem {
  id: string;
  kind: string;
  name: string;
  originalPath: string;
  sizeBytes: number;
  trashedAt: string;
  recordId: string | null;
}

/**
 * What is actually on the machine, as opposed to what Firestore remembers.
 *
 * `trashBytes` is reported separately from `usedBytes` and is not included in
 * it: the bin is outside the disk budget on purpose, so the collector never
 * evicts a live download to make room for a deleted one. The consequence is
 * that the bin can fill a disk while the workspace reports itself comfortable,
 * which is exactly why this number is shown next to the button that empties it.
 */
export interface StorageReport {
  root: string;
  clips: StorageFile[];
  sources: StorageFile[];
  trash: TrashItem[];
  usedBytes: number;
  maxBytes: number;
  freeDiskBytes: number;
  trashBytes: number;
}

@Injectable({ providedIn: 'root' })
export class LocalApiService {
  private handshake: LocalApiHandshake | null = null;

  /** Why the local API is unreachable, when it is. Shown, not swallowed. */
  readonly unavailableReason = signal<string | null>(null);

  /** Whether this build is running inside the desktop shell at all. */
  get inDesktopShell(): boolean {
    return typeof this.tauri()?.core?.invoke === 'function';
  }

  private tauri(): TauriGlobal | undefined {
    return (globalThis as { __TAURI__?: TauriGlobal }).__TAURI__;
  }

  /**
   * Fetch the origin and pairing token from the shell, once.
   *
   * Cached because it reads a file, and the answer cannot change while the app
   * is open in any way the user would notice — the worker writes the token on
   * its first run and reuses it thereafter.
   */
  async connect(): Promise<LocalApiHandshake> {
    if (this.handshake) return this.handshake;

    const invoke = this.tauri()?.core?.invoke;
    if (!invoke) {
      const reason =
        'Connecting a channel needs the desktop app, because the credential is handed ' +
        'to the worker on this machine and never travels. Everything else here works anywhere.';
      this.unavailableReason.set(reason);
      this.handshake = { available: false, reason };
      return this.handshake;
    }

    try {
      const result = (await invoke('local_api')) as LocalApiHandshake;
      this.handshake = result;
      this.unavailableReason.set(result.available ? null : (result.reason ?? null));
      return result;
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      this.unavailableReason.set(reason);
      // Deliberately not cached: unlike "this is a browser", a failed invoke can
      // succeed on a retry, and caching it would strand the page until reload.
      return { available: false, reason };
    }
  }

  /**
   * Ask the worker on this machine what it is, including which database it is
   * using. Unlike the Firestore heartbeat this answers even when the worker is
   * writing somewhere the app is not reading — which is the one failure a
   * heartbeat cannot report, because it looks exactly like no worker at all.
   */
  async workerSelfReport(): Promise<WorkerSelfReport | null> {
    return this.request<WorkerSelfReport>('GET', '/worker');
  }

  /**
   * What the worker is configured with.
   *
   * Labelled rows rather than a typed field map on purpose: the worker decides
   * what is worth showing, and the page renders whatever it is handed, so a new
   * setting appears without a matching change on this side.
   */
  async workerSettings(): Promise<{ groups: WorkerSettingsGroup[] } | null> {
    return this.request<{ groups: WorkerSettingsGroup[] }>('GET', '/worker/settings');
  }

  async status(): Promise<YouTubeStatus | null> {
    return this.request<YouTubeStatus>('GET', '/status');
  }

  async setClient(clientId: string, clientSecret: string): Promise<void> {
    await this.request('POST', '/youtube/client', { clientId, clientSecret });
  }

  /**
   * Start the OAuth dance on this machine.
   *
   * Returns as soon as the browser is open — the worker cannot finish inside
   * this request, because it is waiting on a person. Poll `status()` for the
   * outcome.
   */
  async authorise(force = false): Promise<AuthoriseResult | null> {
    return this.request<AuthoriseResult>('POST', '/youtube/authorise', { force });
  }

  async disconnect(): Promise<void> {
    await this.request('POST', '/youtube/disconnect', {});
  }

  async setDefaults(defaults: Record<string, unknown>): Promise<void> {
    await this.request('POST', '/youtube/defaults', defaults);
  }

  // ── What is on this disk ───────────────────────────────────────────────────
  //
  // Only reachable from the machine holding the files, which is the point.
  // Deleting a record is a statement about a review queue and happens from
  // anywhere; removing media is a statement about one computer.

  async storage(): Promise<StorageReport | null> {
    return this.request<StorageReport>('GET', '/storage');
  }

  /** Move files to the bin. Nothing is deleted, and nothing is irreversible. */
  async moveToTrash(paths: string[]): Promise<StorageReport | null> {
    await this.request('POST', '/storage/trash', { paths });
    return this.storage();
  }

  async restoreFromTrash(ids: string[]): Promise<StorageReport | null> {
    await this.request('POST', '/storage/trash/restore', { ids });
    return this.storage();
  }

  /**
   * Delete for good. The one irreversible call on this service.
   *
   * `all` is a flag rather than "an empty list means everything", because an
   * empty list is what a buggy caller sends by accident and emptying the bin
   * must never be the accident.
   */
  async purgeTrash(ids: string[], all = false): Promise<StorageReport | null> {
    await this.request('POST', '/storage/trash/purge', all ? { all: true } : { ids });
    return this.storage();
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<T | null> {
    const handshake = await this.connect();
    if (!handshake.available || !handshake.origin || !handshake.token) return null;

    let response: Response;
    try {
      response = await fetch(`${handshake.origin}${path}`, {
        method,
        headers: {
          Authorization: `Bearer ${handshake.token}`,
          ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch {
      // fetch rejects with a bare "Failed to fetch" when nothing is listening,
      // which is true and useless. There is only one thing that serves this
      // API, and it not running is the answer every single time — most often
      // after a restart, when nothing has started it again yet.
      throw new Error(
        'The ClipForge worker is not running on this machine, so its settings ' +
          'cannot be read. Start it, then press Recheck.',
      );
    }

    if (!response.ok) {
      // The worker sends its reason in the body, and it is the useful half —
      // "that does not look like a Google OAuth client id" beats "500".
      const detail = await response
        .json()
        .then((payload: { error?: string }) => payload.error)
        .catch(() => null);
      throw new Error(detail || `The worker refused that (${response.status}).`);
    }

    return (await response.json()) as T;
  }
}
