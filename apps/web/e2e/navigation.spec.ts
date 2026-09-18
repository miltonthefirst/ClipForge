import { expect, test } from '@playwright/test';

import { clip, preview, signIn, wipe, write } from './helpers';

/**
 * Coming back to a screen you were just on.
 *
 * The listener is held for fifteen minutes past its last reader, so the *list*
 * was already instant on return. Everything derived per row was not: the poster
 * and the candidate window were fetched by the component, and the router
 * destroys the component on the way out — so Review to Jobs to Review re-read a
 * quarter of a megabyte of base64 to redraw what had been on screen a second
 * earlier. On a desktop that is a flicker. On a phone it is the complaint.
 */

test.beforeEach(async () => {
  await wipe();
});

test('coming back to the review queue renders without fetching again', async ({ page }) => {
  const uid = await signIn(page);
  for (let i = 1; i <= 6; i++) {
    await write(`clips/clip-${i}`, clip(uid, { id: `clip-${i}`, review: 'PENDING' }));
    await write(`clips/clip-${i}/preview/poster`, preview());
  }

  await page.goto('/review');
  await expect(page.locator('img[alt*="Poster frame"]').first()).toBeVisible();

  await page.getByRole('link', { name: 'Jobs', exact: true }).click();
  await expect(page).toHaveURL(/\/jobs/);

  // Timed rather than counted. The held listener keeps a streaming channel open
  // and its long-poll requests are indistinguishable by URL from anything else
  // on it, so counting requests would be asserting the transport rather than
  // the behaviour. What is being claimed is that nothing is *fetched*: the
  // queue is already open and every poster and candidate is a write-once
  // document already in hand, so the cards are assembled from memory.
  const started = Date.now();
  await page.getByRole('link', { name: 'Review', exact: true }).click();
  await expect(page.locator('img[alt*="Poster frame"]')).toHaveCount(6);
  const elapsed = Date.now() - started;

  // Six posters at ~28 KB each could not be re-read from Firestore this fast,
  // even against an emulator on the same machine. Generous on purpose: this is
  // here to catch the cache being lost, not to police a few milliseconds.
  expect(elapsed).toBeLessThan(400);
});

test('the posters that come back are the right ones', async ({ page }) => {
  // A cache keyed wrongly is worse than no cache: it puts one clip's picture on
  // another clip's card, and nothing about that looks like a bug.
  const uid = await signIn(page);
  await write('clips/clip-1', clip(uid, { id: 'clip-1', review: 'PENDING', hook: 'First clip' }));
  await write('clips/clip-1/preview/poster', preview({ widthPx: 111 }));
  await write('clips/clip-2', clip(uid, { id: 'clip-2', review: 'PENDING', hook: 'Second clip' }));
  await write('clips/clip-2/preview/poster', preview({ widthPx: 222 }));

  await page.goto('/review');
  await expect(page.locator('img[alt*="Poster frame"]')).toHaveCount(2);
  const before = await page
    .locator('img[alt*="Poster frame"]')
    .evaluateAll((els) => els.map((e) => (e as HTMLImageElement).src.slice(-24)));

  await page.getByRole('link', { name: 'Jobs', exact: true }).click();
  await expect(page).toHaveURL(/\/jobs/);
  await page.getByRole('link', { name: 'Review', exact: true }).click();
  await expect(page.locator('img[alt*="Poster frame"]')).toHaveCount(2);

  const after = await page
    .locator('img[alt*="Poster frame"]')
    .evaluateAll((els) => els.map((e) => (e as HTMLImageElement).src.slice(-24)));
  expect(after).toEqual(before);
});
