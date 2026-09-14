import { expect, test } from '@playwright/test';

import { clip, preview, signIn, wipe, write } from './helpers';

/**
 * Phase 8 in the browser: the publish gate, and the publish request that
 * follows it.
 *
 * These run against the real emulator with real security rules, so a test that
 * publishes successfully is also evidence that `firestore.rules` allowed it —
 * and a test that cannot is evidence the rules refused, not that a button was
 * greyed out. That distinction is the point: the UI copy of the gate is
 * advisory, and these assert the enforcing copy underneath it.
 *
 * The gate used to demand a rights attestation as well. That was removed; what
 * remains is the condition that was always the load-bearing one — a person
 * watched this clip and said yes.
 */

const NOW = '2026-09-08T12:00:00.000Z';

test.beforeEach(async () => {
  await wipe();
});

test('the publish queue explains itself when nothing is approved', async ({ page }) => {
  await signIn(page);
  await page.goto('/publish');

  await expect(page.getByText('Nothing approved yet')).toBeVisible();
  await expect(page.getByText('Approve a clip on the Review page')).toBeVisible();
});

test('an approved clip is publishable with no further questions asked', async ({ page }) => {
  const uid = await signIn(page);
  await write('clips/clip-1', clip(uid, { review: 'APPROVED' }));
  await write('clips/clip-1/preview/poster', preview());

  await page.goto('/publish');

  // Straight to the upload. Nothing between approval and publishing.
  await expect(page.getByRole('button', { name: /Publish to YouTube/ })).toBeVisible();
  await expect(page.getByText(/Why may you publish this/)).toHaveCount(0);
  await expect(page.getByRole('button', { name: /Record rights/ })).toHaveCount(0);
});

test('publishing an approved clip creates a PUBLISH job the rules accept', async ({ page }) => {
  const uid = await signIn(page);
  await write('clips/clip-1', clip(uid, { review: 'APPROVED' }));
  await write('clips/clip-1/preview/poster', preview());

  await page.goto('/publish');
  await page.getByRole('button', { name: /Publish to YouTube/ }).click();

  // The confirmation appears only after the write succeeded, so seeing it means
  // firestore.rules ran the gate and allowed it.
  await expect(page.getByText('nothing goes public by default')).toBeVisible();
});

test('a clip written before the removal, still carrying rights, publishes anyway', async ({
  page,
}) => {
  // Live documents were not migrated. An extra field the schema no longer
  // declares must be inert, not a reason the rules refuse the job.
  const uid = await signIn(page);
  await write(
    'clips/clip-1',
    clip(uid, {
      review: 'APPROVED',
      rights: { basis: 'OWN_CONTENT', attestedBy: uid, attestedAt: NOW, note: null },
    }),
  );
  await write('clips/clip-1/preview/poster', preview());

  await page.goto('/publish');
  await page.getByRole('button', { name: /Publish to YouTube/ }).click();

  await expect(page.getByText('nothing goes public by default')).toBeVisible();
});

test('an unlisted default is stated before the button is pressed, not after', async ({ page }) => {
  const uid = await signIn(page);
  await write('clips/clip-1', clip(uid, { review: 'APPROVED' }));
  await write('clips/clip-1/preview/poster', preview());

  await page.goto('/publish');
  await expect(page.getByRole('button', { name: 'Publish to YouTube (unlisted)' })).toBeVisible();
});

test('a published clip shows what went out instead of a publish button', async ({ page }) => {
  // Exit criterion 5, from the phone: what was posted, where, and when.
  const uid = await signIn(page);
  await write('clips/clip-1', clip(uid, { review: 'APPROVED' }));
  await write('clips/clip-1/preview/poster', preview());
  await write('clips/clip-1/publications/pub-1', {
    id: 'pub-1',
    clipId: 'clip-1',
    uid,
    platform: 'YOUTUBE',
    state: 'PUBLISHED',
    externalId: 'vid-1',
    externalUrl: 'https://www.youtube.com/watch?v=vid-1',
    privacy: 'unlisted',
    title: 'Most developers never realise this',
    description: null,
    tags: [],
    attempts: 1,
    quotaUnits: 1600,
    error: null,
    publishAt: null,
    createdAt: NOW,
    publishedAt: NOW,
  });

  await page.goto('/publish');

  await expect(page.getByText('Published', { exact: true })).toBeVisible();
  await expect(page.getByText('https://www.youtube.com/watch?v=vid-1')).toBeVisible();
  await expect(page.getByRole('button', { name: /Publish to YouTube/ })).toHaveCount(0);
});

test('a failed attempt is shown rather than silently retried', async ({ page }) => {
  const uid = await signIn(page);
  await write('clips/clip-1', clip(uid, { review: 'APPROVED' }));
  await write('clips/clip-1/preview/poster', preview());
  await write('clips/clip-1/publications/pub-1', {
    id: 'pub-1',
    clipId: 'clip-1',
    uid,
    platform: 'YOUTUBE',
    state: 'FAILED',
    externalId: null,
    externalUrl: null,
    attempts: 3,
    error: {
      type: 'YouTubeError',
      message: 'the YouTube refresh token was rejected',
      code: 'REAUTH_REQUIRED',
      traceback: null,
      retryable: false,
    },
    createdAt: NOW,
  });

  await page.goto('/publish');

  await expect(page.getByText('the YouTube refresh token was rejected')).toBeVisible();
});

test('a clip still awaiting review never appears in the publish queue', async ({ page }) => {
  const uid = await signIn(page);
  await write('clips/clip-1', clip(uid, { review: 'PENDING' }));

  await page.goto('/publish');
  await expect(page.getByText('Nothing approved yet')).toBeVisible();
});

test('a clip somebody else submitted is publishable from here', async ({ page }) => {
  // One workspace, one library. What gates publishing is the state of the clip,
  // not which account happened to submit the job that produced it — the same
  // rule publishing.rules.spec.ts asserts from the enforcing side.
  await signIn(page);
  await write('clips/clip-1', clip('someone-else', { review: 'APPROVED' }));
  await write('clips/clip-1/preview/poster', preview());

  await page.goto('/publish');
  await expect(page.getByRole('button', { name: /Publish to YouTube/ })).toBeVisible();
});
