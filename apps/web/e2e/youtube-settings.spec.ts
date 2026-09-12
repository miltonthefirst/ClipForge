import { expect, test } from '@playwright/test';

import { signIn, wipe, write } from './helpers';

/**
 * The YouTube settings page, seen from a browser.
 *
 * A browser is exactly the context where half of this page cannot work: the
 * worker's control API answers on 127.0.0.1 and demands a token a tab has no
 * way to read, which is what keeps the client secret off Firestore
 * (docs/adr/0011-local-control-api.md).
 *
 * So what these assert is that the page *says so* rather than offering a form
 * that would fail — and that the half which does work anywhere, the connection
 * state read from Firestore, still renders.
 */

const NOW = '2026-09-10T08:00:00.000Z';

function channel(overrides: Record<string, unknown> = {}) {
  return {
    id: 'youtube-primary',
    uid: 'worker-1',
    platform: 'YOUTUBE',
    label: 'YouTube',
    isDefault: true,
    externalChannelId: null,
    externalChannelTitle: null,
    connection: 'NEEDS_AUTH',
    connectionMessage: 'The client is set up but nobody has authorised it.',
    authorisedAt: null,
    checkedAt: NOW,
    defaults: {
      privacy: 'unlisted',
      categoryId: '27',
      tags: ['ai', 'local-first'],
      titleSuffix: '#shorts',
      descriptionTemplate: 'Made with ClipForge.',
    },
    quotaDay: null,
    quotaUsedUnits: null,
    uploadsRemainingToday: 4,
    createdAt: NOW,
    updatedAt: NOW,
    ...overrides,
  };
}

test.beforeEach(async () => {
  await wipe();
});

test('a browser is told the credential form needs the desktop app', async ({ page }) => {
  await signIn(page);
  await page.goto('/settings/youtube');

  // `.first()`: the phrase appears twice on purpose — once for the credential
  // form and once beside the defaults Save button, because a person who reaches
  // either should not have to scroll to find out why it will not work.
  await expect(page.getByText('needs the desktop app').first()).toBeVisible();
  // Not merely hidden — absent. A disabled form still invites a person to try.
  await expect(page.getByPlaceholder(/apps.googleusercontent.com/)).toHaveCount(0);
});

test('connection state still renders from Firestore, because it is not secret', async ({
  page,
}) => {
  await signIn(page);
  await write('channels/youtube-primary', channel());
  await page.goto('/settings/youtube');

  await expect(page.getByText('NEEDS_AUTH')).toBeVisible();
  await expect(page.getByText('nobody has authorised it')).toBeVisible();
  await expect(page.getByText('4')).toBeVisible();
});

test('the saved defaults are shown, so a phone can at least read them', async ({ page }) => {
  await signIn(page);
  await write('channels/youtube-primary', channel());
  await page.goto('/settings/youtube');

  await expect(page.locator('select').first()).toHaveValue('unlisted');
  await expect(page.locator('select').nth(1)).toHaveValue('27');
  await expect(page.getByRole('textbox').filter({ hasText: '' }).first()).toBeVisible();
  await expect(page.locator('input[type=text]').first()).toHaveValue('ai, local-first');
});

test('saving defaults is disabled off the desktop, and says why', async ({ page }) => {
  await signIn(page);
  await write('channels/youtube-primary', channel());
  await page.goto('/settings/youtube');

  await expect(page.getByRole('button', { name: 'Save defaults' })).toBeDisabled();
  await expect(page.getByText('needs the desktop app for now')).toBeVisible();
});

test('the page never displays a secret, even when one exists on the worker', async ({ page }) => {
  // There is no path for a secret to reach this page at all — it is not in
  // Firestore and the control API is unreachable — but asserting the absence is
  // cheap, and this is the page where a future change would most plausibly leak
  // one.
  await signIn(page);
  await write('channels/youtube-primary', channel());
  await page.goto('/settings/youtube');

  const body = await page.locator('body').innerText();
  expect(body).not.toMatch(/client[_ ]?secret["' :]/i);
  expect(body).not.toMatch(/GOCSPX/); // the prefix Google gives client secrets
});
