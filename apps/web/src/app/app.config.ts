import { ApplicationConfig, isDevMode, provideBrowserGlobalErrorListeners } from '@angular/core';
import { provideRouter, withComponentInputBinding } from '@angular/router';
import { provideServiceWorker } from '@angular/service-worker';

import { loadConfig } from '../environments';
import { routes } from './app.routes';

/**
 * The service worker is registered only when the app is pointed at a real
 * project — never in emulator mode.
 *
 * `!isDevMode()` alone would be wrong here. The Playwright suite runs a
 * production build against the Emulator Suite, so a dev-mode check would
 * register a worker that caches the app shell between tests and races
 * registration against the first navigation. Keying on `useEmulators` instead
 * says what is actually meant: offline support is for the deployed app, and the
 * emulator-backed one neither needs it nor should have it.
 *
 * `serviceWorker` overrides both when a host has a reason to — the Tauri shell
 * points at the real project and still must not register one.
 */
const config = loadConfig();
const useServiceWorker = config.serviceWorker ?? (!config.useEmulators && !isDevMode());

export const appConfig: ApplicationConfig = {
  providers: [
    provideBrowserGlobalErrorListeners(),
    // Route parameters arrive as component inputs, so a detail page reads
    // `id` like any other input instead of subscribing to ActivatedRoute.
    provideRouter(routes, withComponentInputBinding()),
    provideServiceWorker('ngsw-worker.js', {
      enabled: useServiceWorker,
      // The review queue is a live Firestore listener, so registering during
      // startup would compete with the connection the user is waiting on.
      registrationStrategy: 'registerWhenStable:30000',
    }),
  ],
};
