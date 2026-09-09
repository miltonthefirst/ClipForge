import { expect, test } from '@playwright/test';

import { signIn, wipe } from './helpers';

/**
 * The app shell as it is actually served — the parts a deploy depends on.
 *
 * These run against the same production build the deploy script publishes, so
 * they assert the two contracts that otherwise only fail in front of a user:
 * that the configuration markers survive the build, and that the service worker
 * stays out of the way when the app is pointed at emulators.
 */

test.beforeEach(async () => {
  await wipe();
});

test('the deploy configuration markers survive into the built HTML', async ({ request }) => {
  // tools/deploy.ps1 replaces everything between these to point the app at a
  // real project, and fails loudly if it cannot find them. That failure is late
  // — it happens at deploy time, on a machine someone is waiting at. This is the
  // same check, at CI time.
  const html = await (await request.get('/index.html')).text();

  expect(html).toContain('/* CLIPFORGE-CONFIG-START */');
  expect(html).toContain('/* CLIPFORGE-CONFIG-END */');
  expect(html.indexOf('CLIPFORGE-CONFIG-START')).toBeLessThan(
    html.indexOf('CLIPFORGE-CONFIG-END'),
  );
});

test('the built app defaults to emulators, so a misconfigured deploy is inert', async ({
  request,
}) => {
  // The default must never be a real project. If the deploy script fails to
  // substitute, what ships should point at 127.0.0.1 and fail obviously — not
  // reach into whatever project the placeholder happened to name.
  const html = await (await request.get('/index.html')).text();
  const block = html.slice(
    html.indexOf('CLIPFORGE-CONFIG-START'),
    html.indexOf('CLIPFORGE-CONFIG-END'),
  );

  expect(block).not.toContain('apiKey');
  expect(block).not.toContain('useEmulators: false');
});

test('the manifest is served, so the app is installable once deployed', async ({ request }) => {
  const response = await request.get('/manifest.webmanifest');
  expect(response.ok()).toBe(true);

  const manifest = (await response.json()) as {
    name?: string;
    start_url?: string;
    display?: string;
    icons?: { sizes?: string }[];
  };
  expect(manifest.name).toBeTruthy();
  expect(manifest.display).toBe('standalone');
  // Chrome will not offer to install without a 192px and a 512px icon.
  const sizes = (manifest.icons ?? []).map((icon) => icon.sizes);
  expect(sizes).toContain('192x192');
  expect(sizes).toContain('512x512');
});

test('no service worker registers while the app is pointed at emulators', async ({ page }) => {
  // `provideServiceWorker` is gated on `useEmulators`, not on `isDevMode()`.
  // This suite runs a production build, so a dev-mode gate would register a
  // worker here — caching the shell between tests and racing the first
  // navigation against registration.
  //
  // Note the registration strategy is `registerWhenStable:30000`, so this
  // asserts "has not registered by the time the app is usable" rather than
  // "can never register". That is the window that would actually affect these
  // tests, which is what this is protecting.
  await signIn(page);
  await expect(page.getByText('Nothing waiting for review')).toBeVisible();

  const registrations = await page.evaluate(async () => {
    if (!('serviceWorker' in navigator)) return -1;
    return (await navigator.serviceWorker.getRegistrations()).length;
  });

  expect(registrations).toBe(0);
});
