import { expect, test } from '@playwright/test';

import { clip, job, listDocs, plain, preview, signIn, wipe, write } from './helpers';

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

  const jobs = (await listDocs('jobs')).map(unwrap) as {
    type: string;
    submission?: string;
    clipOptions?: { instructions: string } | null;
    trendId?: string | null;
  }[];
  const clip = jobs.find((job) => job.type === 'CLIP');
  expect(clip?.submission).toBe('https://www.youtube.com/watch?v=t1aaaaaaaaa');
  // The model's angle rides along as the brief.
  expect(clip?.clipOptions?.instructions).toBe('What a clip about brewers vs orioles would show.');
  // And the job remembers where it came from.
  expect(clip?.trendId).toBe('t1');
  // The promotion is written after the job, and the button does not wait for it.
  await expect
    .poll(async () => ((await listDocs('trends')).map(unwrap)[0] as { status: string })?.status)
    .toBe('PROMOTED');
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

test('where and what kind are typed for, optional, and ride on the job', async ({ page }) => {
  await signIn(page);
  await page.goto('/trends');

  // Country: type part of a name, pick from what is left.
  const where = page.getByRole('combobox', { name: 'Where' });
  await where.fill('united k');
  await page.getByRole('option', { name: 'United Kingdom' }).click();
  await expect(where).toHaveValue('United Kingdom');

  // Category: found by an alias, chosen with the keyboard.
  const category = page.getByRole('combobox', { name: 'Category' });
  await category.fill('soccer');
  await expect(page.getByRole('option', { name: /Football \(soccer\)/ })).toBeVisible();
  await category.press('Enter');
  await expect(category).toHaveValue('Football (soccer)');

  await page.getByRole('button', { name: "Find what's trending" }).click();
  await expect(page).toHaveURL(/run=/);

  await expect.poll(async () => (await listDocs('jobs')).length).toBe(1);
  const asked = (await listDocs('jobs')).map(unwrap) as {
    researchOptions: { region: string | null; category: string | null; topics: string[] };
  }[];
  expect(asked[0]?.researchOptions.region).toBe('GB');
  expect(asked[0]?.researchOptions.category).toBe('football');
  expect(asked[0]?.researchOptions.topics).toEqual([]);

  // The form keeps what was asked; the cross puts a field back to "none".
  await expect(where).toHaveValue('United Kingdom');
  await page.getByRole('button', { name: 'Clear Where' }).click();
  await expect(where).toHaveValue('');
  await expect(where).toHaveAttribute('placeholder', 'Worker’s default');
});

