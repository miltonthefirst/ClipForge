import type { Job } from '@clipforge/contracts';
import { describe, expect, it } from 'vitest';

import { cacheKey } from '../firestore/spec';
import {
  JOB_PAGE,
  jobCountSpec,
  jobEventsSpec,
  jobsForClipSpec,
  jobsSpec,
  newJob,
  newestFirst,
  publishJobsSpec,
} from './jobs';

/**
 * The queries the Jobs pages run, and the one document they are allowed to
 * write.
 *
 * Asserted here rather than eyeballed because every failure in this file is
 * silent and paid for in reads: a bound that goes missing turns one page view
 * into a listen over the whole collection, an ordering added to a count makes
 * it demand an index it never needed, and two tabs whose specs collide hand the
 * second one the first one's rows with nothing on screen to say so.
 */

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: 'job-1',
    uid: 'user-1',
    type: 'CLIP',
    status: 'COMPLETED',
    stages: [{ name: 'RENDER', lane: 'CPU', status: 'DONE' }],
    attempts: 0,
    maxAttempts: 3,
    createdAt: '2026-09-14T09:00:00.000Z',
    updatedAt: '2026-09-14T09:00:00.000Z',
    ...overrides,
  };
}

describe('jobsSpec', () => {
  it('asks for the newest page and no more, filtered or not', () => {
    expect(jobsSpec(null).limit).toBe(JOB_PAGE);
    expect(jobsSpec(['COMPLETED']).limit).toBe(JOB_PAGE);
    expect(jobsSpec(null).orderBy).toEqual([['createdAt', 'desc']]);
  });

  it('asks for every status at once rather than one query per status', () => {
    expect(jobsSpec(['RUNNING', 'QUEUED']).where).toEqual([
      ['status', 'in', ['QUEUED', 'RUNNING']],
    ]);
  });

  it('carries no filter for the tab that wants everything, so it needs no index', () => {
    expect(jobsSpec(null).where).toBeUndefined();
  });

  it('gives two tabs two different cache keys', () => {
    expect(cacheKey(jobsSpec(['RUNNING', 'QUEUED']))).not.toBe(cacheKey(jobsSpec(['COMPLETED'])));
    expect(cacheKey(jobsSpec(null))).not.toBe(cacheKey(jobsSpec(['COMPLETED'])));
  });

  it('gives one tab one cache key however its statuses were ordered', () => {
    expect(cacheKey(jobsSpec(['RUNNING', 'QUEUED']))).toBe(
      cacheKey(jobsSpec(['QUEUED', 'RUNNING'])),
    );
  });
});

describe('jobCountSpec', () => {
  it('counts the whole slice, not just the page the list shows', () => {
    expect(jobCountSpec(['COMPLETED']).limit).toBeUndefined();
  });

  it('asks for no ordering, so the counts are right before the index is built', () => {
    expect(jobCountSpec(['COMPLETED']).orderBy).toBeUndefined();
  });

  it('counts the same slice the list asks for', () => {
    expect(jobCountSpec(['FAILED', 'CANCELLED']).where).toEqual(
      jobsSpec(['FAILED', 'CANCELLED']).where,
    );
  });
});

describe('jobEventsSpec', () => {
  it('orders by seq within an instant, so a reap reads the same way every load', () => {
    expect(jobEventsSpec('job-1').orderBy).toEqual([
      ['at', 'asc'],
      ['seq', 'asc'],
    ]);
  });

  it('reads the log of the job it was asked about', () => {
    expect(jobEventsSpec('job-1').collection).toBe('jobs/job-1/events');
  });

  it('is bounded', () => {
    expect(jobEventsSpec('job-1').limit).toBeGreaterThan(0);
  });
});

describe('publishJobsSpec', () => {
  it('is one equality filter and a bound, so it needs no composite index', () => {
    const spec = publishJobsSpec();
    expect(spec.where).toEqual([['type', '==', 'PUBLISH']]);
    expect(spec.orderBy).toBeUndefined();
    expect(spec.limit).toBeGreaterThan(0);
  });
});

describe('jobsForClipSpec', () => {
  it('is bounded and unordered, for the same reason the publish query is', () => {
    const spec = jobsForClipSpec('clip-1');
    expect(spec.where).toEqual([['clipId', '==', 'clip-1']]);
    expect(spec.orderBy).toBeUndefined();
    expect(spec.limit).toBeGreaterThan(0);
  });
});

describe('newestFirst', () => {
  it('puts the most recent job at the top', () => {
    const older = job({ id: 'a', createdAt: '2026-09-14T09:00:00.000Z' });
    const newer = job({ id: 'b', createdAt: '2026-09-14T11:00:00.000Z' });

    expect(newestFirst([older, newer]).map((j) => j.id)).toEqual(['b', 'a']);
  });

  it('leaves the array it was given alone', () => {
    const jobs = [
      job({ id: 'a', createdAt: '2026-09-14T09:00:00.000Z' }),
      job({ id: 'b', createdAt: '2026-09-14T11:00:00.000Z' }),
    ];
    newestFirst(jobs);

    expect(jobs.map((j) => j.id)).toEqual(['a', 'b']);
  });
});

describe('newJob with a brief', () => {
  it('writes the brief on the job, and null when there is none', () => {
    const now = '2026-09-19T12:00:00.000Z';
    expect(newJob('j1', 'u1', 'https://youtu.be/x', now).clipOptions).toBeNull();
    expect(
      newJob('j2', 'u1', 'https://youtu.be/x', now, {
        instructions: 'the goals',
        maxClips: 3,
        minDurationSec: 20,
        maxDurationSec: 45,
      }).clipOptions,
    ).toEqual({ instructions: 'the goals', maxClips: 3, minDurationSec: 20, maxDurationSec: 45 });
  });
});
