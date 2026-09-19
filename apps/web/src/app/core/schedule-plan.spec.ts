import { describe, expect, it } from 'vitest';

import { describeCadence, describeNext, firstDue, scheduleProblems } from './schedule-plan';

/**
 * The sentences and the one instant the schedule form works out.
 *
 * `firstDue` for DAILY is computed in the browser's own zone, which is the
 * zone the schedule records, so the tests build expectations the same way
 * rather than hardcoding an offset that would be wrong on another machine.
 */

describe('firstDue', () => {
  it('fires an interval schedule straight away', () => {
    const now = new Date('2026-09-19T09:00:00.000Z');
    expect(firstDue('INTERVAL', '', now)).toBe(now.toISOString());
  });

  it('picks today’s time of day when it has not passed, else tomorrow’s', () => {
    const now = new Date();
    now.setHours(10, 0, 0, 0);

    const later = new Date(now);
    later.setHours(11, 30, 0, 0);
    expect(firstDue('DAILY', '11:30', now)).toBe(later.toISOString());

    const tomorrow = new Date(now);
    tomorrow.setHours(7, 30, 0, 0);
    tomorrow.setDate(tomorrow.getDate() + 1);
    expect(firstDue('DAILY', '07:30', now)).toBe(tomorrow.toISOString());
  });
});

describe('describeCadence', () => {
  it('says a daily schedule with its zone', () => {
    expect(
      describeCadence({
        cadence: 'DAILY',
        everyHours: null,
        at: '07:30',
        timezone: 'Europe/London',
      }),
    ).toBe('Daily at 07:30 (Europe/London)');
  });

  it('says an interval in hours, and a day as a day', () => {
    expect(describeCadence({ cadence: 'INTERVAL', everyHours: 12, at: null, timezone: null })).toBe(
      'Every 12 hours',
    );
    expect(describeCadence({ cadence: 'INTERVAL', everyHours: 24, at: null, timezone: null })).toBe(
      'Every day',
    );
  });
});

describe('describeNext', () => {
  const now = Date.parse('2026-09-19T12:00:00.000Z');

  it('says off, or when', () => {
    expect(describeNext({ enabled: false, nextDueAt: '2026-09-19T13:00:00.000Z' }, now)).toBe(
      'off',
    );
    expect(describeNext({ enabled: true, nextDueAt: null }, now)).toBe('on the worker’s next look');
    expect(describeNext({ enabled: true, nextDueAt: '2026-09-19T11:00:00.000Z' }, now)).toBe(
      'due now, when a worker looks',
    );
    expect(describeNext({ enabled: true, nextDueAt: '2026-09-19T12:20:00.000Z' }, now)).toBe(
      'in 20 min',
    );
    expect(describeNext({ enabled: true, nextDueAt: '2026-09-19T18:00:00.000Z' }, now)).toBe(
      'in 6h',
    );
    expect(describeNext({ enabled: true, nextDueAt: '2026-09-24T12:00:00.000Z' }, now)).toBe(
      'in 5 days',
    );
  });
});

describe('scheduleProblems', () => {
  it('is empty for a sound draft', () => {
    expect(
      scheduleProblems({
        name: 'Mornings',
        cadence: 'DAILY',
        everyHours: 24,
        at: '07:30',
        options: {},
      }),
    ).toEqual([]);
  });

  it('names what is missing, in order', () => {
    expect(
      scheduleProblems({ name: ' ', cadence: 'INTERVAL', everyHours: 2, at: '', options: {} }),
    ).toEqual(['Give it a name.', 'The interval has to be between 6 hours and a week.']);
    expect(
      scheduleProblems({ name: 'x', cadence: 'DAILY', everyHours: 24, at: '7am', options: {} }),
    ).toEqual(['Give a time of day as HH:MM.']);
  });
});
