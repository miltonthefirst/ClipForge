import type { Clip, Job } from '@clipforge/contracts';
import { describe, expect, it } from 'vitest';

import { activityFor, describeActivity, kindOf } from './trend-activity';

/**
 * The sentences a trend's page and card make out of its jobs and clips.
 *
 * The one pinned hardest is "nothing to cut": a job that completed and made
 * no clip has to be called that, because a COMPLETED chip beside an empty
 * review queue is the exact confusion this exists to end.
 */

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: 'job-1',
    uid: 'u1',
    type: 'CLIP',
    status: 'COMPLETED',
    stages: [
      { name: 'DOWNLOAD', lane: 'CPU', status: 'DONE' },
      { name: 'TRANSCRIBE', lane: 'GPU', status: 'DONE' },
      { name: 'ANALYZE', lane: 'GPU', status: 'DONE' },
      { name: 'RENDER', lane: 'CPU', status: 'DONE' },
    ],
    attempts: 0,
    maxAttempts: 3,
    createdAt: '2026-09-19T12:00:00.000Z',
    updatedAt: '2026-09-19T12:00:00.000Z',
    trendId: 't1',
    ...overrides,
  } as Job;
}

function clip(overrides: Partial<Clip> = {}): Clip {
  return {
    id: 'clip-1',
    uid: 'u1',
    jobId: 'job-1',
    review: 'PENDING',
    createdAt: '2026-09-19T12:05:00.000Z',
    ...overrides,
  } as Clip;
}

describe('kindOf', () => {
  it('names the two kinds a trend makes, and reads the rest off the type', () => {
    expect(kindOf(job())).toBe('Clip');
    expect(kindOf(job({ type: 'COMPILE' }))).toBe('Compilation');
    expect(kindOf(job({ type: 'COMPOSE' }))).toBe('Drawn video');
    expect(kindOf(job({ type: 'REMAKE' }))).toBe('Remake');
  });
});

describe('activityFor', () => {
  it('puts the clips under the job that made them, newest job first', () => {
    const older = job({ id: 'job-1', createdAt: '2026-09-19T10:00:00.000Z' });
    const newer = job({ id: 'job-2', createdAt: '2026-09-19T11:00:00.000Z' });
    const activity = activityFor(
      [older, newer],
      [clip({ id: 'c1', jobId: 'job-1' }), clip({ id: 'c2', jobId: 'job-1', review: 'APPROVED' })],
    );
    expect(activity.rows.map((row) => row.job.id)).toEqual(['job-2', 'job-1']);
    expect(activity.rows[1]?.clips.map((c) => c.id)).toEqual(['c1', 'c2']);
    expect(activity.toReview).toBe(1);
    expect(activity.approved).toBe(1);
  });

  it('calls a completed job with no clip "finished empty", and a running one not yet', () => {
    const done = job({ id: 'job-1' });
    const running = job({
      id: 'job-2',
      status: 'RUNNING',
      stages: [
        { name: 'DOWNLOAD', lane: 'CPU', status: 'DONE' },
        { name: 'TRANSCRIBE', lane: 'GPU', status: 'RUNNING', progress: 'Transcribing' },
        { name: 'ANALYZE', lane: 'GPU', status: 'PENDING' },
        { name: 'RENDER', lane: 'CPU', status: 'PENDING' },
      ],
    });
    const activity = activityFor([done, running], []);
    const byId = new Map(activity.rows.map((row) => [row.job.id, row]));
    expect(byId.get('job-1')?.finishedEmpty).toBe(true);
    expect(byId.get('job-2')?.finishedEmpty).toBe(false);
    expect(byId.get('job-2')?.progress).toBe('1 of 4 stages · TRANSCRIBE');
    expect(byId.get('job-2')?.note).toBe('Transcribing');
    expect(activity.running).toBe(1);
    expect(activity.empty).toBe(1);
  });
});

describe('describeActivity', () => {
  it('is nothing for a trend nobody has acted on', () => {
    expect(describeActivity(activityFor([], []))).toBeNull();
  });

  it('counts what a person would want to know, in the order they would ask', () => {
    const jobs = [
      job({ id: 'a', status: 'RUNNING' }),
      job({ id: 'b' }),
      job({ id: 'c' }),
      job({ id: 'd', status: 'FAILED' }),
    ];
    const clips = [
      clip({ id: 'c1', jobId: 'b' }),
      clip({ id: 'c2', jobId: 'b', review: 'APPROVED' }),
    ];
    expect(describeActivity(activityFor(jobs, clips))).toBe(
      '1 running · 1 to review · 1 approved · nothing to cut · 1 failed',
    );
  });

  it('falls back to a count when there is nothing more specific to say', () => {
    expect(describeActivity(activityFor([job({ status: 'CANCELLED' })], []))).toBe('1 job');
  });
});
