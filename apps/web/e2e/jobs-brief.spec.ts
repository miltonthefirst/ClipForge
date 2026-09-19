import { expect, test } from '@playwright/test';

import { listDocs, plain, signIn, wipe } from './helpers';

/**
 * The brief on a clip job: what the form writes, and what it does not.
 *
 * An untouched panel must write no options at all, so a job submitted
 * without opening it is the document it always was; an opened one writes
 * exactly the shape the rules accept.
 */

function unwrap(fields: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(fields).map(([key, held]) => [key, plain(held)]));
}

const URL = 'https://www.youtube.com/watch?v=aaaaaaaaaaa';

test.beforeEach(async () => {
  await wipe();
});

test('a submission with the panel untouched writes no brief', async ({ page }) => {
  await signIn(page);
  await page.goto('/jobs');
  await page.getByPlaceholder(/YouTube URL/).fill(URL);
  await page.getByRole('button', { name: 'Add', exact: true }).click();
  // The box empties only once the write is acknowledged; the row can appear
  // earlier, from the listener's local echo of the pending write.
  await expect(page.getByPlaceholder(/YouTube URL/)).toHaveValue('');

  await expect
    .poll(async () => (await listDocs('jobs')).map(unwrap) as { clipOptions: unknown }[])
    .toHaveLength(1);
  const jobs = (await listDocs('jobs')).map(unwrap) as { clipOptions: unknown }[];
  expect(jobs[0]?.clipOptions).toBeNull();
});

test('a brief, a count and a length ride on the job', async ({ page }) => {
  await signIn(page);
  await page.goto('/jobs');
  await page.getByPlaceholder(/YouTube URL/).fill(URL);
  await page.getByRole('button', { name: 'What to look for' }).click();
  await page.getByRole('textbox', { name: 'What to clip' }).fill('the goals and the penalty shout');
  await page.getByRole('spinbutton', { name: 'How many, at most' }).fill('3');
  await page.getByRole('combobox', { name: 'How long' }).selectOption('medium');
  await page.getByRole('button', { name: 'Add', exact: true }).click();
  await expect(page.getByPlaceholder(/YouTube URL/)).toHaveValue('');

  await expect.poll(async () => (await listDocs('jobs')).length).toBe(1);
  const jobs = (await listDocs('jobs')).map(unwrap) as {
    clipOptions: {
      instructions: string;
      maxClips: number;
      minDurationSec: number;
      maxDurationSec: number;
    };
  }[];
  expect(jobs[0]?.clipOptions).toEqual({
    instructions: 'the goals and the penalty shout',
    maxClips: 3,
    minDurationSec: 20,
    maxDurationSec: 45,
  });

  // The words were about that video; the numbers stay for the next one.
  await expect(page.getByRole('textbox', { name: 'What to clip' })).toHaveValue('');
  await expect(page.getByRole('spinbutton', { name: 'How many, at most' })).toHaveValue('3');
});

test('an upside-down range cannot be submitted', async ({ page }) => {
  await signIn(page);
  await page.goto('/jobs');
  await page.getByPlaceholder(/YouTube URL/).fill(URL);
  await page.getByRole('button', { name: 'What to look for' }).click();
  await page.getByRole('combobox', { name: 'How long' }).selectOption('custom');
  await page.getByRole('spinbutton', { name: 'Shortest, s' }).fill('60');
  await page.getByRole('spinbutton', { name: 'Longest, s' }).fill('30');
  await expect(page.getByText('The shortest length is longer than the longest.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Add', exact: true })).toBeDisabled();
});
