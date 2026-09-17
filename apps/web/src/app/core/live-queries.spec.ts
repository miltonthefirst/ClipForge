import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LiveQueries } from './live-queries';

/**
 * Sharing one Firestore listener, and keeping it warm.
 *
 * What is asserted here is the part that is silent when wrong. A cache that
 * opens one listener too many costs money nobody sees on a bill they do not
 * read; a cache that hands the wrong rows to the wrong query looks exactly like
 * working software until someone notices the Completed tab showing failures.
 */

const IDLE_MS = 15 * 60 * 1000;

/** A fake Firestore listener that records whether it is open. */
function listener() {
  const nothingYet = () => {
    throw new Error('the listener has not been opened yet');
  };
  const state = {
    opens: 0,
    closes: 0,
    emit: nothingYet as (value: unknown) => void,
    fail: nothingYet as (error: Error) => void,
  };
  const open = (emit: (v: unknown) => void, fail: (e: Error) => void) => {
    state.opens += 1;
    state.emit = emit;
    state.fail = fail;
    return () => {
      state.closes += 1;
    };
  };
  return { state, open };
}

describe('LiveQueries', () => {
  let live: LiveQueries;

  beforeEach(() => {
    vi.useFakeTimers();
    TestBed.configureTestingModule({});
    live = TestBed.inject(LiveQueries);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('opens one listener however many readers there are', () => {
    const { state, open } = listener();

    const first = live.watch('jobs:active', open);
    const second = live.watch('jobs:active', open);
    state.emit([{ id: 'a' }]);

    expect(state.opens).toBe(1);
    expect(first.value()).toEqual([{ id: 'a' }]);
    expect(second.value()).toEqual([{ id: 'a' }]);
  });

  it('keeps the listener open while anyone is still reading', () => {
    const { state, open } = listener();
    const first = live.watch('jobs:active', open);
    live.watch('jobs:active', open);

    first.release();
    vi.advanceTimersByTime(IDLE_MS * 2);

    expect(state.closes).toBe(0);
  });

  it('holds the listener for fifteen minutes after the last reader leaves', () => {
    const { state, open } = listener();
    live.watch('jobs:active', open).release();

    vi.advanceTimersByTime(IDLE_MS - 1000);
    expect(state.closes).toBe(0);

    vi.advanceTimersByTime(2000);
    expect(state.closes).toBe(1);
  });

  it('costs nothing to come back within the window', () => {
    // The point of the whole file: leaving a page and returning re-reads the
    // whole result set under a listener that closed on the way out.
    const { state, open } = listener();
    const first = live.watch('jobs:active', open);
    state.emit([{ id: 'a' }]);
    first.release();

    vi.advanceTimersByTime(IDLE_MS / 2);
    const second = live.watch('jobs:active', open);

    expect(state.opens).toBe(1);
    expect(state.closes).toBe(0);
    // And the data is already there, so the page renders rather than spinning.
    expect(second.value()).toEqual([{ id: 'a' }]);
  });

  it('starts the countdown again when the returning reader leaves', () => {
    const { state, open } = listener();
    live.watch('jobs:active', open).release();

    vi.advanceTimersByTime(IDLE_MS / 2);
    const second = live.watch('jobs:active', open);
    vi.advanceTimersByTime(IDLE_MS);
    expect(state.closes).toBe(0);

    second.release();
    vi.advanceTimersByTime(IDLE_MS + 1000);
    expect(state.closes).toBe(1);
  });

  it('opens a fresh listener once the old one has been let go', () => {
    const { state, open } = listener();
    live.watch('jobs:active', open).release();
    vi.advanceTimersByTime(IDLE_MS + 1000);

    live.watch('jobs:active', open);
    expect(state.opens).toBe(2);
  });

  it('never lets one query read another query rows', () => {
    // The failure that looks like working software.
    const active = listener();
    const completed = listener();

    const a = live.watch('jobs:RUNNING,QUEUED', active.open);
    const c = live.watch('jobs:COMPLETED', completed.open);
    active.state.emit([{ id: 'running' }]);
    completed.state.emit([{ id: 'done' }]);

    expect(a.value()).toEqual([{ id: 'running' }]);
    expect(c.value()).toEqual([{ id: 'done' }]);
    expect(active.state.opens).toBe(1);
    expect(completed.state.opens).toBe(1);
  });

  it('treats a second release from the same reader as nothing', () => {
    // Otherwise a component destroyed twice frees a listener another page is
    // using, and that page stops updating with no error anywhere.
    const { state, open } = listener();
    const first = live.watch('shared', open);
    const second = live.watch('shared', open);

    first.release();
    first.release();
    vi.advanceTimersByTime(IDLE_MS + 1000);

    expect(state.closes).toBe(0);
    expect(second.value).toBeDefined();
  });

  it('surfaces a failure and clears it when data comes back', () => {
    const { state, open } = listener();
    const held = live.watch('jobs:active', open);

    state.fail(new Error('missing index'));
    expect(held.error()?.message).toBe('missing index');

    state.emit([{ id: 'a' }]);
    expect(held.error()).toBeNull();
  });

  it('forgets a query outright when asked', () => {
    const { state, open } = listener();
    live.watch('jobs:active', open);

    live.evict('jobs:active');

    expect(state.closes).toBe(1);
    expect(live.snapshot()).toEqual([]);
  });

  it('reports what it is holding', () => {
    const { open } = listener();
    const held = live.watch('jobs:active', open);
    expect(live.snapshot()).toEqual([{ key: 'jobs:active', readers: 1, idle: false }]);

    held.release();
    expect(live.snapshot()).toEqual([{ key: 'jobs:active', readers: 0, idle: true }]);
  });
});
