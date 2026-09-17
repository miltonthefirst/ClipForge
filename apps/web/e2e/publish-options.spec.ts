import { expect, test } from '@playwright/test';

import { clip, listDocs, plain, preview, signIn, wipe, write } from './helpers';

/**
 * Per-publish overrides: title, description, privacy, category, tags and
 * channel, chosen for one upload and no other.
 *
 * These run against the real emulator with real security rules, so a test where
 * the job appears is also evidence `firestore.rules` accepted the options block
 * it carried — and a test where it does not is evidence the rules refused it.
 * That matters more here than in most places: privacy is one of these fields,
 * and 'public' is the one setting that cannot be taken back.
 *
 * What the worker then does with the block is asserted where it can be asserted
 * properly, against the resolver itself:
 * apps/worker/tests/unit/test_publish_metadata.py.
 *
 * The panel lives on the clip's own publish page now. The queue kept exactly
 * one thing — publishing with the channel's settings, in one tap — which is the
 * first test below and the reason the button is still on the row at all.
 */

const NOW = '2026-09-08T12:00:00.000Z';

function channel(uid: string, defaults: Record<string, unknown> = {}) {
  return {
    id: 'youtube-primary',
    uid,
    platform: 'YOUTUBE',
    label: 'YouTube',
    isDefault: true,
    externalChannelId: null,
    externalChannelTitle: null,
    connection: 'CONNECTED',
    connectionMessage: null,
    authorisedAt: NOW,
    checkedAt: NOW,
    defaults: {
      privacy: 'unlisted',
      categoryId: '22',
      tags: [],
      titleSuffix: null,
      descriptionTemplate: null,
      ...defaults,
    },
    quotaDay: null,
    quotaUsedUnits: null,
    uploadsRemainingToday: null,
    createdAt: NOW,
    updatedAt: NOW,
  } as Record<string, never>;
}

/** The publish job the app created, with its option block unwrapped. */
async function publishedOptions(): Promise<Record<string, unknown> | null> {
  const jobs = await listDocs('jobs');
  const job = jobs.find((held) => plain(held['type']) === 'PUBLISH');
  if (!job) return null;
  return plain(job['publishOptions']) as Record<string, unknown> | null;
}

test.beforeEach(async () => {
  await wipe();
});

async function seedApprovedClip(uid: string): Promise<void> {
  await write('clips/clip-1', clip(uid, { review: 'APPROVED' }));
  await write('clips/clip-1/preview/poster', preview());
}

