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

  private async request<T>(method: string, path: string, body?: unknown): Promise<T | null> {
    const handshake = await this.connect();
    if (!handshake.available || !handshake.origin || !handshake.token) return null;

    const response = await fetch(`${handshake.origin}${path}`, {
      method,
      headers: {
        Authorization: `Bearer ${handshake.token}`,
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });

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
