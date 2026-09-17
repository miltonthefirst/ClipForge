import { DestroyRef, Injectable, Signal, computed, inject, signal } from '@angular/core';
import {
  collection,
  deleteDoc,
  doc,
  getDoc,
  getDocs,
  limit as limitTo,
  onSnapshot,
  orderBy as orderByField,
  query,
  setDoc,
  updateDoc,
  where as whereField,
  writeBatch,
  type DocumentData,
  type Query,
  type Unsubscribe,
} from 'firebase/firestore';

import { fromDocument } from '../documents';
import { FirebaseService } from '../firebase';
import { cacheKey, type QuerySpec } from './spec';

/**
 * The one door to Firestore.
 *
 * Every `firebase/firestore` import in the application is in this file, and a
 * lint rule keeps it that way. That is worth a layer on its own for three
 * reasons, none of them tidiness:
 *
 * **Reads cost money and the policy belongs in one place.** Every delivered
 * document bills a read, and a listener re-delivers everything when it is
 * attached (docs/adr/0004). Sharing listeners, holding them warm across a
 * navigation, bounding queries — those are one decision made once, not
 * forty-one decisions made in a service that also knows what a publication is.
 *
 * **Documents are not models.** The worker writes timestamps through the Admin
 * SDK and the PWA writes ISO strings, so the same field arrives as either
 * depending on who touched it last. `fromDocument` reconciles that, and it has
 * to be impossible to forget — which it is, if the only way to read is through
 * here.
 *
 * **A described query can be its own cache key.** See `spec.ts`.
 *
 * ## The three states
 *
 * `data` starts null and stays null until something arrives, which used to mean
 * the same thing as "arrived, and there is nothing" — the conflation that had
 * the Jobs page showing "Loading the queue…" for ever when a missing index made
 * the query fail. `loading` and `error` are separate answers here, and a page
 * that renders all three is a page that cannot lie about which one it is in.
 */

/** How long a listener outlives its last reader. */
const IDLE_MS = 15 * 60 * 1000;

export interface Live<T> {
  readonly data: Signal<T | null>;
  readonly loading: Signal<boolean>;
  readonly error: Signal<Error | null>;
  /** Idempotent: a second call must not free a listener someone else took. */
  readonly release: () => void;
}

interface Entry {
  readonly data: ReturnType<typeof signal<unknown>>;
  readonly loading: ReturnType<typeof signal<boolean>>;
  readonly error: ReturnType<typeof signal<Error | null>>;
  readers: number;
  stop: Unsubscribe | null;
  idle: ReturnType<typeof setTimeout> | null;
}

@Injectable({ providedIn: 'root' })
export class FirestoreGateway {
  private readonly firebase = inject(FirebaseService);
  private readonly entries = new Map<string, Entry>();

  constructor() {
    inject(DestroyRef).onDestroy(() => {
      for (const entry of this.entries.values()) this.close(entry);
      this.entries.clear();
    });
  }

  // ── Reading, live ────────────────────────────────────────────────────────

  /** A live list. Shared with anyone else asking the same question. */
  live<T>(spec: QuerySpec): Live<T[]> {
    return this.hold<T[]>(cacheKey(spec), (emit, fail) =>
      onSnapshot(
        this.build(spec),
        (snapshot) => emit(snapshot.docs.map((d) => fromDocument<T>(d.data()))),
        fail,
      ),
    );
  }

  /** A live document. `null` data after loading means it does not exist. */
  liveDoc<T>(path: string, id: string): Live<T> {
    return this.hold<T>(`doc:${path}/${id}`, (emit, fail) =>
      onSnapshot(
        doc(this.firebase.db, path, id),
        (snapshot) => emit(snapshot.exists() ? fromDocument<T>(snapshot.data()) : null),
        fail,
      ),
    );
  }

  // ── Reading, once ────────────────────────────────────────────────────────

  async once<T>(spec: QuerySpec): Promise<T[]> {
    const snapshot = await getDocs(this.build(spec));
    return snapshot.docs.map((d) => fromDocument<T>(d.data()));
  }

  async onceDoc<T>(path: string, id: string): Promise<T | null> {
    const snapshot = await getDoc(doc(this.firebase.db, path, id));
    return snapshot.exists() ? fromDocument<T>(snapshot.data()) : null;
  }

  /**
   * How many documents match, without reading them.
   *
   * An aggregation costs one read rather than one per document, which is the
   * whole reason a count is not `(await once(spec)).length`.
   */
  async count(spec: QuerySpec): Promise<number> {
    const { getCountFromServer } = await import('firebase/firestore');
    const snapshot = await getCountFromServer(this.build(spec));
    return snapshot.data().count;
  }

  // ── Writing ──────────────────────────────────────────────────────────────