test('publishing from the queue sends no overrides at all', async ({ page }) => {
  // The common case, and the one that must stay one tap. A block full of nulls
  // would behave identically today and would pin this upload to the channel's
  // *current* defaults, so "nothing chosen" is written as nothing.
  const uid = await signIn(page);
  await seedApprovedClip(uid);

  await page.goto('/publish');
  await page.getByRole('button', { name: /^Publish \(/ }).click();
  await expect(page.getByText('Queued', { exact: true })).toBeVisible();

  await expect.poll(async () => (await publishedOptions()) !== undefined).toBe(true);
  expect(await publishedOptions()).toBeNull();
});

test('the defaults are visible before anything is opened', async ({ page }) => {
  // Exactly the point of the summary line: an operator must not have to open
  // anything to learn that this is about to go out unlisted.
  const uid = await signIn(page);
  await seedApprovedClip(uid);
  await write('channels/youtube-primary', channel(uid, { categoryId: '27' }));

  // On the row, where the one-tap button is.
  await page.goto('/publish');
  await expect(page.getByText('· unlisted · YouTube')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Publish (unlisted)' })).toBeVisible();

  // And on the clip's page, with the category the row has no room for.
  await page.goto('/publish/clip-1');
  await expect(page.getByText('unlisted · Education · YouTube')).toBeVisible();

  // And the category picker opens on it, rather than on whatever option
  // happened to be first — a `[value]` binding on a <select> is applied before
  // the options exist, which is a silent way to send the wrong category.
  await page.getByRole('button', { name: 'Change' }).click();
  await expect(page.getByLabel('Category', { exact: true })).toHaveValue('27');
});

test('overrides chosen in the panel reach the job the worker will read', async ({ page }) => {
  const uid = await signIn(page);
  await seedApprovedClip(uid);
  await write('channels/youtube-primary', channel(uid));

  await page.goto('/publish/clip-1');
  await page.getByRole('button', { name: 'Change' }).click();

  await page.getByLabel('Title (optional)').fill('A different hook for this one');
  await page.getByLabel('Description (optional)').fill('and different words');
  await page.getByRole('radio', { name: /Public/ }).check();
  await page.getByLabel('Category', { exact: true }).selectOption('27');
  await page.getByLabel('Tags', { exact: true }).fill('angular, signals');

  // The button names the privacy, because 'public' is the irreversible one.
  await page.getByRole('button', { name: 'Publish to YouTube (public)' }).click();
  await expect(page.getByRole('heading', { name: 'Waiting for the worker' })).toBeVisible();

  await expect.poll(async () => (await publishedOptions()) !== null).toBe(true);

  expect(await publishedOptions()).toEqual({
    channelId: null,
    title: 'A different hook for this one',
    description: 'and different words',
    privacy: 'public',
    categoryId: '27',
    tags: ['angular', 'signals'],
  });
});

test('the tag field is prefilled from the channel and can be emptied', async ({ page }) => {
  // The one field where blank has to mean something. Prefilling it is what
  // makes "publish this one with no tags" expressible: the operator clears a
  // field that had content, which is unambiguous in a way an always-empty box
  // could never be.
  const uid = await signIn(page);
  await seedApprovedClip(uid);
  await write('channels/youtube-primary', channel(uid, { tags: ['standing', 'series'] }));

  await page.goto('/publish/clip-1');
  await page.getByRole('button', { name: 'Change' }).click();

  await expect(page.getByLabel('Tags', { exact: true })).toHaveValue('standing, series');

  await page.getByLabel('Tags', { exact: true }).fill('');
  await page.getByRole('button', { name: /Publish to YouTube/ }).click();

  await expect.poll(async () => (await publishedOptions()) !== null).toBe(true);

  const options = await publishedOptions();
  // An empty list, not null: null would fall back to the channel's tags, which
  // is the opposite of what clearing the field asked for.
  expect(options?.['tags']).toEqual([]);
  expect(options?.['privacy']).toBeNull();
});

test('a channel picker appears only when there is more than one channel', async ({ page }) => {
  const uid = await signIn(page);
  await seedApprovedClip(uid);
  await write('channels/youtube-primary', channel(uid));

  await page.goto('/publish/clip-1');
  await page.getByRole('button', { name: 'Change' }).click();

  // One destination is not a choice, and a select with one option is furniture.
  await expect(page.getByLabel('Channel', { exact: true })).toHaveCount(0);

  await write('channels/youtube-cooking', {
    ...channel(uid),
    id: 'youtube-cooking',
    label: 'Cooking',
    isDefault: false,
  });

  await expect(page.getByLabel('Channel', { exact: true })).toBeVisible();
  await page.getByLabel('Channel', { exact: true }).selectOption('youtube-cooking');
  await page.getByRole('button', { name: /Publish to YouTube/ }).click();

  await expect.poll(async () => (await publishedOptions()) !== null).toBe(true);
  expect((await publishedOptions())?.['channelId']).toBe('youtube-cooking');
});

test('the panel shows the channel default rather than a blank privacy', async ({ page }) => {
  // A form that opened with nothing selected would be asking the operator to
  // re-decide something they already configured, and would make "I did not
  // touch this" indistinguishable from "I chose private".
  const uid = await signIn(page);
  await seedApprovedClip(uid);
  await write('channels/youtube-primary', channel(uid, { privacy: 'private' }));

  await page.goto('/publish/clip-1');
  await page.getByRole('button', { name: 'Change' }).click();

  await expect(page.getByRole('radio', { name: /Private/ })).toBeChecked();
  await expect(page.getByRole('button', { name: 'Publish to YouTube (private)' })).toBeVisible();
});

test('a scheduled publish says when, and can be called off', async ({ page }) => {
  // Scheduling was always possible and was invisible the moment the page
  // reloaded: the publication that would have recorded it does not exist until
  // the worker runs. Cancelling the job is therefore the only way to withdraw
  // one, and this is the screen that can offer it.
  const uid = await signIn(page);
  await seedApprovedClip(uid);

  await page.goto('/publish/clip-1');
  await page.getByRole('button', { name: 'Change' }).click();
  await page.getByLabel('Publish at (optional)').fill('2099-01-02T09:30');
  await page.getByRole('button', { name: /^Schedule upload/ }).click();

  await expect(page.getByRole('heading', { name: /Scheduled for/ })).toBeVisible();
  await expect(page.getByText('2 Jan, 09:30')).toBeVisible();

  await page.reload();
  await expect(page.getByRole('heading', { name: /Scheduled for/ })).toBeVisible();

  await page.getByRole('button', { name: 'Call it off' }).click();

  // Back to a clip nobody has queued, which is the only honest state once the
  // job is cancelled.
  await expect(page.getByRole('button', { name: /Publish to YouTube/ })).toBeVisible();
});

test('the publish history records what actually went out', async ({ page }) => {
  // The audit trail, on the phone rather than in worker logs: where it went,
  // with what words, under what privacy, and what the attempt cost.
  const uid = await signIn(page);
  await seedApprovedClip(uid);
  await write('channels/youtube-primary', channel(uid));
  await write('clips/clip-1/publications/pub-1', {
    id: 'pub-1',
    clipId: 'clip-1',
    uid,
    platform: 'YOUTUBE',
    state: 'PUBLISHED',
    externalId: 'vid-1',
    externalUrl: 'https://www.youtube.com/watch?v=vid-1',
    channelId: 'youtube-primary',
    privacy: 'public',
    title: 'The title that actually went out',
    description: null,
    categoryId: '27',
    tags: ['angular', 'signals'],
    attempts: 1,
    quotaUnits: 1600,
    error: null,
    publishAt: null,
    createdAt: NOW,
    publishedAt: NOW,
  });

  await page.goto('/publish/clip-1');

  await expect(page.getByRole('heading', { name: /Publish history/ })).toBeVisible();
  await expect(page.getByText('YOUTUBE · YouTube · public · Education')).toBeVisible();
  await expect(page.getByText('The title that actually went out')).toBeVisible();
  await expect(page.getByText('angular, signals')).toBeVisible();
});
