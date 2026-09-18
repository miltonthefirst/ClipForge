import { expect, test } from '@playwright/test';

import { listDocs, plain, signIn, wipe, write } from './helpers';

/** A whole document, unwrapped: `plain` takes one REST value, and a document is a map of them. */
function unwrap(fields: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(fields).map(([key, held]) => [key, plain(held)]));
}

/**
 * Trends and the compilation that comes out of them.
 *
 * The worker is stubbed by writing what it would have written — a finished
 * RESEARCH job and its rows — and what is asserted is the PWA's half of the
 * contract: the list renders in rank order with the evidence on each row,
 * one press turns a video into a CLIP job, and gathering several turns them
 * into a COMPILE job shaped the way the rules and the worker expect.
 */

const NOW = '2026-09-19T12:00:00.000Z';

function researchJob(uid: string, id = 'job-research-1'): Record<string, never> {
  return {
    id,
    uid,
    type: 'RESEARCH',
    status: 'COMPLETED',
    sourceId: null,
    submission: null,
    researchOptions: { topics: ['premier league'], region: 'GB', lookbackHours: 48 },
    stages: [
      { name: 'RESEARCH', lane: 'CPU', status: 'DONE' },
      { name: 'CURATE', lane: 'GPU', status: 'DONE' },
    ],
    workerId: 'worker-1',
    leaseExpiresAt: null,
    attempts: 0,
    maxAttempts: 2,
    error: null,
    createdAt: NOW,
    updatedAt: NOW,
    startedAt: NOW,
    endedAt: NOW,
  } as unknown as Record<string, never>;
}

function video(id: string, title: string, score: number) {
  return {
    url: `https://www.youtube.com/watch?v=${id}`,
    externalId: id,
    title,
    channel: 'A channel',
    durationSec: 485,
    viewCount: 7_706_726,
    uploadedAt: '2026-09-18T12:00:00.000Z',
    thumbnailUrl: null,
    viewsPerHour: 321113.6,
    score,
    via: 'YOUTUBE',
  };
}

function trend(uid: string, id: string, topic: string, rank: number, score: number) {
  return {
    id,
    uid,
    jobId: 'job-research-1',
    topic,
    rank,
    score,
    signals: [
      { source: 'GOOGLE_TRENDS', strength: 0.6, detail: '200K+ searches', url: null },
      { source: 'REDDIT', strength: 0.9, detail: 'r/videos · #1 today', url: null },
    ],
    videos: [
      video(`${id}aaaaaaaaa`.slice(0, 11), `${topic} — race highlights`, 80),
      video(`${id}bbbbbbbbb`.slice(0, 11), `${topic} — qualifying`, 70),
    ],
    matchedTopics: ['premier league'],
    angle: `What a clip about ${topic} would show.`,
    relevance: 8,
    worthClipping: true,
    compilationTitle: `${topic}: the best of the week`,
    curated: true,
    status: 'NEW',
    decidedAt: null,
    decidedBy: null,
    createdAt: NOW,
  };
}

test.beforeEach(async () => {
  await wipe();
});

test('with nothing asked yet, the page says how to ask', async ({ page }) => {
  await signIn(page);
  await page.goto('/trends');

  await expect(page.getByText('Nothing has been asked yet.')).toBeVisible();
  await expect(page.getByRole('button', { name: "Find what's trending" })).toBeVisible();
});

test('a finished run lists its rows in rank order with the evidence on each', async ({ page }) => {
  const uid = await signIn(page);
  await write('jobs/job-research-1', researchJob(uid));
  await write('trends/t2', trend(uid, 't2', 'arsenal', 2, 61));
  await write('trends/t1', trend(uid, 't1', 'brewers vs orioles', 1, 78));

  await page.goto('/trends');

  const headings = page.locator('ol > li h3');
  await expect(headings).toHaveText(['brewers vs orioles', 'arsenal']);
  const first = page.locator('ol > li').first();
  await expect(first.getByText('78', { exact: true })).toBeVisible();
  await expect(first.getByText('Google Trends · 200K+ searches')).toBeVisible();
  await expect(first.getByText('relevance 8/10')).toBeVisible();
  await expect(first.getByText('What a clip about brewers vs orioles would show.')).toBeVisible();
  // The form remembers what the run was about.
  await expect(page.getByRole('textbox', { name: /What is this channel about/ })).toHaveValue(
    'premier league',
  );
});

