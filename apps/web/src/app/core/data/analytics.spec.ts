import type { MetricSnapshot } from '@clipforge/contracts';
import { describe, expect, it } from 'vitest';

import { LATEST_CALIBRATION_SPEC, METRICS_SPEC, oldestFirst } from './analytics';

/** A snapshot with only the day distinguishing it, which is all these check. */
function on(date: string): MetricSnapshot {
  return {
    id: `pub-1_${date}`,
    uid: 'user-1',
    clipId: 'clip-1',
    publicationId: 'pub-1',
    platform: 'YOUTUBE',
    externalId: 'vid123',
    date,
    fetchedAt: '2026-09-14T06:00:00.000Z',
  };
}

/** The order the metrics query delivers: newest day first. */
const delivered = [on('2026-09-12'), on('2026-09-11'), on('2026-09-10')];

describe('the metrics query', () => {
  it('asks for the newest days, because a bound that kept the oldest end would freeze the dashboard on the first clips ever published', () => {
    expect(METRICS_SPEC.orderBy).toEqual([['date', 'desc']]);
    expect(METRICS_SPEC.limit).toBe(2000);
  });

  it('does not filter by owner, because clips published under a second account are still the same channel', () => {
    expect(METRICS_SPEC.where).toBeUndefined();
  });
});

describe('oldestFirst', () => {
  it('puts the oldest day first, so a chart drawn from it reads left to right in time', () => {
    expect(oldestFirst(delivered).map((row) => row.date)).toEqual([
      '2026-09-10',
      '2026-09-11',
      '2026-09-12',
    ]);
  });

  it('leaves the array it was handed alone, because the shared listener gives every other reader the same one', () => {
    const rows = [...delivered];
    oldestFirst(rows);
    expect(rows.map((row) => row.date)).toEqual(['2026-09-12', '2026-09-11', '2026-09-10']);
  });

  it('has nothing to say about an empty history', () => {
    expect(oldestFirst([])).toEqual([]);
  });
});

describe('the calibration query', () => {
  it('asks for one report rather than the history behind it', () => {
    expect(LATEST_CALIBRATION_SPEC.limit).toBe(1);
    expect(LATEST_CALIBRATION_SPEC.orderBy).toEqual([['generatedAt', 'desc']]);
  });
});
