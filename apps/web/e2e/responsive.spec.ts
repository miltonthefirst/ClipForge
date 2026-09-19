import { expect, test, type Page } from '@playwright/test';

import { candidate, clip, job, preview, signIn, wipe, write } from './helpers';

/**
 * Nothing scrolls sideways on a phone.
 *
 * Every page, at a phone's width, seeded with the awkward content that
 * actually turns up — a URL with no spaces in it, a file path, a long title,
 * a dozen topics — must fit the viewport. A page that does not is one a thumb
 * has to drag left and right to read, which on a phone is the whole
 * complaint. Fixed widths, two-column grids that never collapse and unbroken
 * strings are the usual causes; this pins the symptom rather than the cause,
 * so the next one is caught whatever it is.
 */

test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });

type Doc = Parameters<typeof write>[1];

const NOW = '2026-09-19T12:00:00.000Z';
const LONG_URL =
  'https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf&index=42&t=1234s';
const LONG_PATH =
  'P:/Experiments/AI work/ClipForge/workspace/clips/user-with-a-long-name/2026-09-19/clip-1-vertical-1080x1920-captioned-music-final.mp4';
const LONG_TITLE =
  'Supercalifragilisticexpialidocious_unbroken_title_that_a_creator_actually_typed_once_without_spaces';

/**
 * Whether the document can be scrolled sideways, and which elements stick out.
 *
 * Only the elements that reach past the right edge are listed, and only when
 * the document is actually wider than the viewport: a table inside its own
 * scroll container is allowed to be wide, and is not.
 */
async function overflow(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const root = document.documentElement;
    const width = root.clientWidth;
    if (root.scrollWidth <= width && document.body.scrollWidth <= width) return [];
    const offenders: string[] = [];
    for (const el of Array.from(document.querySelectorAll('body *'))) {
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 || rect.right <= width + 1) continue;
      if (getComputedStyle(el).position === 'fixed') continue;
      const classes = typeof el.className === 'string' ? el.className.split(/\s+/) : [];
      offenders.push(
        `${el.tagName.toLowerCase()}.${classes.slice(0, 5).join('.')} right=${Math.round(rect.right)} width=${Math.round(rect.width)}`,
      );
    }
    return offenders.slice(0, 15);
  });
}

const pageErrors: string[] = [];

async function fits(page: Page, where: string): Promise<void> {
  // Posters and fonts arrive a beat after the markup.
  await page.waitForTimeout(300);
  expect(pageErrors, `${where} threw in the browser`).toEqual([]);
  const wide = await overflow(page);
  expect(wide, `${where} scrolls sideways at 390px:\n  ${wide.join('\n  ')}`).toEqual([]);
}

function trendDoc(uid: string): Doc {
  return {
    id: 't1',
    uid,
    jobId: 'job-research-1',
    topic: LONG_TITLE,
    rank: 1,
    score: 78,
    signals: [
      { source: 'GOOGLE_TRENDS', strength: 0.6, detail: '200K+ searches', url: null },
      { source: 'REDDIT', strength: 0.9, detail: 'r/videos · #1 today', url: null },
      { source: 'YOUTUBE', strength: 0.7, detail: '7.7M views on the top result', url: null },
    ],
    videos: [
      {
        url: LONG_URL,
        externalId: 'dQw4w9WgXcQ',
        title: LONG_TITLE,
        channel: 'A channel with a fairly long name as well',
        durationSec: 485,
        viewCount: 7_706_726,
        uploadedAt: '2026-09-18T12:00:00.000Z',
        thumbnailUrl: null,
        viewsPerHour: 321113.6,
        score: 80,
        via: 'YOUTUBE',
      },
      {
        url: 'https://www.youtube.com/watch?v=aaaaaaaaaaa',
        externalId: 'aaaaaaaaaaa',
        title: 'Second video',
        channel: 'Channel',
        durationSec: 90,
        viewCount: 1200,
        uploadedAt: '2026-09-18T12:00:00.000Z',
        thumbnailUrl: null,
        viewsPerHour: 50,
        score: 40,
        via: 'REDDIT',
      },
    ],
    matchedTopics: ['premier league', 'formula 1', 'nba', 'nfl', 'tennis'],
    angle: 'What a clip about this would show, in one sentence that runs fairly long.',
    relevance: 8,
    worthClipping: true,
    compilationTitle: 'The best of the week',
    curated: true,
    status: 'NEW',
    decidedAt: null,
    decidedBy: null,
    createdAt: NOW,
  };
}

