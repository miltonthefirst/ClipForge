import { Injectable, inject, signal } from '@angular/core';
import { SwUpdate } from '@angular/service-worker';

import { BUILD } from '../build-info';

/**
 * Which build this is, and getting the latest one onto the phone.
 *
 * ## Which build
 *
 * `BUILD` is stamped from the repository's VERSION file before every build
 * (tools/stamp-build.mjs), and every deploy raises that number
 * (tools/release.py). So the version in the sidebar says exactly which release
 * this device is running, and "Updated to version X" says the moment a new one
 * arrived. Both copied from Sarungano.
 *
 * ## The latest build, always
 *
 * Sarungano's phone app has no caching service worker, so every open fetches
 * the newest page. ClipForge keeps Angular's (the installable, offline shell
 * of PLAN.md Phase 7), which on its own serves the cached build and fetches the
 * new one in the background, to be used only on the open after next. This
 * closes that gap:
 *
 * - it asks for an update when the app opens, when it comes back on screen and
 *   every ten minutes;
 * - a new build that arrives within the first seconds after opening, or while
 *   the app is in the background, is switched to at once (a reload nobody is
 *   in the middle of anything for);
 * - one that arrives while someone is using the app waits: "Update ready" in
 *   the header reloads now, and leaving the app does it quietly, so the next
 *   look is the new version;
 * - a cache the service worker cannot recover from reloads at once.
 *
 * Where there is no service worker (the desktop app, emulator builds, a
 * browser that refuses one), every open already gets the latest page, and
 * none of this runs.
 */

/** How long after opening a reload is still invisible. */
const QUIET_AFTER_OPEN_MS = 15_000;
const CHECK_EVERY_MS = 10 * 60_000;
const LAST_VERSION_KEY = 'clipforge.lastVersion';

@Injectable({ providedIn: 'root' })
export class AppUpdateService {
  private readonly updates = inject(SwUpdate, { optional: true });
  private readonly openedAt = Date.now();

  readonly version = BUILD.version;
  readonly label = BUILD.commit ? `${BUILD.version} (${BUILD.commit})` : BUILD.version;
  readonly builtAt = BUILD.builtAt;

  /** A newer build is downloaded and waiting for a reload. */
  readonly ready = signal(false);
  /** Set once, the first time this browser opens a new version. */
  readonly updatedTo = signal<string | null>(null);

  constructor() {
    this.noticeUpdate();

    const updates = this.updates;
    if (!updates?.isEnabled) return;

    updates.versionUpdates.subscribe((event) => {
      if (event.type !== 'VERSION_READY') return;
      if (this.quiet()) void this.reload();
      else this.ready.set(true);
    });
    updates.unrecoverable.subscribe(() => void this.reload(false));

    const check = (): void => {
      updates.checkForUpdate().catch(() => {
        // Offline, or the check itself failed: the next one will ask again.
      });
    };
    check();
    setInterval(check, CHECK_EVERY_MS);
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') check();
      // Leaving the app is the moment nobody is in the middle of anything.
      else if (this.ready()) void this.reload();
    });
  }

  /** Switch to the downloaded build and load it. */
  async reload(activate = true): Promise<void> {
    if (activate && this.updates?.isEnabled) {
      try {
        await this.updates.activateUpdate();
      } catch {
        // Already active, or nothing to activate: a plain reload still helps.
      }
    }
    document.location.reload();
  }

  private quiet(): boolean {
    return (
      document.visibilityState === 'hidden' || Date.now() - this.openedAt < QUIET_AFTER_OPEN_MS
    );
  }

  /** Compares this build with the last one this browser ran. A first visit
   * says nothing; storage that is blocked or cleared just means no notice. */
  private noticeUpdate(): void {
    try {
      const last = localStorage.getItem(LAST_VERSION_KEY);
      if (last && last !== BUILD.version) {
        this.updatedTo.set(BUILD.version);
        setTimeout(() => this.updatedTo.set(null), 8_000);
      }
      localStorage.setItem(LAST_VERSION_KEY, BUILD.version);
    } catch {
      // No storage: nothing to compare with.
    }
  }
}
