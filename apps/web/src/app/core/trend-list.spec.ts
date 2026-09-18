import type { Job } from '@clipforge/contracts';
import { describe, expect, it } from 'vitest';

import {
  curationState,
  describeSignal,
  parseTopics,
  readableAge,
  readableDuration,
  readableViews,
  runInProgress,
  runLabel,
} from './trend-list';

/**
 * The sentences the Trends page makes out of documents.
 *
 * Each one is quiet when wrong — a view count that reads as thousands when it
 * was millions, an age that says hours about last week — so each is pinned.
 */

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: 'job-1',
    uid: 'u1',
    type: 'RESEARCH',
    status: 'COMPLETED',
    stages: [
      { name: 'RESEARCH', lane: 'CPU', status: 'DONE' },
      { name: 'CURATE', lane: 'GPU', status: 'DONE' },
    ],
    attempts: 0,
    maxAttempts: 2,
    createdAt: '2026-09-19T12:00:00.000Z',
    updatedAt: '2026-09-19T12:00:00.000Z',
    ...overrides,
  } as Job;
}

describe('parseTopics', () => {
  it('splits on commas and lines, trims, and drops blanks', () => {
    expect(parseTopics(' premier league,\n formula 1 ,, ')).toEqual([
      'premier league',
      'formula 1',
    ]);
  });

  it('deduplicates regardless of case and keeps the first spelling', () => {
    expect(parseTopics('F1, f1, F1')).toEqual(['F1']);
  });

  it('caps the list at the twelve the contract allows', () => {
    const many = Array.from({ length: 20 }, (_, i) => `topic ${i}`).join(', ');
    expect(parseTopics(many)).toHaveLength(12);
  });

  it('caps each topic at eighty characters', () => {
    expect(parseTopics('x'.repeat(100))[0]).toHaveLength(80);
  });
});

describe('describeSignal', () => {
  it('names the provider and quotes its own number', () => {
    expect(
      describeSignal({ source: 'GOOGLE_TRENDS', strength: 0.6, detail: '200K+ searches' }),
    ).toBe('Google Trends · 200K+ searches');
  });

  it('names the provider alone when it gave no number', () => {
    expect(describeSignal({ source: 'REDDIT', strength: 0.3 })).toBe('Reddit');
  });
});

describe('readableViews', () => {
  it('rounds to the unit a person reads', () => {
    expect(readableViews(7_706_726)).toBe('7.7M');
    expect(readableViews(1_000_000)).toBe('1M');
    expect(readableViews(45_400)).toBe('45K');
    expect(readableViews(312)).toBe('312');
  });

  it('is nothing when nothing is known', () => {
    expect(readableViews(null)).toBeNull();
    expect(readableViews(undefined)).toBeNull();
  });
});

describe('readableAge', () => {
  const now = Date.parse('2026-09-19T12:00:00.000Z');

  it('picks the coarsest honest unit', () => {
    expect(readableAge('2026-09-19T11:40:00.000Z', now)).toBe('just now');
    expect(readableAge('2026-09-19T02:00:00.000Z', now)).toBe('10h ago');
    expect(readableAge('2026-09-16T12:00:00.000Z', now)).toBe('3d ago');
    expect(readableAge('2026-08-19T12:00:00.000Z', now)).toBe('4w ago');
  });

  it('is nothing for an unknown or unreadable time', () => {
    expect(readableAge(null, now)).toBeNull();
    expect(readableAge('yesterday', now)).toBeNull();
  });
});

describe('readableDuration', () => {
  it('reads as a clock', () => {
    expect(readableDuration(485)).toBe('8:05');
    expect(readableDuration(3725)).toBe('1:02:05');
    expect(readableDuration(7)).toBe('0:07');
  });
});

describe('runLabel', () => {
  it('is the topics when there were any', () => {
    expect(runLabel(job({ researchOptions: { topics: ['f1', 'nba'] } }))).toBe('f1, nba');
  });

  it('says plainly when the run asked about nothing in particular', () => {
    expect(runLabel(job({ researchOptions: {} }))).toBe('whatever is trending');
    expect(runLabel(job({ researchOptions: null }))).toBe('whatever is trending');
  });
});

describe('runInProgress', () => {
  it('is true only while a worker has it or could take it', () => {
    expect(runInProgress(job({ status: 'QUEUED' }))).toBe(true);
    expect(runInProgress(job({ status: 'RUNNING' }))).toBe(true);
    expect(runInProgress(job({ status: 'COMPLETED' }))).toBe(false);
    expect(runInProgress(job({ status: 'FAILED' }))).toBe(false);
  });
});

describe('curationState', () => {
  it('is done once the model has had its say', () => {
    expect(curationState(job())).toBe('done');
  });

  it('is skipped when the model had nothing to add, or the run stopped', () => {
    expect(
      curationState(
        job({
          stages: [
            { name: 'RESEARCH', lane: 'CPU', status: 'DONE' },
            { name: 'CURATE', lane: 'GPU', status: 'SKIPPED' },
          ],
        }),
      ),
    ).toBe('skipped');
    expect(
      curationState(
        job({
          status: 'FAILED',
          stages: [
            { name: 'RESEARCH', lane: 'CPU', status: 'FAILED' },
            { name: 'CURATE', lane: 'GPU', status: 'PENDING' },
          ],
        }),
      ),
    ).toBe('skipped');
  });

  it('is pending while the rows are ranked but not yet explained', () => {
    expect(
      curationState(
        job({
          status: 'RUNNING',
          stages: [
            { name: 'RESEARCH', lane: 'CPU', status: 'DONE' },
            { name: 'CURATE', lane: 'GPU', status: 'RUNNING' },
          ],
        }),
      ),
    ).toBe('pending');
  });
});