test('a trend has a page that says what became of it', async ({ page }) => {
  const uid = await signIn(page);
  await write('jobs/job-research-1', researchJob(uid));
  await write('trends/t1', trend(uid, 't1', 'brewers vs orioles', 1, 78));
  // Three jobs made from it: one that made a clip, one that made nothing,
  // and one still going.
  await write(
    'jobs/job-a',
    job(uid, {
      id: 'job-a',
      status: 'COMPLETED',
      trendId: 't1',
      submission: 'https://www.youtube.com/watch?v=t1aaaaaaaaa',
      stages: [
        { name: 'DOWNLOAD', lane: 'CPU', status: 'DONE' },
        { name: 'TRANSCRIBE', lane: 'GPU', status: 'DONE' },
        { name: 'ANALYZE', lane: 'GPU', status: 'DONE' },
        { name: 'RENDER', lane: 'CPU', status: 'DONE', checkpoint: { clipIds: ['clip-1'] } },
      ],
    }),
  );
  await write('clips/clip-1', clip(uid, { id: 'clip-1', jobId: 'job-a', title: 'The late homer' }));
  await write('clips/clip-1/preview/poster', preview());
  await write(
    'jobs/job-b',
    job(uid, {
      id: 'job-b',
      status: 'COMPLETED',
      trendId: 't1',
      submission: 'https://www.youtube.com/watch?v=t1bbbbbbbbb',
      stages: [
        { name: 'DOWNLOAD', lane: 'CPU', status: 'DONE' },
        { name: 'TRANSCRIBE', lane: 'GPU', status: 'DONE' },
        { name: 'ANALYZE', lane: 'GPU', status: 'DONE' },
        { name: 'RENDER', lane: 'CPU', status: 'DONE', checkpoint: { clipIds: [], rendered: 0 } },
      ],
    }),
  );
  await write(
    'jobs/job-c',
    job(uid, {
      id: 'job-c',
      trendId: 't1',
      submission: 'https://www.youtube.com/watch?v=t1ccccccccc',
    }),
  );

  // The card says what became of it, and leads to the page.
  await page.goto('/trends');
  const card = page.locator('ol > li').first();
  await expect(card.getByText('1 running · 1 to review · nothing to cut →')).toBeVisible();
  await card.getByRole('link', { name: 'brewers vs orioles', exact: true }).click();
  await expect(page).toHaveURL(/\/trends\/t1$/);
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('brewers vs orioles');

  // The page lists the three, each with what it did.
  const made = page.locator('section', { hasText: 'What became of it' });
  await expect(made.getByRole('link', { name: 'Job page →' })).toHaveCount(3);
  await expect(made.getByRole('link', { name: 'The late homer' })).toHaveAttribute(
    'href',
    '/review/clip-1',
  );
  await expect(made.getByText('to review', { exact: true })).toBeVisible();
  await expect(made.getByText('Finished, but nothing was worth cutting')).toBeVisible();
  await expect(made.getByText('1 of 4 stages · TRANSCRIBE')).toBeVisible();

  // Clip it from here writes the same job the list page would, trend and all.
  await page.getByRole('button', { name: 'Clip it' }).nth(1).click();
  await expect(page.getByRole('button', { name: 'Queued' })).toBeVisible();
  await expect
    .poll(
      async () =>
        (await listDocs('jobs'))
          .map(unwrap)
          .filter((doc) => (doc as { status: string }).status === 'QUEUED').length,
    )
    .toBe(1);
  const queued = (await listDocs('jobs'))
    .map(unwrap)
    .find((doc) => (doc as { status: string }).status === 'QUEUED') as {
    trendId: string | null;
    submission: string;
  };
  expect(queued.trendId).toBe('t1');
  expect(queued.submission).toBe('https://www.youtube.com/watch?v=t1bbbbbbbbb');
  // And the page shows the new job at once, from the live listener.
  await expect(made.getByRole('link', { name: 'Job page →' })).toHaveCount(4);

  // A finished job, and what it made, can be forgotten from here. The row
  // for the job that made the clip has one Delete; the running and queued
  // ones have none, because deleting a job a worker holds does not stop it.
  await expect(made.getByRole('button', { name: 'Delete' })).toHaveCount(2);
  page.once('dialog', (dialog) => void dialog.accept());
  await made
    .locator('li', { hasText: 'The late homer' })
    .getByRole('button', { name: 'Delete' })
    .click();
  await expect(made.getByRole('link', { name: 'Job page →' })).toHaveCount(3);
  await expect.poll(async () => (await listDocs('clips')).length).toBe(0);
  const remaining = (await listDocs('jobs')).map(unwrap) as { id: string }[];
  expect(remaining.map((job) => job.id).sort()).not.toContain('job-a');
});

