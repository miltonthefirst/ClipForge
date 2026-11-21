import type { Track, TrackStatus } from '@clipforge/contracts';
import { describe, expect, it } from 'vitest';

import { openingTab, tabCounts, tracksIn } from './track-tabs';

const track = (id: string, status: TrackStatus): Track => ({
  id,
  title: id,
  prompt: 'amapiano',
  seconds: 120,
  instrumental: false,
  model: 'ACE-Step 1.5',
  licence: 'MIT',
  localPath: `P:/workspace/music/generated/${id}.wav`,
  status,
  createdAt: '2026-11-21T00:00:00Z',
});

const library = [
  track('a', 'NEW'),
  track('b', 'KEPT'),
  track('c', 'KEPT'),
  track('d', 'DISCARDED'),
];

describe('track tabs', () => {
  it('groups the library by what was decided, keeping its order', () => {
    expect(tracksIn(library, 'KEPT').map((t) => t.id)).toEqual(['b', 'c']);
    expect(tracksIn(library, 'ALL')).toHaveLength(4);
    expect(tabCounts(library)).toEqual({ NEW: 1, KEPT: 2, DISCARDED: 1, ALL: 4 });
  });

  it('opens on the last tab chosen while it has tracks, else on what waits to be heard', () => {
    expect(openingTab(library, 'DISCARDED')).toBe('DISCARDED');
    expect(openingTab(library, null)).toBe('NEW');
    expect(openingTab(library, 'nonsense')).toBe('NEW');
    const decided = [track('b', 'KEPT'), track('d', 'DISCARDED')];
    expect(openingTab(decided, 'NEW')).toBe('KEPT');
    expect(openingTab([track('d', 'DISCARDED')], null)).toBe('ALL');
  });
});
