import { DestroyRef, Injectable, Signal, computed, inject, signal } from '@angular/core';
import type { Unsubscribe } from 'firebase/firestore';

/**
 * One Firestore listener per query, shared, and kept warm after the last reader
 * leaves.
 *
 * ## Why sharing is not the main saving
 *
 * Two components watching the same query used to open two listeners and pay
 * twice for every document. Sharing fixes that, and it is the obvious half.
 *
 * The bigger one is **keeping the listener open across a navigation**. A live
 * `onSnapshot` bills for the documents that change; a freshly attached one
 * bills for the whole result set before it has told you anything. So a reviewer
 * moving Review → Clip → Review pays for the queue twice under a listener that
 * closes on the way out, and once under one that does not — and the second
 * visit renders immediately from the data already in hand rather than showing a
 * spinner while a round trip it has already paid for comes back.
 *
 * ADR-0004 makes this a requirement rather than an optimisation: "`onSnapshot`
 * queries stay narrow. Every delivered document bills a read."
 *
 * ## Why it lets go at all
 *
 * A listener held for ever is a subscription to changes nobody is watching,
 * which on a busy collection is a slow leak of reads and a socket that never
 * closes. Fifteen minutes is long enough to cover the way the app is actually
 * used — leaving a page and coming back, switching tabs, answering the door —
 * and short enough that a session left open overnight is not still paying for
 * a queue nobody has looked at since.
 *
 * ## The key is the query, not the collection
 *
 * `jobs` is not a key; `jobs:RUNNING,QUEUED` is. Two different filters sharing
 * one entry would hand the second caller the first one's rows, which is the one
 * failure mode of a cache like this that looks like working software.
 */

/** How long a listener outlives its last reader. */
const IDLE_MS = 15 * 60 * 1000;

/** What a caller gets: the data, any error, and the way to say it is finished. */
export interface Live<T> {
  readonly value: Signal<T | null>;
  readonly error: Signal<Error | null>;
  /** Idempotent: calling it twice must not free a listener someone else took. */
  readonly release: () => void;
}

interface Entry {
  readonly value: ReturnType<typeof signal<unknown>>;
  readonly error: ReturnType<typeof signal<Error | null>>;
  /** How many callers are holding it. The listener lives while this is above zero. */
  readers: number;
  stop: Unsubscribe | null;
  idle: ReturnType<typeof setTimeout> | null;
}

@Injectable({ providedIn: 'root' })
export class LiveQueries {
  private readonly entries = new Map<string, Entry>();

  constructor() {
    // Nothing should outlive the application, least of all a socket.
    inject(DestroyRef).onDestroy(() => this.closeAll());
  }

  /**
   * Hold a shared listener for `key`, opening one only if nobody else has.
   *
   * `open` is called at most once per live entry and must return the
   * unsubscribe Firestore gave it. It is a callback rather than a query object
   * so this file never imports the query builders — the store owns what the
   * queries are, this owns how long they live.
   */
  watch<T>(key: string, open: (emit: (value: T) => void, fail: (error: Error) => void) => Unsubscribe): Live<T> {
    const entry = this.entries.get(key) ?? this.create<T>(key, open);
    entry.readers += 1;

    // Whoever is arriving now is a reason to stop counting down. The data they
    // are about to read is already here, and it cost nothing to keep.
    if (entry.idle !== null) {
      clearTimeout(entry.idle);
      entry.idle = null;
    }

    let released = false;
    return {
      value: computed(() => entry.value() as T | null),
      error: entry.error.asReadonly(),
      release: () => {
        if (released) return;
        released = true;
        entry.readers -= 1;
        if (entry.readers === 0) this.startIdle(key, entry);
      },
    };
  }

  /**
   * Forget a query outright, listener and data.
   *
   * For the case the idle timer cannot reason about: a write whose result the
   * next reader must not see a stale version of. Rare, and deliberately
   * separate from `release`.
   */
  evict(key: string): void {
    const entry = this.entries.get(key);
    if (!entry) return;
    this.close(entry);
    this.entries.delete(key);
  }

  /** What is held right now. For the storage page, and for tests. */
  snapshot(): { key: string; readers: number; idle: boolean }[] {
    return [...this.entries.entries()].map(([key, entry]) => ({
      key,
      readers: entry.readers,
      idle: entry.idle !== null,
    }));
  }

  private create<T>(
    key: string,
    open: (emit: (value: T) => void, fail: (error: Error) => void) => Unsubscribe,
  ): Entry {
    const entry: Entry = {
      value: signal<unknown>(null),
      error: signal<Error | null>(null),
      readers: 0,
      stop: null,
      idle: null,
    };
    // Registered before `open` runs: Firestore can deliver a cached snapshot
    // synchronously, and an entry that is not in the map yet when that happens
    // would be written to and then replaced by the one stored afterwards.
    this.entries.set(key, entry);
    entry.stop = open(
      (value) => {
        entry.value.set(value);
        // A snapshot after a failure means the failure is over. Leaving it set
        // would keep an error banner over data that is arriving normally.
        if (entry.error() !== null) entry.error.set(null);
      },
      (error) => entry.error.set(error),
    );
    return entry;
  }

  private startIdle(key: string, entry: Entry): void {
    if (entry.idle !== null) clearTimeout(entry.idle);
    entry.idle = setTimeout(() => {
      // Re-read rather than closing over `entry`: it may have been evicted and
      // replaced while the timer was pending, and closing the replacement is
      // exactly the bug this is written to avoid.
      const held = this.entries.get(key);
      if (!held || held.readers > 0) return;
      this.close(held);
      this.entries.delete(key);
    }, IDLE_MS);
  }

  private close(entry: Entry): void {
    if (entry.idle !== null) clearTimeout(entry.idle);
    entry.idle = null;
    entry.stop?.();
    entry.stop = null;
  }

  private closeAll(): void {
    for (const entry of this.entries.values()) this.close(entry);
    this.entries.clear();
  }
}