test('a compilation is deleted, with its job, from where the clip page describes it', async ({
  page,
}) => {
  const uid = await signIn(page);
  await write(
    'jobs/job-compile-1',
    job(uid, { id: 'job-compile-1', type: 'COMPILE', status: 'COMPLETED', submission: null }),
  );
  await write(
    'clips/clip-1',
    clip(uid, {
      id: 'clip-1',
      jobId: 'job-compile-1',
      sourceId: null,
      candidateId: 'compile-job-compile-1',
      title: 'The best of the week',
      compile: {
        theme: 'brewers vs orioles',
        segments: [
          {
            sourceId: 'src-1',
            submission: 'https://www.youtube.com/watch?v=aaaaaaaaaaa',
            startSec: 10,
            endSec: 20,
            durationSec: 10,
          },
          {
            sourceId: 'src-2',
            submission: 'https://www.youtube.com/watch?v=bbbbbbbbbbb',
            startSec: 5,
            endSec: 15,
            durationSec: 10,
          },
        ],
        titleCard: true,
        transition: 'FADE',
      },
    }),
  );
  await write('clips/clip-1/preview/poster', preview());

  await page.goto('/review/clip-1');
  await expect(page.getByRole('heading', { name: 'Compiled from 2 videos' })).toBeVisible();
  page.once('dialog', (dialog) => void dialog.accept());
  await page.getByRole('button', { name: 'Delete this compilation' }).click();
  await expect(page).toHaveURL(/\/review$/);
  await expect.poll(async () => (await listDocs('clips')).length).toBe(0);
  await expect.poll(async () => (await listDocs('jobs')).length).toBe(0);
});

