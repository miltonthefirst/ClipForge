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
  userProfile,
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
  // Approval gating means an account with no profile row can read nothing at
  // all. Both test users are approved members unless a test says otherwise;
  // the gate itself is exercised in access.rules.spec.ts.
  await seed(`users/${ALICE}`, userProfile(ALICE));
  await seed(`users/${BOB}`, userProfile(BOB));
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
// One shared workspace.
//
// This file used to assert the opposite — per-user isolation on read, write and
// list — and that turned out to describe something nobody wanted: two private
// workspaces that happened to share a worker. A job submitted from a phone was
// invisible on the desktop beside it. Access is decided by approval now, and
// `uid` records who submitted the work rather than who may see it.
//
// What these still assert, and what the change did NOT loosen: the gate itself
// (an unapproved account reads nothing), and the client's inability to forge
// pipeline state or reassign work to someone else.
// ─────────────────────────────────────────────────────────────────────────────

describe('a shared workspace', () => {
  beforeEach(async () => {
    await seed('jobs/alice-job', queuedJob(ALICE));
  });

  it('lets any approved member read any job', async () => {
    await assertSucceeds(getDoc(doc(aliceDb(), 'jobs/alice-job')));
    await assertSucceeds(getDoc(doc(bobDb(), 'jobs/alice-job')));
  });

  it('lets an approved member list the whole queue', async () => {
    // The listen the Jobs page actually makes. Under the old rules this was
    // denied, which is why the app filtered by uid and showed half the system.
    await assertSucceeds(getDocs(collection(bobDb(), 'jobs')));
  });

  it("lets a member cancel someone else's job", async () => {
    await assertSucceeds(
      updateDoc(doc(bobDb(), 'jobs/alice-job'), {
        status: 'CANCELLED',
        updatedAt: '2026-09-10T12:00:00.000Z',
      }),
    );
  });

  it('does not let cancelling a job reassign who submitted it', async () => {
    // `uid` is outside the writable field set, so a shared workspace still
    // cannot be used to quietly take credit for someone else's work.
    await assertFails(
      updateDoc(doc(bobDb(), 'jobs/alice-job'), { status: 'CANCELLED', uid: BOB }),
    );
  });

  it('still refuses to let a member forge progress on any job', async () => {
    await assertFails(updateDoc(doc(bobDb(), 'jobs/alice-job'), { status: 'COMPLETED' }));
    await assertFails(updateDoc(doc(aliceDb(), 'jobs/alice-job'), { status: 'COMPLETED' }));
  });

  it('lets any approved member read the clips and candidates', async () => {
    await seed('clips/clip-1', pendingClip(ALICE));
    await seed('candidates/cand-1', candidate(ALICE));

    await assertSucceeds(getDoc(doc(bobDb(), 'clips/clip-1')));
    await assertSucceeds(getDoc(doc(bobDb(), 'candidates/cand-1')));
  });

  it("lets any approved member review a clip they did not submit", async () => {
    await seed('clips/clip-1', pendingClip(ALICE));
    await assertSucceeds(
      updateDoc(doc(bobDb(), 'clips/clip-1'), {
        review: 'APPROVED',
        reviewedAt: '2026-09-10T12:00:00.000Z',
      }),
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
  it('lets any member read the event log but nobody write it', async () => {
    await seed('jobs/alice-job', queuedJob(ALICE));
    await seed('jobs/alice-job/events/e1', {
      id: 'e1',
      jobId: 'alice-job',
      kind: 'CREATED',
      at: '2026-09-08T12:00:00.000Z',
    });

    await assertSucceeds(getDoc(doc(aliceDb(), 'jobs/alice-job/events/e1')));
    await assertSucceeds(getDoc(doc(bobDb(), 'jobs/alice-job/events/e1')));
    await assertFails(
      setDoc(doc(aliceDb(), 'jobs/alice-job/events/forged'), {
        id: 'forged',
        jobId: 'alice-job',
        kind: 'COMPLETED',
        at: '2026-09-08T12:00:00.000Z',
      }),
    );
  });

  it('lets any member read candidates but nobody fabricate a score', async () => {
    await seed('candidates/cand-1', candidate(ALICE));

    await assertSucceeds(getDoc(doc(aliceDb(), 'candidates/cand-1')));
    await assertSucceeds(getDoc(doc(bobDb(), 'candidates/cand-1')));
    await assertFails(updateDoc(doc(aliceDb(), 'candidates/cand-1'), { total: 100 }));
  });

  it('lets any approved member read a heartbeat, but nobody write one', async () => {
    // Deliberately *not* isolated by uid, unlike everything else in this file.
    // A worker is one shared machine serving everyone's queue, so "is anything
    // running?" has the same answer for every member — and the worker announces
    // itself with a placeholder uid rather than a Firebase account, so an
    // ownership rule here matched nobody and left the heartbeat unreadable.
    await seed('workers/worker-1', heartbeat('local'));

    await assertSucceeds(getDoc(doc(aliceDb(), 'workers/worker-1')));
    await assertSucceeds(getDoc(doc(bobDb(), 'workers/worker-1')));
    await assertSucceeds(getDocs(collection(bobDb(), 'workers')));
    await assertFails(updateDoc(doc(aliceDb(), 'workers/worker-1'), { status: 'OFFLINE' }));
    await assertFails(getDoc(doc(anonDb(), 'workers/worker-1')));
  });

  it('lets any member read a transcript but nobody write one', async () => {
    await seed('sources/src-1', source(ALICE));
    await seed('sources/src-1/transcripts/whisper-v3', {
      sourceId: 'src-1',
      modelVersion: 'whisper-v3',
      segments: [],
      createdAt: '2026-09-08T12:00:00.000Z',
    });

    await assertSucceeds(getDoc(doc(aliceDb(), 'sources/src-1/transcripts/whisper-v3')));
    await assertSucceeds(getDoc(doc(bobDb(), 'sources/src-1/transcripts/whisper-v3')));
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

  it("allows reviewing a clip somebody else submitted", async () => {
    // One queue. Whoever gets to it first reviews it.
    await assertSucceeds(updateDoc(doc(bobDb(), 'clips/clip-1'), { review: 'APPROVED' }));
  });

  it('denies reassigning a clip while reviewing it', async () => {
    await assertFails(updateDoc(doc(bobDb(), 'clips/clip-1'), { review: 'APPROVED', uid: BOB }));
  });

  it('denies creating a clip from the client', async () => {
    await assertFails(setDoc(doc(aliceDb(), 'clips/forged'), pendingClip(ALICE, { id: 'forged' })));
  });

  it('denies repointing a clip at a file on the worker', async () => {
    await assertFails(
      updateDoc(doc(aliceDb(), 'clips/clip-1'), {
        review: 'APPROVED',
        localPath: 'C:/Windows/System32/config/SAM',
      }),
    );
  });

  it('denies inventing a playbackUrl', async () => {
    await assertFails(
      updateDoc(doc(aliceDb(), 'clips/clip-1'), {
        review: 'APPROVED',
        playbackUrl: 'https://example.com/not-mine.mp4',
      }),
    );
  });
});

// On the free tier the poster is the only part of a clip a phone can see, so it
// is read-often — but it is produced by the render stage, never by a client.
describe('clip previews', () => {
  beforeEach(async () => {
    await seed('clips/clip-1', pendingClip(ALICE));
    await seed('clips/clip-1/preview/poster', {
      clipId: 'clip-1',
      posterBase64: 'AAAA',
      filmstripBase64: null,
      widthPx: 1080,
      heightPx: 1920,
      createdAt: '2026-09-08T12:00:00.000Z',
    });
  });

  it('lets the owner read the poster', async () => {
    await assertSucceeds(getDoc(doc(aliceDb(), 'clips/clip-1/preview/poster')));
  });

  it('lets another member read it — on the free tier the poster is the review', async () => {
    await assertSucceeds(getDoc(doc(bobDb(), 'clips/clip-1/preview/poster')));
  });

  it('denies the client writing one', async () => {
    await assertFails(
      setDoc(doc(aliceDb(), 'clips/clip-1/preview/forged'), {
        clipId: 'clip-1',
        posterBase64: 'BBBB',
        widthPx: 1,
        heightPx: 1,
        createdAt: '2026-09-08T12:00:00.000Z',
      }),
    );
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
