import type { Trend } from '@clipforge/contracts';
import { describe, expect, it } from 'vitest';

import { cacheKey } from '../firestore/spec';
import {
  IN_LIMIT,
  RESEARCH_RUNS,
  TRENDS_PER_RUN,
  byRank,
  clipsForJobsSpec,
  jobsForTrendSpec,
  jobsForTrendsSpec,
  newCompileJob,
  newResearchJob,
  researchRunsSpec,
  trendsSpec,
} from './research';

/**
 * The queries the Trends and Compile pages run, and the two documents they
 * are allowed to write.
 *
 * The documents matter most. A job's stage list is authoritative for its whole
 * life, so a shape here that disagrees with the worker's pipeline produces a
 * job that runs until it reaches a stage nothing can run — after everything
 * before it has been paid for.
 */

function trend(over: Partial<Trend> = {}): Trend {
  return {
    id: 't1',
    uid: 'u1',
    jobId: 'job-1',
    topic: 'a topic',
    rank: 1,
    score: 50,
    signals: [],
    videos: [],
    status: 'NEW',
    createdAt: '2026-09-19T12:00:00.000Z',
    ...over,
  } as Trend;
}

describe('researchRunsSpec', () => {
  it('asks for research jobs only, bounded, with no ordering to index', () => {
    expect(researchRunsSpec()).toEqual({
      collection: 'jobs',
      where: [['type', '==', 'RESEARCH']],
      limit: RESEARCH_RUNS,
    });
  });

  it('is one listener however many pages ask', () => {
    expect(cacheKey(researchRunsSpec())).toBe(cacheKey(researchRunsSpec()));
  });
});

describe('trendsSpec', () => {
  it('reads one run and no more rows than a run can write', () => {
    expect(trendsSpec('job-1')).toEqual({
      collection: 'trends',
      where: [['jobId', '==', 'job-1']],
      limit: TRENDS_PER_RUN,
    });
  });
});

describe('byRank', () => {
  it('puts rank 1 first whatever order the rows arrived in', () => {
    const rows = byRank([
      trend({ id: 'c', rank: 3 }),
      trend({ id: 'a', rank: 1 }),
      trend({ id: 'b', rank: 2 }),
    ]);
    expect(rows.map((row) => row.id)).toEqual(['a', 'b', 'c']);
  });

  it('breaks a tie on the topic, so the order is stable', () => {
    const rows = byRank([trend({ id: 'z', topic: 'zebra' }), trend({ id: 'a', topic: 'apple' })]);
    expect(rows.map((row) => row.id)).toEqual(['a', 'z']);
  });
});

describe('newResearchJob', () => {
  const job = newResearchJob('job-1', 'u1', { topics: ['f1'] }, '2026-09-19T12:00:00.000Z');

  it('declares the two stages the worker registers, in order and on the right lanes', () => {
    expect(job.stages).toEqual([
      { name: 'RESEARCH', lane: 'CPU', status: 'PENDING' },
      { name: 'CURATE', lane: 'GPU', status: 'PENDING' },
    ]);
  });

  it('is exactly what the rules let a client create', () => {
    expect(job.status).toBe('QUEUED');
    expect(job.workerId).toBeNull();
    expect(job.leaseExpiresAt).toBeNull();
    expect(job.attempts).toBe(0);
    expect(job.researchOptions).toEqual({ topics: ['f1'] });
  });

  it('gives up after two attempts, because a third asks the same feeds again', () => {
    expect(job.maxAttempts).toBe(2);
  });
});

describe('newCompileJob', () => {
  const job = newCompileJob(
    'job-2',
    'u1',
    { theme: 'goals', items: [{ submission: 'a' }, { submission: 'b' }] },
    '2026-09-19T12:00:00.000Z',
  );

  it('declares gather, select and assemble, with only the choosing on the GPU', () => {
    expect(job.stages).toEqual([
      { name: 'GATHER', lane: 'CPU', status: 'PENDING' },
      { name: 'SELECT', lane: 'GPU', status: 'PENDING' },
      { name: 'ASSEMBLE', lane: 'CPU', status: 'PENDING' },
    ]);
  });

  it('carries the options and no submission of its own', () => {
    expect(job.type).toBe('COMPILE');
    expect(job.submission).toBeNull();
    expect(job.compileOptions?.theme).toBe('goals');
  });

  it('repeats the trend on the job itself, so one filter finds it', () => {
    expect(job.trendId).toBeNull();
    const fromTrend = newCompileJob(
      'job-3',
      'u1',
      { theme: 'goals', items: [{ submission: 'a' }, { submission: 'b' }], trendId: 't1' },
      '2026-09-19T12:00:00.000Z',
    );
    expect(fromTrend.trendId).toBe('t1');
  });
});

describe('what became of a trend', () => {
  it('finds one trend’s jobs with one equality filter and no ordering', () => {
    const spec = jobsForTrendSpec('t1');
    expect(spec.where).toEqual([['trendId', '==', 't1']]);
    expect(spec.orderBy).toBeUndefined();
    expect(spec.limit).toBe(IN_LIMIT);
  });

  it('finds a whole run’s jobs in one `in` filter, capped where Firestore caps it', () => {
    const ids = Array.from({ length: 35 }, (_, i) => `t${i}`);
    const spec = jobsForTrendsSpec(ids);
    expect(spec.where?.[0]?.[1]).toBe('in');
    expect((spec.where?.[0]?.[2] as string[]).length).toBe(IN_LIMIT);
    expect(IN_LIMIT).toBe(TRENDS_PER_RUN);
  });

  it('finds the clips those jobs made by jobId, the same way', () => {
    const spec = clipsForJobsSpec(['j1', 'j2']);
    expect(spec.collection).toBe('clips');
    expect(spec.where).toEqual([['jobId', 'in', ['j1', 'j2']]]);
    expect(spec.orderBy).toBeUndefined();
  });
});
