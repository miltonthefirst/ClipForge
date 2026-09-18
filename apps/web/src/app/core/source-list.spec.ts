import type { Source } from '@clipforge/contracts';
import { describe, expect, it } from 'vitest';

import {
  SOURCE_TABS,
  byUse,
  describeUse,
  initial,
  isKept,
  readableLength,
  readableSize,
  sourceTab,
} from './source-list';

function source(over: Partial<Source> = {}): Source {
  return {
    id: 's1',
    uid: 'u1',
    provider: 'youtube',
    createdAt: '2026-09-01T00:00:00.000Z',
    ...over,
  } as Source;
}

describe('ordering the library', () => {
  it('puts what has been used most at the top', () => {
    const ordered = byUse([
      source({ id: 'once', useCount: 1 }),
      source({ id: 'often', useCount: 9 }),
      source({ id: 'never', useCount: 0 }),
    ]);

    expect(ordered.map((s) => s.id)).toEqual(['often', 'once', 'never']);
  });

  it('breaks a tie on what was touched most recently', () => {
    // Without this a fresh library is all zeroes and ones, and the order is
    // whatever Firestore returned — different on every visit, for no reason the
    // reviewer can see.
    const ordered = byUse([
      source({ id: 'older', useCount: 1, lastAccessedAt: '2026-09-01T00:00:00.000Z' }),
      source({ id: 'newer', useCount: 1, lastAccessedAt: '2026-09-17T00:00:00.000Z' }),
    ]);

    expect(ordered.map((s) => s.id)).toEqual(['newer', 'older']);
  });

  it('falls back to when it arrived, for a source nothing has touched', () => {
    const ordered = byUse([
      source({ id: 'old', createdAt: '2026-01-01T00:00:00.000Z' }),
      source({ id: 'new', createdAt: '2026-09-01T00:00:00.000Z' }),
    ]);

    expect(ordered.map((s) => s.id)).toEqual(['new', 'old']);
  });

  it('does not reorder the array it was given', () => {
    const input = [source({ id: 'a', useCount: 1 }), source({ id: 'b', useCount: 5 })];
    byUse(input);
    expect(input.map((s) => s.id)).toEqual(['a', 'b']);
  });
});

describe('what the collector will keep', () => {
  it('keeps anything used more than once', () => {
    // The worker's rule, mirrored: somebody who came back to a source will come
    // back again, and re-fetching risks a video that has since been taken down.
    expect(isKept(source({ useCount: 2 }))).toBe(true);
    expect(isKept(source({ useCount: 1 }))).toBe(false);
    expect(isKept(source({ useCount: 0 }))).toBe(false);
  });

  it('keeps anything pinned by hand, however little it has been used', () => {
    expect(isKept(source({ pinned: true, useCount: 0 }))).toBe(true);
  });
});

describe('reading the numbers', () => {
  it('scales bytes to something a person can hold in their head', () => {
    expect(readableSize(512)).toBe('1 KB');
    expect(readableSize(5 * 1_048_576)).toBe('5.0 MB');
    expect(readableSize(300 * 1_048_576)).toBe('300 MB');
    expect(readableSize(2 * 1024 * 1_048_576)).toBe('2.0 GB');
  });

  it('says nothing rather than zero when a size is unknown', () => {
    // A row that says "0 KB" is making a claim about a file it has not measured.
    expect(readableSize(null)).toBeNull();
    expect(readableSize(0)).toBeNull();
    expect(readableSize(undefined)).toBeNull();
  });

  it('drops the hour from anything shorter than one', () => {
    expect(readableLength(95)).toBe('1:35');
    expect(readableLength(3725)).toBe('1:02:05');
  });

  it('says nothing rather than 0:00 when a length is unknown', () => {
    expect(readableLength(null)).toBeNull();
    expect(readableLength(0)).toBeNull();
  });
});

describe('describing a source', () => {
  it('counts uses in words', () => {
    expect(describeUse(source({ useCount: 0 }))).toBe('never used');
    expect(describeUse(source({ useCount: 1 }))).toBe('used once');
    expect(describeUse(source({ useCount: 4 }))).toBe('used 4 times');
  });

  it('reads a missing count as never, not as unknown', () => {
    // Every source written before `useCount` existed has none, and none of them
    // has been used since the counting started.
    expect(describeUse(source())).toBe('never used');
  });

  it('takes a letter from the title for a source with no picture', () => {
    expect(initial(source({ title: 'canal+ highlights' }))).toBe('C');
  });

  it('falls back to the id, then to a question mark', () => {
    expect(initial(source({ title: null, externalId: 'dQw4w9WgXcQ' }))).toBe('D');
    expect(initial(source({ title: null, externalId: null }))).toBe('?');
    expect(initial(source({ title: '   ' }))).toBe('?');
  });
});

describe('the tabs', () => {
  it('falls through to everything for a tab name the URL made up', () => {
    expect(sourceTab('nonsense')).toBe('all');
    expect(sourceTab(null)).toBe('all');
    expect(sourceTab(undefined)).toBe('all');
  });

  it('keeps a tab name it recognises', () => {
    expect(sourceTab('music')).toBe('music');
    expect(sourceTab('video')).toBe('video');
  });

  it('offers a tab for every kind a source can be, plus everything', () => {
    expect(SOURCE_TABS.map((t) => t.key)).toEqual(['all', 'video', 'music']);
  });
});
