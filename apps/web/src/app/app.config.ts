import { ApplicationConfig, isDevMode, provideBrowserGlobalErrorListeners } from '@angular/core';
import { provideRouter } from '@angular/router';
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
 */
const useServiceWorker = !loadConfig().useEmulators && !isDevMode();

export const appConfig: ApplicationConfig = {
  providers: [
    provideBrowserGlobalErrorListeners(),
    provideRouter(routes),
    provideServiceWorker('ngsw-worker.js', {
      enabled: useServiceWorker,
      // The review queue is a live Firestore listener, so registering during
      // startup would compete with the connection the user is waiting on.
      registrationStrategy: 'registerWhenStable:30000',
    }),
  ],
};