test('pressing Clip it creates an ordinary CLIP job from the video URL', async ({ page }) => {
  const uid = await signIn(page);
  await write('jobs/job-research-1', researchJob(uid));
  await write('trends/t1', trend(uid, 't1', 'brewers vs orioles', 1, 78));

  await page.goto('/trends');
  await page.getByRole('button', { name: 'Clip it' }).first().click();
  await expect(page.getByRole('button', { name: 'Queued' })).toBeVisible();

  const jobs = (await listDocs('jobs')).map(unwrap) as { type: string; submission?: string }[];
  const clip = jobs.find((job) => job.type === 'CLIP');
  expect(clip?.submission).toBe('https://www.youtube.com/watch?v=t1aaaaaaaaa');
  const trends = (await listDocs('trends')).map(unwrap) as { status: string }[];
  expect(trends[0]?.status).toBe('PROMOTED');
});

test('Compile this gathers the trend and the Compile page makes a COMPILE job', async ({
  page,
}) => {
  const uid = await signIn(page);
  await write('jobs/job-research-1', researchJob(uid));
  await write('trends/t1', trend(uid, 't1', 'brewers vs orioles', 1, 78));

  await page.goto('/trends');
  await page.getByRole('button', { name: 'Compile this' }).click();

  await expect(page).toHaveURL(/\/compile$/);
  await expect(page.getByText('2 of 12')).toBeVisible();
  await expect(page.getByRole('textbox', { name: 'Theme' })).toHaveValue('brewers vs orioles');
  await expect(page.getByRole('textbox', { name: 'Title' })).toHaveValue(
    'brewers vs orioles: the best of the week',
  );

  await page.getByRole('button', { name: 'Make the compilation' }).click();
  await expect(page).toHaveURL(/\/jobs\/[A-Za-z0-9]+$/);
  await expect(page.getByRole('heading', { level: 1 })).toHaveText(
    'Compilation: brewers vs orioles: the best of the week',
  );

  const jobs = (await listDocs('jobs')).map(unwrap) as {
    type: string;
    compileOptions?: { theme: string; trendId: string | null; items: { submission: string }[] };
    stages: { name: string }[];
  }[];
  const compile = jobs.find((job) => job.type === 'COMPILE');
  expect(compile?.compileOptions?.theme).toBe('brewers vs orioles');
  expect(compile?.compileOptions?.trendId).toBe('t1');
  expect(compile?.compileOptions?.items.map((item) => item.submission)).toEqual([
    'https://www.youtube.com/watch?v=t1aaaaaaaaa',
    'https://www.youtube.com/watch?v=t1bbbbbbbbb',
  ]);
  expect(compile?.stages.map((stage) => stage.name)).toEqual(['GATHER', 'SELECT', 'ASSEMBLE']);
});

test('a dismissed trend leaves the list and can be brought back', async ({ page }) => {
  const uid = await signIn(page);
  await write('jobs/job-research-1', researchJob(uid));
  await write('trends/t1', trend(uid, 't1', 'brewers vs orioles', 1, 78));

  await page.goto('/trends');
  await page.getByRole('button', { name: 'Dismiss' }).click();
  await expect(page.locator('ol > li')).toHaveCount(0);
  await page.getByRole('button', { name: /Show 1 dismissed/ }).click();
  await page.getByRole('button', { name: 'Bring back' }).click();
  await expect(page.getByRole('button', { name: 'Dismiss' })).toBeVisible();
});
