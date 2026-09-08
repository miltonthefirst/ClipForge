import { defineConfig, devices } from '@playwright/test';

/**
 * End-to-end against the Emulator Suite, with no real Firebase project and no
 * worker process.
 *
 * The worker is *stubbed by writing documents directly* rather than run: what
 * these tests assert is the PWA's half of the contract — that it renders what
 * the worker writes, and writes what the rules allow — and running a real
 * pipeline would make them slow, GPU-dependent and non-deterministic without
 * proving anything extra about the UI.
 *
 * Auth uses the emulator, which accepts any credential, so there is no real
 * Google sign-in in the loop.
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  forbidOnly: !!process.env['CI'],
  retries: process.env['CI'] ? 1 : 0,
  workers: 1,
  reporter: process.env['CI'] ? [['github'], ['list']] : [['list']],
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: 'http://127.0.0.1:4300',
    trace: 'retain-on-failure',
    // The emulator's sign-in popup is same-origin and needs no real network.
    permissions: [],
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'], channel: 'chromium-headless-shell' },
    },
  ],
  webServer: {
    // A production build served statically: closer to what actually ships than
    // a dev server, and it starts faster than `ng serve` rebuilds.
    command: 'npm run build && npx http-server dist/web/browser -p 4300 -s --proxy http://127.0.0.1:4300?',
    url: 'http://127.0.0.1:4300',
    reuseExistingServer: !process.env['CI'],
    timeout: 180_000,
  },
});