function researchJob(uid: string): Doc {
  return {
    ...job(uid, { id: 'job-research-1', type: 'RESEARCH', status: 'COMPLETED', submission: null }),
    researchOptions: {
      topics: [
        'premier league',
        'formula 1',
        'nba',
        'nfl',
        'tennis',
        'golf',
        'cricket',
        'rugby',
        'boxing',
        'mma',
        'olympics',
        'esports',
      ],
      region: 'GB',
      category: 'football',
      lookbackHours: 48,
    },
    stages: [
      { name: 'RESEARCH', lane: 'CPU', status: 'DONE' },
      { name: 'CURATE', lane: 'GPU', status: 'DONE' },
    ],
    scheduleId: 'sched-1',
  };
}

function schedule(uid: string): Doc {
  return {
    id: 'sched-1',
    uid,
    name: 'A schedule with a name that is quite a bit longer than most',
    enabled: true,
    cadence: 'DAILY',
    everyHours: null,
    at: '07:30',
    timezone: 'America/Argentina/ComodRivadavia',
    options: {
      topics: ['premier league', 'formula 1', 'nba', 'nfl', 'tennis', 'golf', 'cricket'],
      region: 'GB',
      category: 'football',
      lookbackHours: 24,
      curate: true,
    },
    nextDueAt: '2026-09-20T06:30:00.000Z',
    lastRunAt: NOW,
    lastJobId: 'job-research-1',
    lastOutcome: 'fired',
    createdAt: NOW,
    updatedAt: NOW,
  };
}

function publication(uid: string): Doc {
  return {
    id: 'pub-1',
    clipId: 'clip-1',
    uid,
    platform: 'YOUTUBE',
    state: 'PUBLISHED',
    externalId: 'vid-1',
    externalUrl: LONG_URL,
    privacy: 'unlisted',
    title: LONG_TITLE,
    description: null,
    tags: ['a-tag', 'another-tag', 'yet-another-tag', 'and-one-more-tag', 'a-final-tag'],
    attempts: 1,
    quotaUnits: 1600,
    error: null,
    publishAt: null,
    createdAt: NOW,
    publishedAt: NOW,
  };
}

function channel(uid: string): Doc {
  return {
    id: 'youtube-primary',
    uid,
    platform: 'YOUTUBE',
    label: 'YouTube',
    isDefault: true,
    externalChannelId: 'UCxxxxxxxxxxxxxxxxxxxxxxxx',
    externalChannelTitle: LONG_TITLE,
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
    },
    quotaDay: null,
    quotaUsedUnits: null,
    uploadsRemainingToday: null,
    createdAt: NOW,
    updatedAt: NOW,
  };
}

function source(uid: string, id: string): Doc {
  return {
    id,
    uid,
    provider: 'youtube',
    externalId: 'abc12345678',
    url: LONG_URL,
    title: LONG_TITLE,
    channel: 'CANAL+',
    durationSec: 95,
    localPath: LONG_PATH,
    sizeBytes: 5 * 1_048_576,
    pinned: false,
    useCount: 3,
    kind: 'video',
    createdAt: NOW,
  };
}

async function seedClip(uid: string): Promise<void> {
  await write('jobs/job-1', job(uid, { submission: LONG_URL, status: 'COMPLETED' }));
  await write('candidates/cand-1', candidate(uid, { hook: LONG_TITLE }));
  await write(
    'clips/clip-1',
    clip(uid, { title: LONG_TITLE, localPath: LONG_PATH, review: 'APPROVED' }),
  );
  await write('clips/clip-1/preview/poster', preview());
  await write('clips/clip-1/publications/pub-1', publication(uid));
}

test.beforeEach(async ({ page }) => {
  await wipe();
  pageErrors.length = 0;
  page.on('pageerror', (error) => pageErrors.push(error.message));
});