  /**
   * An id for a document that does not exist yet.
   *
   * Needed because a job carries its own id as a *field* — the worker reads
   * `job.id`, not the document key — so the id has to exist before the write
   * rather than being returned by it.
   *
   * Firestore's own generator rather than a hand-rolled one. Two repositories
   * had each written their own `crypto.getRandomValues` version, with careful
   * reasoning about secure contexts and about folding 64 random values onto a
   * 62-character alphabet making the first two letters twice as likely. Both
   * were right and neither was necessary: `doc()` on a collection mints an id
   * without touching the network, and it is the same generator every other
   * document in the database was named by.
   */
  newId(path: string): string {
    return doc(collection(this.firebase.db, path)).id;
  }

  async create(path: string, data: DocumentData, id?: string): Promise<string> {
    const reference = id
      ? doc(this.firebase.db, path, id)
      : doc(collection(this.firebase.db, path));
    await setDoc(reference, data);
    return reference.id;
  }

  async update(path: string, id: string, changes: DocumentData): Promise<void> {
    await updateDoc(doc(this.firebase.db, path, id), changes);
  }

  async merge(path: string, id: string, changes: DocumentData): Promise<void> {
    await setDoc(doc(this.firebase.db, path, id), changes, { merge: true });
  }

  async remove(path: string, id: string): Promise<void> {
    await deleteDoc(doc(this.firebase.db, path, id));
  }

  /**
   * Several writes, atomically.
   *
   * Firestore caps a batch at 500, and the caller is left to page rather than
   * having it hidden here: a caller that does not know it is making 900 writes
   * is a caller that will one day make 900,000.
   */
  async writeAll(
    writes: readonly { path: string; id: string; data: DocumentData; op: 'set' | 'update' }[],
  ): Promise<void> {
    if (writes.length === 0) return;
    const batch = writeBatch(this.firebase.db);
    for (const write of writes) {
      const reference = doc(this.firebase.db, write.path, write.id);
      if (write.op === 'update') batch.update(reference, write.data);
      else batch.set(reference, write.data);
    }
    await batch.commit();
  }

  // ── Cache administration ─────────────────────────────────────────────────

  /** Forget a query outright, for a write whose result must not be read stale. */
  evict(spec: QuerySpec): void {
    const key = cacheKey(spec);
    const entry = this.entries.get(key);
    if (!entry) return;
    this.close(entry);
    this.entries.delete(key);
  }

  /** What is held right now. For diagnostics, and for tests. */
  held(): { key: string; readers: number; idle: boolean }[] {
    return [...this.entries.entries()].map(([key, entry]) => ({
      key,
      readers: entry.readers,
      idle: entry.idle !== null,
    }));
  }

  // ── The shared-listener machinery ────────────────────────────────────────

  private build(spec: QuerySpec): Query {
    const constraints = [
      ...(spec.where ?? []).map(([field, op, value]) => whereField(field, op, value)),
      ...(spec.orderBy ?? []).map(([field, dir]) => orderByField(field, dir ?? 'asc')),
      ...(spec.limit === undefined ? [] : [limitTo(spec.limit)]),
    ];
    return query(collection(this.firebase.db, spec.collection), ...constraints);
  }

  private hold<T>(
    key: string,
    open: (emit: (value: T | null) => void, fail: (error: Error) => void) => Unsubscribe,
  ): Live<T> {
    const entry = this.entries.get(key) ?? this.create$(key, open);
    entry.readers += 1;

    // Someone arriving is a reason to stop counting down. What they are about
    // to read is already here and cost nothing to keep.
    if (entry.idle !== null) {
      clearTimeout(entry.idle);
      entry.idle = null;
    }

    let released = false;
    return {
      data: computed(() => entry.data() as T | null),
      loading: entry.loading.asReadonly(),
      error: entry.error.asReadonly(),
      release: () => {
        if (released) return;
        released = true;
        entry.readers -= 1;
        if (entry.readers === 0) this.startIdle(key, entry);
      },
    };
  }

  private create$<T>(
    key: string,
    open: (emit: (value: T | null) => void, fail: (error: Error) => void) => Unsubscribe,
  ): Entry {
    const entry: Entry = {
      data: signal<unknown>(null),
      loading: signal(true),
      error: signal<Error | null>(null),
      readers: 0,
      stop: null,
      idle: null,
    };
    // Registered before `open` runs: Firestore can deliver a cached snapshot
    // synchronously, and an entry not yet in the map when that happens would be
    // written to and then replaced by the one stored afterwards.
    this.entries.set(key, entry);
    entry.stop = open(
      (value) => {
        entry.data.set(value);
        entry.loading.set(false);
        // A snapshot after a failure means the failure is over; leaving the
        // error set would keep a banner over data that is arriving normally.
        if (entry.error() !== null) entry.error.set(null);
      },
      (error) => {
        entry.error.set(error);
        // Not loading any more. It did not work, which is a different answer
        // from "not yet" and the page has to be able to tell them apart.
        entry.loading.set(false);
      },
    );
    return entry;
  }

  private startIdle(key: string, entry: Entry): void {
    if (entry.idle !== null) clearTimeout(entry.idle);
    entry.idle = setTimeout(() => {
      // Re-read rather than closing over `entry`: it may have been evicted and
      // replaced while the timer was pending, and closing the replacement is
      // the bug this is written to avoid.
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
}
