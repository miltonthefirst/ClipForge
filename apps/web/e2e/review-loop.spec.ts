import { expect, test } from '@playwright/test';

import { candidate, clip, job, preview, readDoc, signIn, wipe, write } from './helpers';

/**
 * Phase 7, exit criterion 2: submit → progress → review → approve, against the
 * Emulator Suite with a stubbed worker.
 *
 * The worker is stubbed by writing documents directly rather than run. What
 * these assert is the PWA's half of the contract — that it renders what the
 * worker writes, and writes only what the rules allow — and running a real
 * pipeline would be slow, GPU-dependent and non-deterministic without proving
 * anything more about the UI.
 *
 * Criteria 4 and 6 ride along: every view's empty state, and the
 * poster-plus-metadata fallback when no video can be played — which on the free
 * tier is the normal case, not an edge one.
 */
test.beforeEach(async () => {
  await wipe();
});

test('an empty queue explains what to do next rather than showing nothing', async ({
  page,
}) => {
  await signIn(page);

  await expect(page.getByText('Nothing waiting for review')).toBeVisible();
  await expect(page.getByText('Submit a video on the Jobs page')).toBeVisible();
});

test('submitting a URL creates a QUEUED job the worker could claim', async ({ page }) => {
  await signIn(page);
  await page.goto('/jobs');
  await expect(page.getByText('No jobs yet')).toBeVisible();

  await page.getByPlaceholder('YouTube URL').fill('https://youtu.be/dQw4w9WgXcQ');
  await page.getByRole('button', { name: 'Add' }).click();

  // Appears via the live listener, not an optimistic local update — so this
  // also proves the write satisfied firestore.rules.
  await expect(page.getByText('https://youtu.be/dQw4w9WgXcQ')).toBeVisible();
  await expect(page.getByText('QUEUED')).toBeVisible();
});

test('per-stage progress is shown, not an indeterminate spinner', async ({ page }) => {
  const uid = await signIn(page);
  await write('jobs/job-1', job(uid));

  await page.goto('/jobs');

  // This is what the checkpointed stage model buys the UI.
  await expect(page.getByText('1 of 4 stages')).toBeVisible();
  await expect(page.getByText('TRANSCRIBE')).toBeVisible();
});

test('a failed job shows the worker’s reason, not a stack trace', async ({ page }) => {
  const uid = await signIn(page);
  await write(
    'jobs/job-1',
    job(uid, {
      status: 'FAILED',
      error: {
        type: 'IngestError',
        message: 'This video is age-restricted and cannot be downloaded without signing in.',
        code: 'AGE_RESTRICTED',
        traceback: null,
        retryable: false,
      },
    }),
  );

  await page.goto('/jobs');

  await expect(page.getByText('age-restricted')).toBeVisible();
});

test('reviewing a clip shows poster, hook, score breakdown and excerpt', async ({ page }) => {
  const uid = await signIn(page);
  await write('candidates/cand-1', candidate(uid));
  await write('clips/clip-1', clip(uid));
  await write('clips/clip-1/preview/poster', preview());

  await expect(page.getByText('Most developers never realise this')).toBeVisible();
  await expect(page.getByText('87')).toBeVisible();
  await expect(page.getByText('Hook', { exact: true })).toBeVisible();
  await expect(page.getByText('running your own hardware')).toBeVisible();
  await expect(page.locator('img[alt*="Poster frame"]')).toBeVisible();
});

test('a clip with no playable URL says so instead of showing a broken player', async ({
  page,
}) => {
  // The free tier's normal case: the file is on the worker, and this browser is
  // not on the worker's machine.
  const uid = await signIn(page);
  await write('candidates/cand-1', candidate(uid));
  await write('clips/clip-1', clip(uid));
  await write('clips/clip-1/preview/poster', preview());

  await expect(page.getByText('playable on the worker machine')).toBeVisible();
  await expect(page.locator('video')).toHaveCount(0);
  await expect(page.getByText('stay on the machine that rendered them')).toBeVisible();
});

test('a clip with a playbackUrl gets a real player — the Blaze upgrade path', async ({
  page,
}) => {
  const uid = await signIn(page);
  await write('candidates/cand-1', candidate(uid));
  await write(
    'clips/clip-1',
    clip(uid, { location: 'REMOTE', playbackUrl: 'http://127.0.0.1:4300/clip.mp4' }),
  );
  await write('clips/clip-1/preview/poster', preview());

  // The element exists and is wired to the URL. Whether the file resolves is the
  // server's business, not the UI's — and this is the only branch that changes
  // when Blaze is enabled.
  await expect(page.locator('video')).toHaveCount(1);
});

test('approving a clip writes the decision and clears it from the queue', async ({ page }) => {
  const uid = await signIn(page);
  await write('candidates/cand-1', candidate(uid));
  await write('clips/clip-1', clip(uid));
  await write('clips/clip-1/preview/poster', preview());

  await page.getByRole('button', { name: 'Approve' }).click();

  // Gone from the pending queue, because the listener is filtered on review.
  await expect(page.getByText('Nothing waiting for review')).toBeVisible();

  const stored = await readDoc('clips/clip-1');
  expect(stored?.['review']).toEqual({ stringValue: 'APPROVED' });
});

test('rejecting a clip is recorded just as explicitly as approving', async ({ page }) => {
  const uid = await signIn(page);
  await write('candidates/cand-1', candidate(uid));
  await write('clips/clip-1', clip(uid));
  await write('clips/clip-1/preview/poster', preview());

  await page.getByRole('button', { name: 'Reject' }).click();

  await expect(page.getByText('Nothing waiting for review')).toBeVisible();
  const stored = await readDoc('clips/clip-1');
  expect(stored?.['review']).toEqual({ stringValue: 'REJECTED' });
});

test('another user’s clip never appears in this user’s queue', async ({ page }) => {
  // The rules enforce this; the UI must also not *ask* for it, or the listener
  // fails outright rather than returning nothing.
  await signIn(page);
  await write('candidates/cand-1', candidate('someone-else'));
  await write('clips/clip-1', clip('someone-else'));

  await expect(page.getByText('Nothing waiting for review')).toBeVisible();
});
