import type { ResearchSchedule } from '@clipforge/contracts';
import { describe, expect, it } from 'vitest';

import { SCHEDULE_PAGE, byName, newSchedule, scheduleUpdate, schedulesSpec } from './schedules';

/**
 * The one query and the one document the schedule list writes.
 *
 * The document matters most: the rules refuse a schedule that arrives with
 * the worker's fields filled in, and a shape drift here would be a Save button
 * that fails with a bare permission error.
 */

function schedule(over: Partial<ResearchSchedule> = {}): ResearchSchedule {
  return {
    id: 's1',
    uid: 'u1',
    name: 'Mornings',
    enabled: true,
    cadence: 'DAILY',
    at: '07:30',
    options: {},
    createdAt: '2026-09-19T12:00:00.000Z',
    updatedAt: '2026-09-19T12:00:00.000Z',
    ...over,
  } as ResearchSchedule;
}

describe('schedulesSpec', () => {
  it('is bounded and unordered, so it needs no index', () => {
    expect(schedulesSpec()).toEqual({ collection: 'schedules', limit: SCHEDULE_PAGE });
  });
});

describe('byName', () => {
  it('sorts by name', () => {
    const rows = byName([
      schedule({ id: 'b', name: 'Weekly' }),
      schedule({ id: 'a', name: 'Mornings' }),
    ]);
    expect(rows.map((row) => row.id)).toEqual(['a', 'b']);
  });
});

describe('scheduleUpdate', () => {
  const now = '2026-09-19T12:00:00.000Z';

  it('writes only what the rules let an update touch', () => {
    const made = scheduleUpdate(
      schedule({
        cadence: 'INTERVAL',
        everyHours: 12,
        at: null,
        nextDueAt: '2026-09-19T20:00:00.000Z',
      }),
      {
        name: ' Evenings ',
        cadence: 'INTERVAL',
        everyHours: 12,
        at: '',
        options: { topics: ['nba'] },
      },
      now,
    );
    expect(Object.keys(made).sort()).toEqual(
      [
        'at',
        'cadence',
        'everyHours',
        'name',
        'nextDueAt',
        'options',
        'timezone',
        'updatedAt',
      ].sort(),
    );
    expect(made.name).toBe('Evenings');
    expect(made.options).toEqual({ topics: ['nba'] });
    // Same interval: the due time it already had.
    expect(made.nextDueAt).toBe('2026-09-19T20:00:00.000Z');
    expect(made.updatedAt).toBe(now);
  });

  it('switching to daily records the zone and drops the interval', () => {
    const made = scheduleUpdate(
      schedule({ cadence: 'INTERVAL', everyHours: 12, at: null }),
      { name: 'x', cadence: 'DAILY', everyHours: 12, at: '06:00', options: {} },
      now,
    );
    expect(made.at).toBe('06:00');
    expect(made.everyHours).toBeNull();
    expect(typeof made.timezone).toBe('string');
  });
});

describe('newSchedule', () => {
  const now = '2026-09-19T12:00:00.000Z';

  it('writes a daily schedule with the browser’s zone and no interval', () => {
    const made = newSchedule(
      's1',
      'u1',
      {
        name: ' Mornings ',
        cadence: 'DAILY',
        everyHours: 12,
        at: '07:30',
        options: { topics: ['f1'] },
      },
      now,
    );
    expect(made.name).toBe('Mornings');
    expect(made.cadence).toBe('DAILY');
    expect(made.at).toBe('07:30');
    expect(made.everyHours).toBeNull();
    expect(typeof made.timezone).toBe('string');
    expect(made.options).toEqual({ topics: ['f1'] });
    expect(made.enabled).toBe(true);
  });

  it('writes an interval schedule due now, with no time of day', () => {
    const made = newSchedule(
      's2',
      'u1',
      { name: 'Often', cadence: 'INTERVAL', everyHours: 12, at: '07:30', options: {} },
      now,
    );
    expect(made.everyHours).toBe(12);
    expect(made.at).toBeNull();
    expect(made.timezone).toBeNull();
    expect(made.nextDueAt).toBe(now);
  });

  it("leaves the worker's record blank, which is what the rules require", () => {
    const made = newSchedule(
      's3',
      'u1',
      { name: 'x', cadence: 'INTERVAL', everyHours: 24, at: '', options: {} },
      now,
    );
    expect(made.lastRunAt).toBeNull();
    expect(made.lastJobId).toBeNull();
    expect(made.lastOutcome).toBeNull();
  });
});