test('a trend with nothing to clip becomes a drawn video from its page', async ({ page }) => {
  const uid = await signIn(page);
  await write('jobs/job-research-1', researchJob(uid));
  await write('trends/t1', {
    ...trend(uid, 't1', 'brewers vs orioles', 1, 78),
    worthClipping: false,
    angle: 'The evidence only confirms the topic exists.',
  });

  // The card's button lands on the page with the panel open.
  await page.goto('/trends');
  await page.getByRole('link', { name: 'Make a video' }).click();
  await expect(page).toHaveURL(/\/trends\/t1\?make=1$/);
  await expect(page.getByRole('heading', { name: 'Make a video about this' })).toBeVisible();

  await page
    .getByRole('textbox', { name: 'What it should say' })
    .fill('Explain what happened, plainly.');
  await page.getByRole('combobox', { name: 'How long' }).selectOption({ label: 'One minute' });
  await page.getByRole('combobox', { name: 'Voice' }).selectOption({ label: 'George · UK, male' });
  await page.getByRole('button', { name: 'Draw the video' }).click();
  await expect(page).toHaveURL(/\/jobs\/[A-Za-z0-9]+$/);
  await expect(page.getByRole('heading', { level: 1 })).toHaveText(
    'Drawn video: brewers vs orioles',
  );

  const jobs = (await listDocs('jobs')).map(unwrap) as {
    type: string;
    trendId: string | null;
    stages: { name: string; lane: string }[];
    composeOptions?: {
      topic: string;
      angle: string | null;
      script: string | null;
      context: string | null;
      targetDurationSec: number;
      voice: string | null;
      style: string;
      trendId: string | null;
    };
  }[];
  const compose = jobs.find((job) => job.type === 'COMPOSE');
  expect(compose?.trendId).toBe('t1');
  expect(compose?.stages.map((stage) => stage.name)).toEqual([
    'SCRIPT',
    'NARRATE',
    'ALIGN',
    'DRAW',
    'ASSEMBLE',
  ]);
  expect(compose?.composeOptions?.topic).toBe('brewers vs orioles');
  expect(compose?.composeOptions?.angle).toBe('Explain what happened, plainly.');
  expect(compose?.composeOptions?.script).toBeNull();
  expect(compose?.composeOptions?.targetDurationSec).toBe(60);
  expect(compose?.composeOptions?.voice).toBe('bm_george');
  expect(compose?.composeOptions?.style).toBe('STICK');
  // The evidence went along as facts, the curator's doubt among them.
  expect(compose?.composeOptions?.context).toContain('Google Trends · 200K+ searches');
  expect(compose?.composeOptions?.context).toContain("The curator's note");
  // Asking for a video is acting on the trend.
  await expect
    .poll(async () => ((await listDocs('trends')).map(unwrap)[0] as { status: string })?.status)
    .toBe('PROMOTED');
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

test('saving the form as a schedule writes what the worker fires, and the list shows it', async ({
  page,
}) => {
  await signIn(page);
  await page.goto('/trends');

  await page.getByRole('textbox', { name: /What is this channel about/ }).fill('premier league');
  await page.getByRole('button', { name: 'Do this automatically…' }).click();
  await page.getByRole('textbox', { name: 'Name' }).fill('Mornings');
  await page.getByRole('button', { name: 'Save schedule' }).click();
  // The panel closes only once the write is acknowledged; the row below can
  // appear earlier, from the listener's local echo of the pending write.
  await expect(page.getByRole('button', { name: 'Do this automatically…' })).toBeVisible();

  const row = page.locator('section', { hasText: 'Automatic' }).locator('li').first();
  await expect(row).toContainText('Mornings');
  await expect(row).toContainText('Daily at 07:30');
  await expect(row).toContainText('premier league');

  const schedules = (await listDocs('schedules')).map(unwrap) as {
    name: string;
    enabled: boolean;
    cadence: string;
    at: string | null;
    everyHours: number | null;
    timezone: string | null;
    options: { topics: string[]; region: string | null; category: string | null };
    lastJobId: string | null;
  }[];
  expect(schedules).toHaveLength(1);
  expect(schedules[0]?.name).toBe('Mornings');
  expect(schedules[0]?.enabled).toBe(true);
  expect(schedules[0]?.cadence).toBe('DAILY');
  expect(schedules[0]?.at).toBe('07:30');
  expect(schedules[0]?.everyHours).toBeNull();
  expect(typeof schedules[0]?.timezone).toBe('string');
  expect(schedules[0]?.options.topics).toEqual(['premier league']);
  // Left alone, both are null: the worker's default region, nothing steered.
  expect(schedules[0]?.options.region).toBeNull();
  expect(schedules[0]?.options.category).toBeNull();
  expect(schedules[0]?.lastJobId).toBeNull();

  await row.getByRole('button', { name: 'Switch off' }).click();
  await expect(row).toContainText('off');
});

test('a schedule can be changed in place, and only the request changes', async ({ page }) => {
  const uid = await signIn(page);
  await write('schedules/sched-1', {
    id: 'sched-1',
    uid,
    name: 'Mornings',
    enabled: true,
    cadence: 'DAILY',
    everyHours: null,
    at: '07:30',
    timezone: 'Europe/London',
    options: { topics: ['premier league'], region: 'GB', lookbackHours: 24, curate: true },
    nextDueAt: '2026-09-20T06:30:00.000Z',
    lastRunAt: NOW,
    lastJobId: 'job-research-1',
    lastOutcome: 'fired',
    createdAt: NOW,
    updatedAt: NOW,
  });

  await page.goto('/trends');
  const row = page.locator('section', { hasText: 'Automatic' }).locator('li').first();
  await row.getByRole('button', { name: 'Edit' }).click();
  await row.getByRole('textbox', { name: 'Name' }).fill('Evenings');
  await row.getByRole('textbox', { name: 'What to look for' }).fill('nba, formula 1');
  await row.getByRole('button', { name: 'Save changes' }).click();

  await expect(row).toContainText('Evenings');
  await expect(row).toContainText('nba, formula 1');
  await expect(row.getByRole('button', { name: 'Edit' })).toBeVisible();

  const schedules = (await listDocs('schedules')).map(unwrap) as {
    name: string;
    options: { topics: string[] };
    lastJobId: string | null;
    lastOutcome: string | null;
    at: string | null;
  }[];
  expect(schedules[0]?.name).toBe('Evenings');
  expect(schedules[0]?.options.topics).toEqual(['nba', 'formula 1']);
  expect(schedules[0]?.at).toBe('07:30');
  // The worker's record survives the edit.
  expect(schedules[0]?.lastJobId).toBe('job-research-1');
  expect(schedules[0]?.lastOutcome).toBe('fired');
});
