import type { Source } from '@clipforge/contracts';
import { describe, expect, it } from 'vitest';

import { SOURCE_PAGE, ofSourceSpec, sourceKind, sourcesOfKind, sourcesSpec } from './sources';

/**
 * The queries the Sources domain asks, and the one rule about reading them.
 *
 * Specs are tested as data because that is what they are now: a spec says what
 * Firestore is asked for, what it costs and which index it needs, and all three
 * are decisions rather than plumbing. The bound especially — Firestore bills a
 * read per delivered document and re-delivers everything on reconnect, so a
 * listener that lost its limit in a refactor is a bill nobody notices until the
 * month ends.
 */

/** A source as the worker writes one today. */
function source(over: Partial<Source> = {}): Source {
  return {
    id: 's1',
    uid: 'u1',
    provider: 'youtube',
    createdAt: '2026-09-01T00:00:00.000Z',
    ...over,
  } as Source;
}

describe('sourcesSpec', () => {
  it('bounds the library listener, because an unbounded listen re-bills on every reconnect', () => {
    expect(sourcesSpec().limit).toBe(SOURCE_PAGE);
  });

  it('orders by a field every source has, so none can be dropped for missing one', () => {
    // `useCount` is what the contract says the list sorts by, and Firestore's
    // orderBy omits documents without the field. Every source written before
    // this week lacks it, which would make them vanish rather than sort last.
    expect(sourcesSpec().orderBy).toEqual([['createdAt', 'desc']]);
  });

  it('describes one query whatever kind is being looked for, so the gate shares the listener', () => {
    expect(sourcesSpec()).toEqual(sourcesSpec());
  });
});

describe('ofSourceSpec', () => {
  it('pages the delete cascade at the batch ceiling', () => {
    expect(ofSourceSpec('clips', 's1', 500)).toEqual({
      collection: 'clips',
      where: [['sourceId', '==', 's1']],
      limit: 500,
    });
  });

  it('leaves a counted query unbounded, because an aggregation delivers a number and not documents', () => {
    expect(ofSourceSpec('candidates', 's1').limit).toBeUndefined();
  });

  it('asks each collection for its own children only', () => {
    expect(ofSourceSpec('candidates', 's2').where).toEqual([['sourceId', '==', 's2']]);
    expect(ofSourceSpec('candidates', 's2').collection).toBe('candidates');
  });
});

describe('sourceKind', () => {
  it('reads a source written before the field existed as video', () => {
    // The contract is explicit that null and absent both mean video. This is
    // the reason the kind filter is applied to delivered documents instead of
    // in the query: `where('kind', '==', 'video')` matches the stored field,
    // so it would return none of the library that predates this week.
    expect(sourceKind(source())).toBe('video');
    // Cast because the generated TypeScript declares `kind?: SourceKind` while
    // the Python contract declares it nullable, so a document really can arrive
    // carrying an explicit null that the declared type says is impossible.
    expect(sourceKind({ ...source(), kind: null } as unknown as Source)).toBe('video');
  });

  it('takes the field at its word when there is one', () => {
    expect(sourceKind(source({ kind: 'music' }))).toBe('music');
    expect(sourceKind(source({ kind: 'video' }))).toBe('video');
  });
});

describe('sourcesOfKind', () => {
  const library = [
    source({ id: 'legacy' }),
    source({ id: 'video', kind: 'video' }),
    source({ id: 'track', kind: 'music' }),
  ];

  it('keeps the untagged library with the videos', () => {
    expect(sourcesOfKind(library, 'video').map((s) => s.id)).toEqual(['legacy', 'video']);
  });

  it('never counts an untagged source as music', () => {
    expect(sourcesOfKind(library, 'music').map((s) => s.id)).toEqual(['track']);
  });

  it('holds the delivered order, which is the order the query decided', () => {
    expect(sourcesOfKind(library, 'video')[0]?.id).toBe('legacy');
  });
});
