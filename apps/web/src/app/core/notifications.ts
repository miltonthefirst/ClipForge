import { Injectable, computed, effect, inject, signal, untracked } from '@angular/core';
import { SwPush } from '@angular/service-worker';
import type { AppNotification, NotificationKind, NotificationSettings } from '@clipforge/contracts';

import { WorkersRepository } from './data/workers';
import { FirebaseService } from './firebase';
import { FirestoreGateway, type Live } from './firestore/gateway';
import { SessionService } from './session';

/**
 * Notifications on this device (apps/worker/clipforge/notify).
 *
 * Copied in design from Sarungano: the person turns them on here, once, from
 * the Notifications page — never on their own at start — and the browser's
 * push token is kept at `users/{uid}/devices/{id}`, which only they can read or
 * write. The PC sends through Cloud Messaging; Angular's service worker shows
 * the push and opens its link on a tap. Each kind has a switch, and a list of
 * what was sent is kept, so one missed is still there.
 *
 * Push needs the service worker, so it is offered only where one runs: the
 * deployed app in a browser, or installed on a phone. iPhones need the app
 * added to the home screen first; the page says so.
 */

/** What each kind is, in the words the page shows. Mirrors clipforge/notify/kinds.py. */
export interface KindInfo {
  readonly kind: NotificationKind;
  readonly label: string;
  readonly detail: string;
  readonly adminOnly: boolean;
  readonly defaultOn: boolean;
}

export const KINDS: readonly KindInfo[] = [
  {
    kind: 'CLIPS_READY',
    label: 'Clips ready to review',
    detail: 'A job you started has finished with clips to look at.',
    adminOnly: false,
    defaultOn: true,
  },
  {
    kind: 'JOB_FAILED',
    label: 'A job failed',
    detail: 'One of yours, for good: no retries left.',
    adminOnly: false,
    defaultOn: true,
  },
  {
    kind: 'JOB_WAITING',
    label: 'A job is waiting for the worker',
    detail: 'Yours has sat in the queue ten minutes with no worker running.',
    adminOnly: false,
    defaultOn: true,
  },
  {
    kind: 'PUBLISHED',
    label: 'A clip went out',
    detail: 'A publish you asked for is up on YouTube.',
    adminOnly: false,
    defaultOn: true,
  },
  {
    kind: 'YOUTUBE_SIGN_IN',
    label: 'YouTube needs signing in again',
    detail: "A publish failed on the channel's sign-in.",
    adminOnly: true,
    defaultOn: true,
  },
  {
    kind: 'WORKER_DOWN',
    label: 'The worker will not stay up',
    detail: 'The agent on the PC stopped restarting it.',
    adminOnly: true,
    defaultOn: true,
  },
  {
    kind: 'SIGNUP',
    label: 'Someone signed up',
    detail: 'A new account is waiting for approval.',
    adminOnly: true,
    defaultOn: true,
  },
];

const DEVICE_KEY = 'clipforge.notifyDevice';
const REFRESH_EVERY_MS = 24 * 3600_000;
const LIST_LIMIT = 50;

@Injectable({ providedIn: 'root' })
export class NotificationsService {
  private readonly firebase = inject(FirebaseService);
  private readonly db = inject(FirestoreGateway);
  private readonly session = inject(SessionService);
  private readonly workers = inject(WorkersRepository);
  private readonly swPush = inject(SwPush, { optional: true });

  /** Whether this browser can be pushed to at all. */
  readonly supported =
    !!this.swPush?.isEnabled &&
    typeof window !== 'undefined' &&
    'Notification' in window &&
    'PushManager' in window;
  readonly permission = signal<NotificationPermission | 'unsupported'>(
    this.supported ? Notification.permission : 'unsupported',
  );
  /** This browser is registered for pushes on this account. */
  readonly enabled = signal(this.storedDevice() !== null && this.permission() === 'granted');
  readonly busy = signal(false);
  readonly error = signal<string | null>(null);

  private readonly heldList = signal<Live<AppNotification[]> | null>(null);
  private readonly heldSettings = signal<Live<NotificationSettings> | null>(null);

  /** The latest notifications for this account, newest first. */
  readonly list = computed(() => this.heldList()?.data() ?? null);
  readonly unread = computed(() => (this.list() ?? []).filter((item) => !item.readAt).length);
  readonly switches = computed(() => this.heldSettings()?.data()?.kinds ?? {});

  constructor() {
    effect(() => {
      const uid = this.session.approved() ? this.session.uid : null;
      untracked(() => this.follow(uid));
    });
  }

  /** Turn pushes on for this browser: the browser asks, then the token is kept. */
  async enable(): Promise<void> {
    const uid = this.session.uid;
    if (!this.supported || !uid) return;
    this.busy.set(true);
    this.error.set(null);
    try {
      const answer = await Notification.requestPermission();
      this.permission.set(answer);
      if (answer !== 'granted') {
        this.error.set(
          answer === 'denied'
            ? 'This browser has blocked notifications for ClipForge. Allow them in the site settings, then try again.'
            : 'Notifications were not allowed.',
        );
        return;
      }
      await this.register(uid);
      this.enabled.set(true);
    } catch (error) {
      this.error.set(error instanceof Error ? error.message : String(error));
    } finally {
      this.busy.set(false);
    }
  }

