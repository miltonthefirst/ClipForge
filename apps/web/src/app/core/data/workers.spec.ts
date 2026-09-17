import { describe, expect, it } from 'vitest';

import { AGENTS_QUERY, WORKERS_QUERY, byFreshest, wishWrite } from './workers';

/**
 * The parts of the worker domain that are wrong quietly.
 *
 * A panel reading the wrong heartbeat says a machine is running when it is not,
 * which is the exact lie the panel exists to stop telling. A wish that omits
 * `requestedAt` looks like a wish nobody made, and the person who pressed Start
 * on a failed agent gets nothing and no reason. Neither announces itself.
 */

const beat = (id: string, lastSeenAt: string) => ({ id, lastSeenAt });

describe('byFreshest', () => {
  it('puts the most recently seen machine first', () => {
    const order = byFreshest([
      beat('old', '2026-09-17T10:00:00.000Z'),
      beat('new', '2026-09-17T10:05:00.000Z'),
      beat('middle', '2026-09-17T10:02:00.000Z'),
    ]).map((report) => report.id);

    expect(order).toEqual(['new', 'middle', 'old']);
  });

  it('sorts a machine whose timestamp will not parse behind every one that will', () => {
    const order = byFreshest([
      beat('broken', 'not a date'),
      beat('live', '2026-09-17T10:00:00.000Z'),
    ]).map((report) => report.id);

    expect(order).toEqual(['live', 'broken']);
  });

  it('leaves the list it was given alone', () => {
    const reports = [beat('a', '2026-09-17T10:00:00.000Z'), beat('b', '2026-09-17T11:00:00.000Z')];

    byFreshest(reports);

    expect(reports.map((report) => report.id)).toEqual(['a', 'b']);
  });

  it('has nothing to say about an empty collection', () => {
    expect(byFreshest([])).toEqual([]);
  });
});

describe('wishWrite', () => {
  it('stamps the time on a wish that repeats the current one', () => {
    const first = wishWrite('uid-1', 'RUNNING', new Date('2026-09-17T10:00:00.000Z'));
    const again = wishWrite('uid-1', 'RUNNING', new Date('2026-09-17T10:04:00.000Z'));

    // The second press is how a person clears an agent that has given up. A
    // supervisor comparing only `desired` could not tell it from the first.
    expect(again.requestedAt).not.toBe(first.requestedAt);
  });

  it('records who asked, so a worker somebody started is not one that started itself', () => {
    expect(wishWrite('uid-1', 'STOPPED').requestedBy).toBe('uid-1');
  });

  it('writes the time as an ISO string, as the rules require', () => {
    const write = wishWrite('uid-1', 'RUNNING', new Date('2026-09-17T10:00:00.000Z'));

    expect(write.requestedAt).toBe('2026-09-17T10:00:00.000Z');
  });
});

describe('the queries', () => {
  /**
   * Firestore bills a read per delivered document and re-delivers the whole
   * result set on reconnect. These two listen for as long as the panel is open,
   * so a limit going missing is a bill rather than a bug report.
   */
  it('bounds both listeners', () => {
    expect(WORKERS_QUERY.limit).toBe(10);
    expect(AGENTS_QUERY.limit).toBe(4);
  });

  it('reads the collections the agent and the worker write', () => {
    expect(WORKERS_QUERY.collection).toBe('workers');
    expect(AGENTS_QUERY.collection).toBe('agents');
  });
});
