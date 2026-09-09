import { expect, test } from '@playwright/test';

import { clip, preview, readDoc, signIn, wipe, write } from './helpers';

/**
 * Phase 8 in the browser: the rights gate, the attestation it demands, and the
 * publish request that follows.
 *
 * These run against the real emulator with real security rules, so a test that
 * publishes successfully is also evidence that `firestore.rules` allowed it —
 * and a test that cannot is evidence the rules refused, not that a button was
 * greyed out. That distinction is the point: the UI copy of the gate is
 * advisory, and these assert the enforcing copy underneath it.
 */

const NOW = '2026-09-08T12:00:00.000Z';

function attestation(overrides: Record<string, string | null> = {}) {
  return {
    basis: 'OWN_CONTENT',
    attestedBy: 'placeholder',
    attestedAt: NOW,
    note: null,
    ...overrides,
  };
}

test.beforeEach(async () => {
  await wipe();
});

test('an approved clip with no attestation cannot be published yet', async ({ page }) => {
  const uid = await signIn(page);
  await write('clips/clip-1', clip(uid, { review: 'APPROVED' }));
  await write('clips/clip-1/preview/poster', preview());

  await page.goto('/publish');

  await expect(page.getByText('Record why you may publish this clip')).toBeVisible();
  // No publish affordance at all, rather than one that fails when pressed.
  await expect(page.getByRole('button', { name: /Publish to YouTube/ })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Record rights basis' })).toBeDisabled();
});

test('the publish queue explains itself when nothing is approved', async ({ page }) => {
  await signIn(page);
  await page.goto('/publish');

  await expect(page.getByText('Nothing approved yet')).toBeVisible();
  await expect(page.getByText('Approve a clip on the Review page')).toBeVisible();
});

test('choosing a basis records an attestation stamped with the caller', async ({ page }) => {
  const uid = await signIn(page);
  await write('clips/clip-1', clip(uid, { review: 'APPROVED' }));
  await write('clips/clip-1/preview/poster', preview());

  await page.goto('/publish');
  await page.getByRole('radio', { name: /I made this/ }).check();
  await page.getByRole('button', { name: 'Record rights basis' }).click();

  await expect(page.getByRole('button', { name: /Publish to YouTube/ })).toBeVisible();

  // Polled: the UI reflects the write locally before the server has it, so a
  // single read here races the round trip.
  await expect
    .poll(async () => (await readDoc('clips/clip-1'))?.['rights'] !== undefined)
    .toBe(true);

  const stored = await readDoc('clips/clip-1');
  const rights = stored?.['rights'] as { mapValue: { fields: Record<string, never> } };
  const fields = rights.mapValue.fields as Record<string, { stringValue?: string }>;
  expect(fields['basis'].stringValue).toBe('OWN_CONTENT');
  // Stamped by the app from the session, never accepted from a form: an
  // attestation whose author could be typed in answers nothing.
  expect(fields['attestedBy'].stringValue).toBe(uid);
  expect(fields['attestedAt'].stringValue).toBeTruthy();
});

test('fair use cannot be recorded without reasoning', async ({ page }) => {
  const uid = await signIn(page);
  await write('clips/clip-1', clip(uid, { review: 'APPROVED' }));
  await write('clips/clip-1/preview/poster', preview());

  await page.goto('/publish');
  await page.getByRole('radio', { name: /Fair use/ }).check();

  await expect(page.getByText('required for fair use')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Record rights basis' })).toBeDisabled();

  await page.getByRole('textbox').fill('30 seconds of a 90 minute lecture, with commentary.');
  await expect(page.getByRole('button', { name: 'Record rights basis' })).toBeEnabled();
});

test('publishing an attested clip creates a PUBLISH job the rules accept', async ({ page }) => {
  const uid = await signIn(page);
  await write(
    'clips/clip-1',
    clip(uid, { review: 'APPROVED', rights: attestation({ attestedBy: uid }) }),
  );
  await write('clips/clip-1/preview/poster', preview());

  await page.goto('/publish');
  await page.getByRole('button', { name: /Publish to YouTube/ }).click();

  // The confirmation appears only after the write succeeded, so seeing it means
  // firestore.rules ran the gate and allowed it.
  await expect(page.getByText('nothing goes public by default')).toBeVisible();
});

test('an unlisted default is stated before the button is pressed, not after', async ({
  page,
}) => {
  const uid = await signIn(page);
  await write(
    'clips/clip-1',
    clip(uid, { review: 'APPROVED', rights: attestation({ attestedBy: uid }) }),
  );
  await write('clips/clip-1/preview/poster', preview());

  await page.goto('/publish');
  await expect(page.getByRole('button', { name: 'Publish to YouTube (unlisted)' })).toBeVisible();
});

test('a published clip shows its audit trail instead of a publish button', async ({ page }) => {
  // Exit criterion 5, from the phone: who authorised it, on what basis, and
  // what went out.
  const uid = await signIn(page);
  await write(
    'clips/clip-1',
    clip(uid, { review: 'APPROVED', rights: attestation({ attestedBy: uid }) }),
  );
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
    rights: attestation({ attestedBy: uid, note: 'Filmed on my own camera.' }),
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
  await expect(page.getByText('Filmed on my own camera.')).toBeVisible();
  await expect(page.getByRole('button', { name: /Publish to YouTube/ })).toHaveCount(0);
});

test('a failed attempt is shown rather than silently retried', async ({ page }) => {
  const uid = await signIn(page);
  await write(
    'clips/clip-1',
    clip(uid, { review: 'APPROVED', rights: attestation({ attestedBy: uid }) }),
  );
  await write('clips/clip-1/preview/poster', preview());
  await write('clips/clip-1/publications/pub-1', {
    id: 'pub-1',
    clipId: 'clip-1',
    uid,
    platform: 'YOUTUBE',
    state: 'FAILED',
    externalId: null,
    externalUrl: null,
    rights: attestation({ attestedBy: uid }),
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

test('another user’s approved clip is not publishable from here', async ({ page }) => {
  await signIn(page);
  await write(
    'clips/clip-1',
    clip('someone-else', { review: 'APPROVED', rights: attestation() }),
  );

  await page.goto('/publish');
  await expect(page.getByText('Nothing approved yet')).toBeVisible();
});
