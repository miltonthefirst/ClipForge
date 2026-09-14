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

test('the queue shows the poster, the hook and the score — and no more', async ({ page }) => {
  // Fifty of these is something to move through, so a card carries only what a
  // decision needs. Everything else is one click away, which the next test
  // asserts from the other side.
  //
  // These three used to assert the clip page's content without ever opening it,
  // and had been failing since the queue became one row per clip.
  const uid = await signIn(page);
  await write('candidates/cand-1', candidate(uid));
  await write('clips/clip-1', clip(uid));
  await write('clips/clip-1/preview/poster', preview());

  await expect(page.getByText('Most developers never realise this')).toBeVisible();
  await expect(page.getByText('87')).toBeVisible();
  await expect(page.locator('img[alt*="Poster frame"]')).toBeVisible();

  // The detail that used to be here, and is not any more.
  await expect(page.getByText('running your own hardware')).toHaveCount(0);
  await expect(page.locator('video')).toHaveCount(0);
});

test('opening a clip shows the hook, the score breakdown and the excerpt', async ({ page }) => {
  const uid = await signIn(page);
  await write('candidates/cand-1', candidate(uid));
  await write('clips/clip-1', clip(uid));
  await write('clips/clip-1/preview/poster', preview());

  await page.goto('/review/clip-1');

  await expect(page.getByText('Most developers never realise this')).toBeVisible();
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

  await page.goto('/review/clip-1');

  await expect(page.locator('video')).toHaveCount(0);
  await expect(page.locator('img[alt*="Poster frame"]')).toBeVisible();
  await expect(page.getByText('No cloud copy of this one')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Ask the worker to upload it' })).toBeVisible();
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

  await page.goto('/review/clip-1');

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

  // Polled, not read once. The Firestore SDK applies a write locally before the
  // server acknowledges it, so the card disappears from the queue *first* and a
  // single read here races the round trip — reading the value it was seeded
  // with, from a document the UI has already moved on from.
  await expect
    .poll(async () => (await readDoc('clips/clip-1'))?.['review'])
    .toEqual({ stringValue: 'APPROVED' });
});

test('rejecting a clip is recorded just as explicitly as approving', async ({ page }) => {
  const uid = await signIn(page);
  await write('candidates/cand-1', candidate(uid));
  await write('clips/clip-1', clip(uid));
  await write('clips/clip-1/preview/poster', preview());

  await page.getByRole('button', { name: 'Reject' }).click();

  await expect(page.getByText('Nothing waiting for review')).toBeVisible();
  await expect
    .poll(async () => (await readDoc('clips/clip-1'))?.['review'])
    .toEqual({ stringValue: 'REJECTED' });
});

test('a clip somebody else submitted is still this workspace’s to review', async ({
  page,
}) => {
  // One workspace, one library: `allow read: if isApproved()` deliberately does
  // not scope clips to whoever submitted them. Whoever is holding a phone is the
  // reviewer, and a queue that hid half the work would be the bug.
  //
  // This asserted the opposite until now, and passed only by racing the listener
  // and winning: the page starts empty, so an immediate check for the empty state
  // succeeds before the snapshot arrives.
  await signIn(page);
  await write('candidates/cand-1', candidate('someone-else'));
  await write('clips/clip-1', clip('someone-else'));

  await expect(page.getByText('Most developers never realise this')).toBeVisible();
});
