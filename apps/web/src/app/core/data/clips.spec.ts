import type { Clip, MetricSnapshot } from '@clipforge/contracts';
import { describe, expect, it } from 'vitest';

import {
  byDate,
  byVersion,
  clipMetricsSpec,
  isAlreadyCollected,
  lineageSettlement,
  lineageSpec,
  reviewQueueSpec,
} from './clips';
import { cacheKey } from '../firestore/spec';

/**
 * The parts of the clips repository that decide something.
 *
 * What is asserted here is the part that is silent when wrong. A bound that
 * slips spends quota on a queue nobody is reading and says nothing; an approval
 * that writes REJECTED onto the earlier versions throws away work and looks
 * like tidying; a storage failure mistaken for "already collected" leaves a
 * document claiming there is no bucket copy while the bytes are still there.
 */

function clip(overrides: Partial<Clip> = {}): Clip {
  return {
    id: 'clip-1',
    uid: 'user-1',
    candidateId: 'cand-1',
    location: 'LOCAL',
    localPath: 'P:\\workspace\\clips\\user-1\\clip-1.mp4',
    review: 'PENDING',
    createdAt: '2026-09-08T12:00:00Z',
    ...overrides,
  } as Clip;
}

function snapshot(date: string): MetricSnapshot {
  return { id: `m-${date}`, clipId: 'clip-1', date } as MetricSnapshot;
}

/** A Firebase Storage error, which carries its reason in `code`. */
function storageError(code: string): Error & { code: string } {
  return Object.assign(new Error(code), { code });
}

describe('the review-queue query', () => {
  it('asks for one review state, and not for one reviewer', () => {
    // ClipForge is a single shared workspace: a uid filter here would show the
    // desktop a different queue from the phone that submitted the job.
    expect(reviewQueueSpec('PENDING').where).toEqual([['review', '==', 'PENDING']]);
  });

  it('stays bounded at fifty, because every row is delivered again on reconnect', () => {
    expect(reviewQueueSpec('PENDING').limit).toBe(50);
  });

  it('puts the newest clip first, which is where the reviewer starts', () => {
    expect(reviewQueueSpec('PENDING').orderBy).toEqual([['createdAt', 'desc']]);
  });

  it('gives the pending queue and the approved list separate cache keys', () => {
    // They share a shape and differ only by the filter. Sharing a listener
    // would hand the publish page the review queue's rows with nothing
    // anywhere saying so.
    expect(cacheKey(reviewQueueSpec('PENDING'))).not.toBe(cacheKey(reviewQueueSpec('APPROVED')));
  });
});

describe('the lineage query', () => {
  it('finds every version by the id they share, rather than walking parents', () => {
    expect(lineageSpec('lin-1').where).toEqual([['lineageId', '==', 'lin-1']]);
  });

  it('stays short enough that settling a lineage is one batched write', () => {
    // Firestore caps a batch at 500 and the gate leaves paging to the caller.
    // This bound is what means there is nothing to page.
    expect(lineageSpec('lin-1').limit).toBe(25);
  });
});

describe('the per-clip metrics query', () => {
  it('leaves the ordering out, so the filter needs no composite index', () => {
    expect(clipMetricsSpec('clip-1').orderBy).toBeUndefined();
  });

  it('is bounded like every other read here', () => {
    expect(clipMetricsSpec('clip-1').limit).toBe(400);
  });
});

describe('byVersion', () => {
  it('reads a lineage as the attempts happened, first cut first', () => {
    const ordered = byVersion([
      clip({ id: 'c3', version: 3 }),
      clip({ id: 'c1', version: 1 }),
      clip({ id: 'c2', version: 2 }),
    ]);

    expect(ordered.map((c) => c.id)).toEqual(['c1', 'c2', 'c3']);
  });

  it('treats a clip written before versions existed as the first attempt', () => {
    const ordered = byVersion([clip({ id: 'remake', version: 2 }), clip({ id: 'original' })]);

    expect(ordered.map((c) => c.id)).toEqual(['original', 'remake']);
  });

  it('leaves the list it was handed alone', () => {
    // The list comes from a shared listener's snapshot in the general case, and
    // sorting it in place would reorder what every other reader sees.
    const given = [clip({ id: 'c2', version: 2 }), clip({ id: 'c1', version: 1 })];
    byVersion(given);

    expect(given.map((c) => c.id)).toEqual(['c2', 'c1']);
  });
});

describe('byDate', () => {
  it('reads oldest day first, whatever order the snapshots arrived in', () => {
    const ordered = byDate([
      snapshot('2026-09-10'),
      snapshot('2026-08-31'),
      snapshot('2026-09-01'),
    ]);

    expect(ordered.map((s) => s.date)).toEqual(['2026-08-31', '2026-09-01', '2026-09-10']);
  });
});

describe('lineageSettlement', () => {
  it('supersedes the earlier versions when the latest one is approved', () => {
    expect(lineageSettlement('APPROVED', 'now')).toEqual({ supersededAt: 'now' });
  });

  it('does not reject the earlier versions on an approval', () => {
    // Superseded is recoverable and REJECTED is not: the worker's tidy pass
    // bins a rejected clip's files, and approving the fifth cut is not a
    // decision to destroy the first four.
    expect(lineageSettlement('APPROVED', 'now')).not.toHaveProperty('review');
  });

  it('rejects the whole lineage when the latest one is rejected', () => {
    expect(lineageSettlement('REJECTED', 'now')).toEqual({
      review: 'REJECTED',
      reviewedAt: 'now',
    });
  });

  it('carries nothing when a clip is put back to PENDING', () => {
    // An undecided clip is the absence of a decision. There is nothing to
    // settle on the versions it was also about.
    expect(lineageSettlement('PENDING', 'now')).toBeNull();
  });
});

describe('isAlreadyCollected', () => {
  it('treats an object the lifecycle rule already removed as released', () => {
    expect(isAlreadyCollected(storageError('storage/object-not-found'))).toBe(true);
  });

  it('keeps the pointer when the bucket refused the delete', () => {
    // The bytes are still there. Clearing `storagePath` here would leave a
    // document that says the copy is gone and a bucket that is still billing
    // for it.
    expect(isAlreadyCollected(storageError('storage/unauthorized'))).toBe(false);
  });

  it('keeps the pointer when the failure carries no code at all', () => {
    expect(isAlreadyCollected(new Error('network request failed'))).toBe(false);
    expect(isAlreadyCollected(null)).toBe(false);
    expect(isAlreadyCollected(undefined)).toBe(false);
  });
});
