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
 *
 * The queue is now a list that clicks through to a clip's own publish page, so
 * most of what used to be asserted on `/publish` is asserted on `/publish/:id`.
 * The one thing that stayed on the row is the publish itself.
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
  await expect(page.getByRole('button', { name: /^Publish \(/ })).toBeVisible();
  await expect(page.getByText(/Why may you publish this/)).toHaveCount(0);
  await expect(page.getByRole('button', { name: /Record rights/ })).toHaveCount(0);
});

test('the queue opens the clip rather than making the row a form', async ({ page }) => {
  // What the redesign is for. The row carries the poster, the title and the
  // state; everything that governs what an upload says is on the clip's page.
  const uid = await signIn(page);
  await write('clips/clip-1', clip(uid, { review: 'APPROVED' }));
  await write('clips/clip-1/preview/poster', preview());

  await page.goto('/publish');
  await expect(page.getByLabel('Tags', { exact: true })).toHaveCount(0);

  await page.getByRole('link', { name: 'Most developers never realise this' }).click();

  await expect(page).toHaveURL(/\/publish\/clip-1$/);
  await expect(page.getByRole('heading', { name: 'What goes out' })).toBeVisible();
});

test('publishing an approved clip creates a PUBLISH job the rules accept', async ({ page }) => {
  const uid = await signIn(page);
  await write('clips/clip-1', clip(uid, { review: 'APPROVED' }));
  await write('clips/clip-1/preview/poster', preview());

  await page.goto('/publish');
  await page.getByRole('button', { name: /^Publish \(/ }).click();

  // The row reports the job it just created, and reports it from the job
  // itself rather than from a confirmation held in memory — which is what
  // makes a queued publish survive a reload.
  await expect(page.getByText('Nothing picks this up until the worker is running')).toBeVisible();
  await expect(page.getByRole('button', { name: /^Publish \(/ })).toHaveCount(0);
});

test('a queued publish still reads as queued after a reload', async ({ page }) => {
  // The bug the state model exists to prevent: queue an upload, come back, and
  // the queue offers Publish again — so the same video goes out twice.
  const uid = await signIn(page);
  await write('clips/clip-1', clip(uid, { review: 'APPROVED' }));
  await write('clips/clip-1/preview/poster', preview());

  await page.goto('/publish');
  await page.getByRole('button', { name: /^Publish \(/ }).click();
  await expect(page.getByText('Queued', { exact: true })).toBeVisible();

  await page.reload();

  await expect(page.getByText('Queued', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: /^Publish \(/ })).toHaveCount(0);
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
  await page.getByRole('button', { name: /^Publish \(/ }).click();

  await expect(page.getByText('Queued', { exact: true })).toBeVisible();
});

test('an unlisted default is stated before the button is pressed, not after', async ({ page }) => {
  const uid = await signIn(page);
  await write('clips/clip-1', clip(uid, { review: 'APPROVED' }));
  await write('clips/clip-1/preview/poster', preview());

  await page.goto('/publish');
  await expect(page.getByRole('button', { name: 'Publish (unlisted)' })).toBeVisible();

  await page.goto('/publish/clip-1');
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

  // The queue keeps it, under Published, rather than dropping it on success.
  await expect(page.getByRole('heading', { name: /^Published/ })).toBeVisible();
  await expect(page.getByRole('button', { name: /^Publish \(/ })).toHaveCount(0);

  await page.goto('/publish/clip-1');

  await expect(page.getByRole('heading', { name: 'On YouTube' })).toBeVisible();
  await expect(page.getByText('https://www.youtube.com/watch?v=vid-1')).toBeVisible();
  await expect(page.getByText('1,600 quota units')).toBeVisible();
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
  await expect(page.getByText('Failed', { exact: true })).toBeVisible();
  // A retry is offered, and named as one — a failure the operator can see and
  // act on beats one that is quietly attempted again.
  await expect(page.getByRole('button', { name: /^Try again \(/ })).toBeVisible();

  await page.goto('/publish/clip-1');
  await expect(page.getByRole('heading', { name: 'The last attempt failed' })).toBeVisible();
  await expect(page.getByText('retrying will not help on its own')).toBeVisible();
});

test('the review page refuses a second publish behind the first', async ({ page }) => {
  // Reviewing and publishing sit on one screen on purpose, so that screen has
  // to know the same thing the queue does: a publish already asked for. It used
  // to read only publications, and the worker does not write one until it
  // starts — so a job queued for Friday left nothing here to notice, and the
  // button stayed on offer.
  const uid = await signIn(page);
  await write('clips/clip-1', clip(uid, { review: 'APPROVED' }));
  await write('clips/clip-1/preview/poster', preview());

  await page.goto('/review/clip-1');
  await page.getByRole('button', { name: /Publish to YouTube/ }).click();

  await expect(page.getByText('Nothing picks this up until the worker is running')).toBeVisible();
  await expect(page.getByRole('button', { name: /Publish to YouTube/ })).toHaveCount(0);

  // And it survives a reload, which is where the in-memory confirmation it
  // replaced used to give up.
  await page.reload();
  await expect(page.getByText('Nothing picks this up until the worker is running')).toBeVisible();
  await expect(page.getByRole('button', { name: /Publish to YouTube/ })).toHaveCount(0);

  // Withdrawing it is the counterpart: there is no publication to retract, so
  // cancelling the job is the only way to change your mind.
  await page.getByRole('button', { name: 'Call it off' }).click();
  await expect(page.getByRole('button', { name: /Publish to YouTube/ })).toBeVisible();
});

test('the review page points at the full record rather than duplicating it', async ({ page }) => {
  const uid = await signIn(page);
  await write('clips/clip-1', clip(uid, { review: 'APPROVED' }));
  await write('clips/clip-1/preview/poster', preview());

  await page.goto('/review/clip-1');
  await page.getByRole('link', { name: 'Publishing details →' }).click();

  await expect(page).toHaveURL(/\/publish\/clip-1$/);
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
  await expect(page.getByRole('button', { name: /^Publish \(/ })).toBeVisible();
});
