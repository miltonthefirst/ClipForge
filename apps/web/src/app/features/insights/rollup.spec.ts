import type { MetricSnapshot } from '@clipforge/contracts';
import { describe, expect, it } from 'vitest';

import { rollUp, retentionAtHalf, toCurves } from './rollup';

/** The y of the first point of an SVG polyline: 'x,y x,y x,y'. */
function firstY(points: string): number {
  return Number(points.split(' ')[0]!.split(',')[1]);
}

/**
 * The two ways a dashboard lies while rendering perfectly.
 *
 * Summing a field that is already an average, and normalising each curve to its
 * own maximum. Neither produces an error, an empty screen, or anything a
 * screenshot would catch — they produce a chart that looks right and reports
 * something that did not happen.
 */
function snapshot(overrides: Partial<MetricSnapshot> = {}): MetricSnapshot {
  return {
    id: 'pub-1_2026-09-10',
    uid: 'user-1',
    clipId: 'clip-1',
    publicationId: 'pub-1',
    platform: 'YOUTUBE',
    externalId: 'vid123',
    date: '2026-09-10',
    views: 10,
    likes: 1,
    comments: 0,
    shares: 0,
    subscribersGained: 0,
    estimatedMinutesWatched: 1,
    averageViewDurationSec: null,
    averageViewPercentage: null,
    retention: [],
    partial: false,
    fetchedAt: '2026-09-14T06:00:00.000Z',
    ...overrides,
  };
}

describe('rolling daily snapshots up to one row per clip', () => {
  it('sums views across days, because the API reports them per day', () => {
    const rows = rollUp([
      snapshot({ id: 'a', date: '2026-09-10', views: 100 }),
      snapshot({ id: 'b', date: '2026-09-11', views: 60 }),
      snapshot({ id: 'c', date: '2026-09-12', views: 15 }),
    ]);

    expect(rows).toHaveLength(1);
    expect(rows[0]!.views).toBe(175);
    expect(rows[0]!.days).toBe(3);
  });

  it('does NOT average the watch percentage, which is already an average', () => {
    // Day one is a single early viewer who watched all of it; by day three the
    // real figure has settled at 40%. Averaging the three would report 60%.
    const rows = rollUp([
      snapshot({ id: 'a', date: '2026-09-10', averageViewPercentage: 100 }),
      snapshot({ id: 'b', date: '2026-09-11', averageViewPercentage: 40 }),
      snapshot({ id: 'c', date: '2026-09-12', averageViewPercentage: 40 }),
    ]);

    expect(rows[0]!.viewPercentage).toBe(40);
  });

  it('falls back to the most recent day that has a percentage at all', () => {
    const rows = rollUp([
      snapshot({ id: 'a', date: '2026-09-10', averageViewPercentage: 55 }),
      snapshot({ id: 'b', date: '2026-09-11', averageViewPercentage: null }),
    ]);

    expect(rows[0]!.viewPercentage).toBe(55);
  });

  it('reports no percentage rather than zero when none was ever given', () => {
    // YouTube withholds this below its privacy threshold. Zero would mean
    // "nobody watched any of it", which is a different and much worse claim.
    const rows = rollUp([snapshot({ averageViewPercentage: null })]);
    expect(rows[0]!.viewPercentage).toBeNull();
  });

  it('keeps clips apart and orders them by views', () => {
    const rows = rollUp([
      snapshot({ id: 'a', publicationId: 'pub-1', clipId: 'clip-1', views: 10 }),
      snapshot({ id: 'b', publicationId: 'pub-2', clipId: 'clip-2', views: 900 }),
    ]);

    expect(rows.map((r) => r.publicationId)).toEqual(['pub-2', 'pub-1']);
  });

  it('takes the curve from the latest day that has one', () => {
    const rows = rollUp([
      snapshot({ id: 'a', date: '2026-09-10', retention: [] }),
      snapshot({
        id: 'b',
        date: '2026-09-11',
        retention: [
          { elapsedRatio: 0, audienceWatchRatio: 1 },
          { elapsedRatio: 1, audienceWatchRatio: 0.2 },
        ],
      }),
    ]);

    expect(rows[0]!.curve).toHaveLength(2);
  });

  it('handles nothing at all', () => {
    expect(rollUp([])).toEqual([]);
  });
});

describe('retention at the midpoint', () => {
  it('interpolates rather than taking the nearest sample', () => {
    expect(
      retentionAtHalf([
        { x: 0, y: 1 },
        { x: 1, y: 0 },
      ]),
    ).toBeCloseTo(0.5);
  });

  it('clamps to the ends of a curve that does not reach the middle', () => {
    const curve = [
      { x: 0.6, y: 0.7 },
      { x: 1, y: 0.3 },
    ];
    expect(retentionAtHalf(curve)).toBeCloseTo(0.7);
  });

  it('is null for a withheld curve, not zero', () => {
    expect(retentionAtHalf([])).toBeNull();
  });
});

describe('drawing curves', () => {
  const curved = (publicationId: string, peak: number) =>
    snapshot({
      id: publicationId,
      publicationId,
      retention: [
        { elapsedRatio: 0, audienceWatchRatio: peak },
        { elapsedRatio: 0.5, audienceWatchRatio: peak / 2 },
        { elapsedRatio: 1, audienceWatchRatio: 0 },
      ],
    });

  it('puts every curve on one scale so two can be compared', () => {
    // One clip retains twice as well as the other. Per-curve normalisation
    // would draw them identically, which is the opposite of the point.
    const rows = rollUp([curved('pub-1', 1), curved('pub-2', 2)]);
    const curves = toCurves(rows, 100, 100);

    const one = firstY(curves.find((c) => c.publicationId === 'pub-1')!.points);
    const two = firstY(curves.find((c) => c.publicationId === 'pub-2')!.points);

    // Lower y is higher on the chart, so the better-retained clip starts above.
    expect(two).toBeLessThan(one);
  });

  it('never scales a poorly retained clip up to fill the box', () => {
    // Ceiling floors at 1, so a curve that never exceeds 0.5 occupies the
    // bottom half of the chart rather than being stretched over all of it.
    const rows = rollUp([curved('pub-1', 0.5)]);
    const [curve] = toCurves(rows, 100, 100);
    expect(firstY(curve!.points)).toBeGreaterThan(40);
  });

  it('skips a clip with no curve rather than drawing a flat line', () => {
    const rows = rollUp([snapshot({ retention: [] })]);
    expect(toCurves(rows, 100, 100)).toEqual([]);
  });

  it('reports the midpoint alongside the path', () => {
    const rows = rollUp([curved('pub-1', 1)]);
    const [curve] = toCurves(rows, 100, 100);
    expect(curve!.midpoint).toBeCloseTo(0.5);
  });
});
