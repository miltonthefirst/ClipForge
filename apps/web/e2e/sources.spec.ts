import { expect, test } from '@playwright/test';

import { signIn, wipe, write } from './helpers';

/**
 * The library: what is on the worker's disk, and what has earned its place.
 *
 * Sources are the expensive thing and the irreplaceable one — a download is
 * bandwidth and minutes, a video since taken down is gone. The collector is
 * always reclaiming something, so what this page has to get right is the order
 * (most-used first, because that is what the reviewer asked to see) and the
 * honesty about what a browser can do with a file it cannot reach.
 */

function source(uid: string, over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 'src-1',
    uid,
    provider: 'youtube',
    externalId: 'abc12345678',
    url: 'https://www.youtube.com/watch?v=abc12345678',
    title: 'A football match',
    channel: 'CANAL+',
    durationSec: 95,
    localPath: 'C:/workspace/sources/abc.mp4',
    sizeBytes: 5 * 1_048_576,
    pinned: false,
    useCount: 0,
    kind: 'video',
    createdAt: '2026-09-01T12:00:00.000Z',
    ...over,
  };
}

test.beforeEach(async () => {
  await wipe();
});

test('an empty library says what would fill it rather than showing nothing', async ({ page }) => {
  await signIn(page);
  await page.goto('/sources');

  await expect(page.getByText('Nothing downloaded yet')).toBeVisible();
  await expect(page.getByText(/Submit a video on the Jobs page/)).toBeVisible();
});

test('the most-used source comes first, which is the whole point of the list', async ({ page }) => {
  const uid = await signIn(page);
  await write('sources/src-1', source(uid, { id: 'src-1', title: 'Used once', useCount: 1 }));
  await write('sources/src-2', source(uid, { id: 'src-2', title: 'Used often', useCount: 7 }));
  await write('sources/src-3', source(uid, { id: 'src-3', title: 'Never used', useCount: 0 }));

  await page.goto('/sources');

  const titles = page.locator('li p.font-medium');
  await expect(titles).toHaveText(['Used often', 'Used once', 'Never used']);
});

test('a source says how often it has been used, in words', async ({ page }) => {
  const uid = await signIn(page);
  await write('sources/src-1', source(uid, { useCount: 3 }));

  await page.goto('/sources');

  // Scoped to the row: the header also carries a size, because one source is
  // also the whole library, and that total is a separate claim worth its own
  // assertion below.
  const row = page.locator('li').first();
  await expect(row.getByText('used 3 times')).toBeVisible();
  await expect(row.getByText('CANAL+')).toBeVisible();
  await expect(row.getByText('1:35')).toBeVisible();
  await expect(row.getByText('5.0 MB')).toBeVisible();

  await expect(page.getByText("5.0 MB on the worker's disk")).toBeVisible();
});

test('the page says which files the collector will keep and which it may take', async ({ page }) => {
  // The collector is otherwise invisible, and a library whose contents cannot
  // be predicted is one the operator keeps re-downloading.
  const uid = await signIn(page);
  await write('sources/src-1', source(uid, { id: 'src-1', title: 'Kept', useCount: 2 }));
  await write('sources/src-2', source(uid, { id: 'src-2', title: 'At risk', useCount: 1 }));

  await page.goto('/sources');

  await expect(page.getByText('kept', { exact: true })).toBeVisible();
  await expect(page.getByText('may be reclaimed if space runs short')).toBeVisible();
});

test('a pinned source is kept however little it has been used', async ({ page }) => {
  const uid = await signIn(page);
  await write('sources/src-1', source(uid, { pinned: true, useCount: 0 }));

  await page.goto('/sources');

  await expect(page.getByText('kept · pinned')).toBeVisible();
});

test('a browser is offered the original, because the file itself is out of reach', async ({
  page,
}) => {
  const uid = await signIn(page);
  await write('sources/src-1', source(uid));

  await page.goto('/sources');

  await expect(page.getByRole('link', { name: /^Open/ })).toHaveAttribute(
    'href',
    'https://www.youtube.com/watch?v=abc12345678',
  );
  // And it says where removal happens rather than offering a button that
  // cannot work from here.
  await expect(page.getByText(/Storage page in the desktop app/)).toBeVisible();
});

test('the music tab shows tracks and the video tab does not', async ({ page }) => {
  const uid = await signIn(page);
  await write('sources/src-1', source(uid, { id: 'src-1', title: 'A match', kind: 'video' }));
  await write(
    'sources/src-2',
    source(uid, { id: 'src-2', title: 'A backing track', kind: 'music' }),
  );

  await page.goto('/sources?tab=music');
  await expect(page.getByText('A backing track')).toBeVisible();
  await expect(page.getByText('A match')).toHaveCount(0);

  await page.goto('/sources?tab=video');
  await expect(page.getByText('A match')).toBeVisible();
  await expect(page.getByText('A backing track')).toHaveCount(0);
});

test('a source written before kinds existed still reads as video', async ({ page }) => {
  // Every source on the live project predates the field, and a library that
  // hid all of them behind a tab nobody had chosen would look empty.
  const uid = await signIn(page);
  const legacy = source(uid, { title: 'From before' });
  delete legacy['kind'];
  await write('sources/src-1', legacy);

  await page.goto('/sources?tab=video');

  await expect(page.getByText('From before')).toBeVisible();
});
