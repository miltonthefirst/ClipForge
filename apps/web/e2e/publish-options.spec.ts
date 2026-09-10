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
 */

const NOW = '2026-09-08T12:00:00.000Z';

function attestation(uid: string) {
  return {
    basis: 'OWN_CONTENT',
    attestedBy: uid,
    attestedAt: NOW,
    note: null,
  };
}

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
  await write('clips/clip-1', clip(uid, { review: 'APPROVED', rights: attestation(uid) }));
  await write('clips/clip-1/preview/poster', preview());
}

test('publishing without opening the panel sends no overrides at all', async ({ page }) => {
  // The common case, and the one that must stay one tap. A block full of nulls
  // would behave identically today and would pin this upload to the channel's
  // *current* defaults, so "nothing chosen" is written as nothing.
  const uid = await signIn(page);
  await seedApprovedClip(uid);

  await page.goto('/publish');
  await page.getByRole('button', { name: /Publish to YouTube/ }).click();
  await expect(page.getByText('nothing goes public by default')).toBeVisible();

  await expect.poll(async () => (await publishedOptions()) !== undefined).toBe(true);
  expect(await publishedOptions()).toBeNull();
});

test('the defaults are visible before the panel is opened', async ({ page }) => {
  // Exactly the point of the summary line: an operator must not have to open
  // anything to learn that this is about to go out unlisted.
  const uid = await signIn(page);
  await seedApprovedClip(uid);
  await write('channels/youtube-primary', channel(uid, { categoryId: '27' }));

  await page.goto('/publish');

  await expect(page.getByText('unlisted · Education · YouTube')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Publish to YouTube (unlisted)' })).toBeVisible();
});

test('overrides chosen in the panel reach the job the worker will read', async ({ page }) => {
  const uid = await signIn(page);
  await seedApprovedClip(uid);
  await write('channels/youtube-primary', channel(uid));

  await page.goto('/publish');
  await page.getByRole('button', { name: /Options/ }).click();

  await page.getByLabel('Title (optional)').fill('A different hook for this one');
  await page.getByLabel('Description (optional)').fill('and different words');
  await page.getByRole('radio', { name: /Public/ }).check();
  await page.getByLabel('Category', { exact: true }).selectOption('27');
  await page.getByLabel('Tags', { exact: true }).fill('angular, signals');

  // The button names the privacy, because 'public' is the irreversible one.
  await page.getByRole('button', { name: 'Publish to YouTube (public)' }).click();
  await expect(page.getByText('nothing goes public by default')).toBeVisible();

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

  await page.goto('/publish');
  await page.getByRole('button', { name: /Options/ }).click();

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

  await page.goto('/publish');
  await page.getByRole('button', { name: /Options/ }).click();

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

  await page.goto('/publish');
  await page.getByRole('button', { name: /Options/ }).click();

  await expect(page.getByRole('radio', { name: /Private/ })).toBeChecked();
  await expect(page.getByRole('button', { name: 'Publish to YouTube (private)' })).toBeVisible();
});
