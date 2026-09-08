import {
  assertFails,
  assertSucceeds,
  type RulesTestEnvironment,
} from '@firebase/rules-unit-testing';
import {
  collection,
  deleteDoc,
  doc,
  getDoc,
  getDocs,
  query,
  setDoc,
  updateDoc,
  where,
} from 'firebase/firestore';
import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest';

import {
  ALICE,
  BOB,
  candidate,
  createTestEnvironment,
  heartbeat,
  pendingClip,
  queuedJob,
  source,
} from './helpers.js';

let testEnv: RulesTestEnvironment;

beforeAll(async () => {
  testEnv = await createTestEnvironment();
});

afterAll(async () => {
  await testEnv.cleanup();
});

beforeEach(async () => {
  await testEnv.clearFirestore();
});

const aliceDb = () => testEnv.authenticatedContext(ALICE).firestore();
const bobDb = () => testEnv.authenticatedContext(BOB).firestore();
const anonDb = () => testEnv.unauthenticatedContext().firestore();

/** Seed as the worker would, bypassing rules exactly as the Admin SDK does. */
async function seed(path: string, data: Record<string, unknown>): Promise<void> {
  await testEnv.withSecurityRulesDisabled(async (ctx) => {
    await setDoc(doc(ctx.firestore(), path), data);
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Phase 1, exit criterion 1: per-user isolation on read, write and list.
// ─────────────────────────────────────────────────────────────────────────────

describe('per-user isolation', () => {
  beforeEach(async () => {
    await seed('jobs/alice-job', queuedJob(ALICE));
  });

  it("lets the owner read their own job", async () => {
    await assertSucceeds(getDoc(doc(aliceDb(), 'jobs/alice-job')));
  });

  it("denies another user reading it", async () => {
    await assertFails(getDoc(doc(bobDb(), 'jobs/alice-job')));
  });

  it("denies another user writing it", async () => {
    await assertFails(updateDoc(doc(bobDb(), 'jobs/alice-job'), { status: 'CANCELLED' }));
  });

  it("denies another user deleting it", async () => {
    await assertFails(deleteDoc(doc(bobDb(), 'jobs/alice-job')));
  });

  it('denies an unfiltered list, which would leak every user’s jobs', async () => {
    await assertFails(getDocs(collection(aliceDb(), 'jobs')));
  });

  it("denies listing another user's jobs by uid", async () => {
    await assertFails(getDocs(query(collection(bobDb(), 'jobs'), where('uid', '==', ALICE))));
  });

  it('allows a list scoped to the caller’s own uid', async () => {
    await assertSucceeds(
      getDocs(query(collection(aliceDb(), 'jobs'), where('uid', '==', ALICE))),
    );
  });

  it('denies an anonymous caller entirely', async () => {
    await assertFails(getDoc(doc(anonDb(), 'jobs/alice-job')));
    await assertFails(setDoc(doc(anonDb(), 'jobs/anon-job'), queuedJob(ALICE)));
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// The client may enqueue work, but may not forge pipeline state.
// ─────────────────────────────────────────────────────────────────────────────

describe('job creation', () => {
  it('allows a well-formed queued job', async () => {
    await assertSucceeds(setDoc(doc(aliceDb(), 'jobs/new-job'), queuedJob(ALICE)));
  });

  it("denies creating a job owned by someone else", async () => {
    await assertFails(setDoc(doc(aliceDb(), 'jobs/new-job'), queuedJob(BOB)));
  });

  it('denies creating a job that is already RUNNING', async () => {
    await assertFails(
      setDoc(doc(aliceDb(), 'jobs/new-job'), queuedJob(ALICE, { status: 'RUNNING' })),
    );
  });

  it('denies creating a job that already claims a worker', async () => {
    await assertFails(
      setDoc(doc(aliceDb(), 'jobs/new-job'), queuedJob(ALICE, { workerId: 'worker-1' })),
    );
  });

  it('denies creating a job that arrives holding a lease', async () => {
    await assertFails(
      setDoc(
        doc(aliceDb(), 'jobs/new-job'),
        queuedJob(ALICE, { leaseExpiresAt: '2099-01-01T00:00:00.000Z' }),
      ),
    );
  });

  it('denies creating a job with attempts already burned', async () => {
    await assertFails(setDoc(doc(aliceDb(), 'jobs/new-job'), queuedJob(ALICE, { attempts: 2 })));
  });
});

describe('job updates', () => {
  beforeEach(async () => {
    await seed('jobs/alice-job', queuedJob(ALICE));
  });

  it('allows the owner to cancel', async () => {
    await assertSucceeds(
      updateDoc(doc(aliceDb(), 'jobs/alice-job'), {
        status: 'CANCELLED',
        updatedAt: '2026-09-08T12:05:00.000Z',
      }),
    );
  });

  it('denies marking a job COMPLETED — only the worker may do that', async () => {
    await assertFails(
      updateDoc(doc(aliceDb(), 'jobs/alice-job'), {
        status: 'COMPLETED',
        updatedAt: '2026-09-08T12:05:00.000Z',
      }),
    );
  });

  it('denies rewriting stage results under cover of a cancellation', async () => {
    await assertFails(
      updateDoc(doc(aliceDb(), 'jobs/alice-job'), {
        status: 'CANCELLED',
        stages: [{ name: 'ECHO_ONE', lane: 'CPU', status: 'DONE' }],
      }),
    );
  });

  it('denies granting itself a lease', async () => {
    await assertFails(
      updateDoc(doc(aliceDb(), 'jobs/alice-job'), {
        status: 'CANCELLED',
        leaseExpiresAt: '2099-01-01T00:00:00.000Z',
      }),
    );
  });

  it('denies cancelling an already-completed job', async () => {
    await seed('jobs/done-job', queuedJob(ALICE, { status: 'COMPLETED' }));
    await assertFails(
      updateDoc(doc(aliceDb(), 'jobs/done-job'), {
        status: 'CANCELLED',
        updatedAt: '2026-09-08T12:05:00.000Z',
      }),
    );
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Worker-owned collections are read-only to the client.
// ─────────────────────────────────────────────────────────────────────────────

describe('worker-owned data is read-only', () => {
  it('lets the owner read the event log but never write it', async () => {
    await seed('jobs/alice-job', queuedJob(ALICE));
    await seed('jobs/alice-job/events/e1', {
      id: 'e1',
      jobId: 'alice-job',
      kind: 'CREATED',
      at: '2026-09-08T12:00:00.000Z',
    });

    await assertSucceeds(getDoc(doc(aliceDb(), 'jobs/alice-job/events/e1')));
    await assertFails(getDoc(doc(bobDb(), 'jobs/alice-job/events/e1')));
    await assertFails(
      setDoc(doc(aliceDb(), 'jobs/alice-job/events/forged'), {
        id: 'forged',
        jobId: 'alice-job',
        kind: 'COMPLETED',
        at: '2026-09-08T12:00:00.000Z',
      }),
    );
  });

  it('lets the owner read candidates but never fabricate a score', async () => {
    await seed('candidates/cand-1', candidate(ALICE));

    await assertSucceeds(getDoc(doc(aliceDb(), 'candidates/cand-1')));
    await assertFails(getDoc(doc(bobDb(), 'candidates/cand-1')));
    await assertFails(updateDoc(doc(aliceDb(), 'candidates/cand-1'), { total: 100 }));
  });

  it('lets the owner read a heartbeat but never write one', async () => {
    await seed('workers/worker-1', heartbeat(ALICE));

    await assertSucceeds(getDoc(doc(aliceDb(), 'workers/worker-1')));
    await assertFails(getDoc(doc(bobDb(), 'workers/worker-1')));
    await assertFails(updateDoc(doc(aliceDb(), 'workers/worker-1'), { status: 'OFFLINE' }));
  });

  it('lets the owner read a transcript but never write one', async () => {
    await seed('sources/src-1', source(ALICE));
    await seed('sources/src-1/transcripts/whisper-v3', {
      sourceId: 'src-1',
      modelVersion: 'whisper-v3',
      segments: [],
      createdAt: '2026-09-08T12:00:00.000Z',
    });

    await assertSucceeds(getDoc(doc(aliceDb(), 'sources/src-1/transcripts/whisper-v3')));
    await assertFails(getDoc(doc(bobDb(), 'sources/src-1/transcripts/whisper-v3')));
    await assertFails(
      setDoc(doc(aliceDb(), 'sources/src-1/transcripts/forged'), {
        sourceId: 'src-1',
        modelVersion: 'forged',
        segments: [],
        createdAt: '2026-09-08T12:00:00.000Z',
      }),
    );
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Review is the one thing the user genuinely owns.
// ─────────────────────────────────────────────────────────────────────────────

describe('clip review', () => {
  beforeEach(async () => {
    await seed('clips/clip-1', pendingClip(ALICE));
  });

  it('allows the owner to approve', async () => {
    await assertSucceeds(
      updateDoc(doc(aliceDb(), 'clips/clip-1'), {
        review: 'APPROVED',
        reviewedAt: '2026-09-08T12:05:00.000Z',
      }),
    );
  });

  it('allows attaching a rights attestation', async () => {
    await assertSucceeds(
      updateDoc(doc(aliceDb(), 'clips/clip-1'), {
        review: 'APPROVED',
        rights: {
          basis: 'OWN_CONTENT',
          attestedBy: ALICE,
          attestedAt: '2026-09-08T12:05:00.000Z',
          note: null,
        },
      }),
    );
  });

  it('denies an invalid review state', async () => {
    await assertFails(updateDoc(doc(aliceDb(), 'clips/clip-1'), { review: 'PUBLISHED' }));
  });

  it('denies repointing storagePath at another render', async () => {
    await assertFails(
      updateDoc(doc(aliceDb(), 'clips/clip-1'), {
        review: 'APPROVED',
        storagePath: 'users/bob-uid/clips/clip-9.mp4',
      }),
    );
  });

  it("denies reviewing another user's clip", async () => {
    await assertFails(updateDoc(doc(bobDb(), 'clips/clip-1'), { review: 'APPROVED' }));
  });

  it('denies creating a clip from the client', async () => {
    await assertFails(setDoc(doc(aliceDb(), 'clips/forged'), pendingClip(ALICE, { id: 'forged' })));
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Sources, and the default-deny fallback.
// ─────────────────────────────────────────────────────────────────────────────

describe('sources', () => {
  it('allows the owner to submit a source', async () => {
    await assertSucceeds(setDoc(doc(aliceDb(), 'sources/src-1'), source(ALICE)));
  });

  it("denies submitting a source owned by someone else", async () => {
    await assertFails(setDoc(doc(aliceDb(), 'sources/src-1'), source(BOB)));
  });

  it('denies mutating a source once ingested', async () => {
    await seed('sources/src-1', source(ALICE));
    await assertFails(updateDoc(doc(aliceDb(), 'sources/src-1'), { title: 'renamed' }));
  });
});

describe('default deny', () => {
  it('refuses a collection the rules never mention', async () => {
    await assertFails(getDoc(doc(aliceDb(), 'secrets/anything')));
    await assertFails(setDoc(doc(aliceDb(), 'secrets/anything'), { uid: ALICE }));
  });
});

// A guard against the rules file silently becoming permissive.
describe('the rules are actually loaded', () => {
  it('rejects an anonymous write to an arbitrary path', async () => {
    await assertFails(setDoc(doc(anonDb(), 'anything/at-all'), { x: 1 }));
    expect(true).toBe(true);
  });
});
