import { describe, expect, it } from 'vitest';

import { cacheKey, type QuerySpec } from './spec';

/**
 * Keys derived from the query itself.
 *
 * The one way a shared-listener cache fails while still looking like working
 * software is two different queries sharing a key: the second caller gets the
 * first one's rows and nothing anywhere says so. Hand-written keys made that a
 * thing to remember. These tests are what make it a thing that cannot happen.
 */

const jobs = (over: Partial<QuerySpec> = {}): QuerySpec => ({ collection: 'jobs', ...over });

describe('cacheKey', () => {
  it('separates two queries that differ only by their filter', () => {
    const active = jobs({ where: [['status', 'in', ['RUNNING', 'QUEUED']]] });
    const done = jobs({ where: [['status', 'in', ['COMPLETED']]] });

    expect(cacheKey(active)).not.toBe(cacheKey(done));
  });

  it('separates a filtered query from an unfiltered one', () => {
    expect(cacheKey(jobs())).not.toBe(cacheKey(jobs({ where: [['status', '==', 'FAILED']] })));
  });

  it('separates two collections', () => {
    expect(cacheKey({ collection: 'jobs' })).not.toBe(cacheKey({ collection: 'clips' }));
  });

  it('separates two limits, because the shorter one is not the longer one', () => {
    expect(cacheKey(jobs({ limit: 25 }))).not.toBe(cacheKey(jobs({ limit: 50 })));
  });

  it('separates two orderings', () => {
    expect(cacheKey(jobs({ orderBy: [['createdAt', 'desc']] }))).not.toBe(
      cacheKey(jobs({ orderBy: [['createdAt', 'asc']] })),
    );
  });

  it('separates a string from the number that looks like it', () => {
    // Firestore treats these as different values, so the cache must too.
    expect(cacheKey(jobs({ where: [['attempts', '==', 1]] }))).not.toBe(
      cacheKey(jobs({ where: [['attempts', '==', '1']] })),
    );
  });

  it('shares between two callers who asked the same thing in a different order', () => {
    // They are the same question. Making them two listeners would bill twice
    // for one answer.
    const one = jobs({
      where: [
        ['status', '==', 'RUNNING'],
        ['uid', '==', 'u1'],
      ],
    });
    const other = jobs({
      where: [
        ['uid', '==', 'u1'],
        ['status', '==', 'RUNNING'],
      ],
    });

    expect(cacheKey(one)).toBe(cacheKey(other));
  });

  it('treats an absent clause and an empty one as the same query', () => {
    expect(cacheKey(jobs())).toBe(cacheKey(jobs({ where: [], orderBy: [] })));
  });

  it('does not let a value containing the separator forge another key', () => {
    // A field value is user data in the general case, and JSON-encoding it is
    // what stops it impersonating the structure around it.
    const forged = jobs({ where: [['submission', '==', '|createdAt:desc|25']] });
    expect(cacheKey(forged)).not.toBe(cacheKey(jobs({ orderBy: [['createdAt', 'desc']], limit: 25 })));
  });

  it('is stable for the same spec described twice', () => {
    const spec = jobs({ where: [['status', 'in', ['RUNNING']]], orderBy: [['createdAt', 'desc']], limit: 25 });
    expect(cacheKey(spec)).toBe(cacheKey({ ...spec }));
  });
});