test('the review queue and a clip page', async ({ page }) => {
  const uid = await signIn(page);
  await seedClip(uid);
  await write('clips/clip-2', clip(uid, { id: 'clip-2', review: 'PENDING', title: LONG_TITLE }));
  await write('clips/clip-2/preview/poster', preview({ clipId: 'clip-2' }));

  await page.goto('/review');
  await expect(page.locator('img[alt*="Poster frame"]').first()).toBeVisible();
  await fits(page, 'the review queue');

  await page.goto('/review/clip-1');
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  await fits(page, 'the clip page');
});

test('the publish queue and a publication page', async ({ page }) => {
  const uid = await signIn(page);
  await seedClip(uid);
  await write('channels/youtube-primary', channel(uid));

  await page.goto('/publish');
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  await fits(page, 'the publish queue');

  await page.goto('/publish/clip-1');
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  await fits(page, 'the publication page');
});

test('the jobs list and a job page', async ({ page }) => {
  const uid = await signIn(page);
  await seedClip(uid);
  await write('jobs/job-research-1', researchJob(uid));
  await write(
    'jobs/job-2',
    job(uid, {
      id: 'job-2',
      status: 'FAILED',
      submission: LONG_PATH,
      error: {
        code: 'DOWNLOAD_FAILED',
        message: `yt-dlp could not fetch ${LONG_URL} after three attempts: HTTP 403`,
        retryable: false,
      },
    }),
  );

  // The list opens on what is still moving, so one of the jobs has to be.
  await write('jobs/job-3', job(uid, { id: 'job-3', submission: LONG_URL }));

  await page.goto('/jobs');
  await expect(page.getByText('stages').first()).toBeVisible();
  await fits(page, 'the jobs list');
  await page.getByRole('button', { name: 'What to look for' }).click();
  await fits(page, 'the jobs list with the brief open');

  await page.goto('/jobs/job-2');
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  await fits(page, 'a failed job page');

  await page.goto('/jobs/job-research-1');
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  await fits(page, 'a research job page');
});

test('trends, with a run, a schedule and the editor open', async ({ page }) => {
  const uid = await signIn(page);
  await write('jobs/job-research-1', researchJob(uid));
  await write('trends/t1', trendDoc(uid));
  await write('schedules/sched-1', schedule(uid));

  await page.goto('/trends');
  await expect(page.locator('ol > li h3').first()).toBeVisible();
  await fits(page, 'the trends page');

  await page.getByRole('button', { name: 'Do this automatically…' }).click();
  await fits(page, 'the trends page with the schedule panel open');

  await page.getByRole('button', { name: 'Edit' }).click();
  await expect(page.getByRole('button', { name: 'Save changes' })).toBeVisible();
  await fits(page, 'the trends page with a schedule being edited');

  await write('jobs/job-t', job(uid, { id: 'job-t', trendId: 't1', submission: LONG_URL }));
  await page.goto('/trends/t1');
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  await expect(page.getByText('1 of 4 stages')).toBeVisible();
  await fits(page, 'a trend’s page');
});

test('compile, with a full basket', async ({ page }) => {
  await signIn(page);
  await page.evaluate(
    ([url, title, path]) => {
      localStorage.setItem(
        'clipforge.compile-basket',
        JSON.stringify({
          items: [
            { url, title, channel: 'A channel with a fairly long name as well' },
            { url: 'https://www.youtube.com/watch?v=aaaaaaaaaaa', title: null, channel: null },
            { url: path, title: 'A file on the worker', channel: null },
          ],
          theme: title,
          title: '',
          trendId: null,
        }),
      );
    },
    [LONG_URL, LONG_TITLE, LONG_PATH] as const,
  );

  await page.goto('/compile');
  await expect(page.getByText('3 of 12')).toBeVisible();
  await fits(page, 'the compile page');
});

test('insights, sources, settings and the admin page', async ({ page }) => {
  const uid = await signIn(page);
  await write('sources/src-1', source(uid, 'src-1'));
  await write('sources/src-2', source(uid, 'src-2'));
  await write('channels/youtube-primary', channel(uid));

  for (const route of [
    '/insights',
    '/sources',
    '/settings',
    '/settings/worker',
    '/settings/storage',
    '/settings/youtube',
    '/admin/users',
  ]) {
    await page.goto(route);
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    await fits(page, route);
  }
});