  /** Stop pushes to this browser; the list stays. */
  async disable(): Promise<void> {
    const uid = this.session.uid;
    const device = this.storedDevice();
    this.busy.set(true);
    try {
      if (uid && device) await this.db.remove(`users/${uid}/devices`, device);
      try {
        const { getMessaging, deleteToken } = await import('firebase/messaging');
        await deleteToken(getMessaging(this.firebase.app));
      } catch {
        // No token to delete; the device record is what the PC reads anyway.
      }
      this.forgetDevice();
      this.enabled.set(false);
    } finally {
      this.busy.set(false);
    }
  }

  async setKind(kind: NotificationKind, on: boolean): Promise<void> {
    const uid = this.session.uid;
    if (!uid) return;
    await this.db.merge(`users/${uid}/settings`, 'notifications', {
      kinds: { ...this.switches(), [kind]: on },
      updatedAt: new Date().toISOString(),
    });
  }

  isOn(info: KindInfo): boolean {
    const value = this.switches()[info.kind];
    return value === undefined ? info.defaultOn : value;
  }

  /** Ask the PC to send this account a test, through its agent. */
  async sendTest(): Promise<void> {
    const uid = this.session.uid;
    const live = this.workers.agents();
    try {
      const agent = live.data()?.[0] ?? (await waitFor(() => live.data()?.[0] ?? null));
      if (!uid || !agent)
        throw new Error('No PC has reported in yet, so there is nobody to send it.');
      await this.db.updateStamped(
        'agents',
        agent.agentId,
        { notifyTestRequestedBy: uid },
        'notifyTestRequestedAt',
      );
    } finally {
      live.release();
    }
  }

  async markRead(item: AppNotification): Promise<void> {
    const uid = this.session.uid;
    if (!uid || item.readAt) return;
    await this.db.update(`users/${uid}/notifications`, item.id, {
      readAt: new Date().toISOString(),
    });
  }

  async markAllRead(): Promise<void> {
    await Promise.all(
      (this.list() ?? []).filter((item) => !item.readAt).map((item) => this.markRead(item)),
    );
  }

  // ── Internals ──────────────────────────────────────────────────────────────

  private follow(uid: string | null): void {
    this.heldList()?.release();
    this.heldSettings()?.release();
    if (!uid) {
      this.heldList.set(null);
      this.heldSettings.set(null);
      return;
    }
    this.heldList.set(
      this.db.live<AppNotification>({
        collection: `users/${uid}/notifications`,
        orderBy: [['createdAt', 'desc']],
        limit: LIST_LIMIT,
      }),
    );
    this.heldSettings.set(
      this.db.liveDoc<NotificationSettings>(`users/${uid}/settings`, 'notifications'),
    );
    // A registered browser keeps its token fresh and says it is still here.
    if (this.enabled()) void this.refresh(uid);
  }

  private async register(uid: string): Promise<void> {
    const { getMessaging, getToken, isSupported } = await import('firebase/messaging');
    if (!(await isSupported())) {
      throw new Error(
        'This browser cannot receive push notifications. On an iPhone, add ClipForge to the home screen first and open it from there.',
      );
    }
    // Angular's service worker, so one worker both caches the app and shows pushes.
    const registration = await navigator.serviceWorker.ready;
    const token = await getToken(getMessaging(this.firebase.app), {
      serviceWorkerRegistration: registration,
    });
    const id = await deviceId(token);
    const now = new Date().toISOString();
    await this.db.create(
      `users/${uid}/devices`,
      { id, token, label: deviceLabel(), createdAt: now, lastSeenAt: now },
      id,
    );
    this.storeDevice(id);
  }

  private async refresh(uid: string): Promise<void> {
    try {
      const last = Number(localStorage.getItem(`${DEVICE_KEY}.seen`) ?? '0');
      if (Date.now() - last < REFRESH_EVERY_MS) return;
      const previous = this.storedDevice();
      await this.register(uid);
      const current = this.storedDevice();
      if (previous && current && previous !== current) {
        await this.db.remove(`users/${uid}/devices`, previous);
      }
      localStorage.setItem(`${DEVICE_KEY}.seen`, String(Date.now()));
    } catch {
      // Offline, or the browser withdrew permission: the next open tries again.
    }
  }

  private storedDevice(): string | null {
    try {
      return localStorage.getItem(DEVICE_KEY);
    } catch {
      return null;
    }
  }

  private storeDevice(id: string): void {
    try {
      localStorage.setItem(DEVICE_KEY, id);
    } catch {
      // No storage: the device still works, it just cannot be refreshed.
    }
  }

  private forgetDevice(): void {
    try {
      localStorage.removeItem(DEVICE_KEY);
      localStorage.removeItem(`${DEVICE_KEY}.seen`);
    } catch {
      // Nothing stored.
    }
  }
}

/** A stable id for a token: the first 20 hex digits of its SHA-256. */
export async function deviceId(token: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(token));
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('')
    .slice(0, 20);
}

/** "Android · Chrome", "iPhone · Safari": enough to tell devices apart. */
export function deviceLabel(agent: string = navigator.userAgent): string {
  const platform = /Android/i.test(agent)
    ? 'Android'
    : /iPhone|iPad/i.test(agent)
      ? 'iPhone'
      : /Windows/i.test(agent)
        ? 'Windows'
        : /Mac OS/i.test(agent)
          ? 'Mac'
          : 'Other';
  const browser = /Edg\//.test(agent)
    ? 'Edge'
    : /Firefox\//.test(agent)
      ? 'Firefox'
      : /Chrome\//.test(agent)
        ? 'Chrome'
        : /Safari\//.test(agent)
          ? 'Safari'
          : 'Browser';
  return `${platform} · ${browser}`;
}

async function waitFor<T>(read: () => T | null, ms = 3_000): Promise<T | null> {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    const value = read();
    if (value !== null) return value;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  return read();
}
