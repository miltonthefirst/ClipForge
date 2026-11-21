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
  serverTimestamp,
  setDoc,
  Timestamp,
  updateDoc,
  where,
} from 'firebase/firestore';
import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest';

import {
  ALICE,
  BOB,
  agentReport,
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
      updateDoc(doc(bobDb(), 'jobs/alice-job'), {
        status: 'CANCELLED',
        uid: BOB,
      }),
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

  it('lets any approved member review a clip they did not submit', async () => {
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

  it('denies creating a job owned by someone else', async () => {
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

  it('denies writing a field the clip no longer has', async () => {
    // `rights` was a writable field until it was removed. The allow-list is
    // what makes a removal actually take effect on the wire, rather than
    // leaving a client free to keep writing a field nothing reads.
    await assertFails(
      updateDoc(doc(aliceDb(), 'clips/clip-1'), {
        review: 'APPROVED',
        rights: { basis: 'OWN_CONTENT', attestedBy: ALICE },
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

  it('allows reviewing a clip somebody else submitted', async () => {
    // One queue. Whoever gets to it first reviews it.
    await assertSucceeds(updateDoc(doc(bobDb(), 'clips/clip-1'), { review: 'APPROVED' }));
  });

  it('denies reassigning a clip while reviewing it', async () => {
    await assertFails(updateDoc(doc(bobDb(), 'clips/clip-1'), { review: 'APPROVED', uid: BOB }));
  });

  it('lets a reviewer record why they decided, alongside the decision', async () => {
    // The note never leaves the system — it answers "why did I reject this?"
    // weeks later, and is the human half of the Phase 9 calibration evidence.
    await assertSucceeds(
      updateDoc(doc(aliceDb(), 'clips/clip-1'), {
        review: 'REJECTED',
        reviewedAt: '2026-09-10T12:00:00.000Z',
        reviewNote: 'Hook lands but the payoff is off-camera.',
        title: 'A retitled clip',
      }),
    );
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

  it('denies submitting a source owned by someone else', async () => {
    await assertFails(setDoc(doc(aliceDb(), 'sources/src-1'), source(BOB)));
  });

  it('denies mutating a source once ingested', async () => {
    await seed('sources/src-1', source(ALICE));
    await assertFails(updateDoc(doc(aliceDb(), 'sources/src-1'), { title: 'renamed' }));
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Agents: the one thing a client may say about what runs on the machine.
// ─────────────────────────────────────────────────────────────────────────────

describe('the agent document', () => {
  const WISH = {
    desired: 'RUNNING',
    requestedBy: ALICE,
    requestedAt: '2026-09-12T09:00:00.000Z',
  };

  beforeEach(async () => {
    await seed('agents/tower', agentReport());
  });

  it('lets any approved member start the worker, from anywhere', async () => {
    // The entire point of the collection: a phone can do more than queue work.
    await assertSucceeds(updateDoc(doc(aliceDb(), 'agents/tower'), WISH));
    await assertSucceeds(
      updateDoc(doc(bobDb(), 'agents/tower'), {
        ...WISH,
        desired: 'STOPPED',
        requestedBy: BOB,
      }),
    );
  });

  it('denies anyone who is not approved', async () => {
    await assertFails(updateDoc(doc(anonDb(), 'agents/tower'), WISH));
    await assertFails(getDoc(doc(anonDb(), 'agents/tower')));
  });

  it('denies filing the request under somebody else', async () => {
    // A shared workspace can afford anyone starting the worker. It cannot
    // afford not knowing who did.
    await assertFails(updateDoc(doc(aliceDb(), 'agents/tower'), { ...WISH, requestedBy: BOB }));
  });

  it('denies a wish that is not one of the two things it can be', async () => {
    await assertFails(updateDoc(doc(aliceDb(), 'agents/tower'), { ...WISH, desired: 'PAUSED' }));
    await assertFails(updateDoc(doc(aliceDb(), 'agents/tower'), { ...WISH, desired: 7 }));
  });

  it('requires the request to be dated', async () => {
    // Pressing Start on a worker that is already wanted is how a person clears
    // an agent that has given up. Without a fresh timestamp that press is
    // indistinguishable from the wish simply still being RUNNING.
    const { requestedAt, ...undated } = WISH;
    await assertFails(updateDoc(doc(aliceDb(), 'agents/tower'), undated));
    await assertFails(
      updateDoc(doc(aliceDb(), 'agents/tower'), {
        ...WISH,
        requestedAt: 12345,
      }),
    );
  });

  it('denies writing what the agent reports about itself', async () => {
    // `desired` is a wish. Everything describing what is actually happening
    // belongs to the agent, and a client that could write `state` could claim a
    // worker was running while the machine was off.
    for (const forged of [
      { state: 'RUNNING' },
      { workerPid: 99 },
      { lastExitCode: 0 },
      { log: ['all fine'] },
      { lastSeenAt: '2026-09-12T09:00:00.000Z' },
    ]) {
      await assertFails(updateDoc(doc(aliceDb(), 'agents/tower'), { ...WISH, ...forged }));
    }
  });

  it('denies conjuring a machine that has never run an agent', async () => {
    // "No document" means no agent has ever run there, which is a different
    // problem from "the agent is not reporting right now". A client that could
    // create one would make a phone offer Start for a machine with nothing
    // listening.
    await assertFails(setDoc(doc(aliceDb(), 'agents/laptop'), agentReport({ agentId: 'laptop' })));
    await assertFails(deleteDoc(doc(aliceDb(), 'agents/tower')));
  });

  it('lets a member see every machine, so "which PC?" is answerable', async () => {
    await assertSucceeds(getDoc(doc(bobDb(), 'agents/tower')));
    await assertSucceeds(getDocs(collection(bobDb(), 'agents')));
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// The home wifi route: opening a port on the owner's network is the owner's call.
// ─────────────────────────────────────────────────────────────────────────────

describe('the home wifi route wish', () => {
  const ON = {
    homeRouteDesired: 'ON',
    homeRouteRequestedBy: ALICE,
    homeRouteRequestedAt: '2026-11-15T09:00:00.000Z',
  };

  beforeEach(async () => {
    await seed(`users/${ALICE}`, userProfile(ALICE, { role: 'ADMIN' }));
    await seed('agents/tower', agentReport());
  });

  it('lets an admin switch it on and off', async () => {
    await assertSucceeds(updateDoc(doc(aliceDb(), 'agents/tower'), ON));
    await assertSucceeds(
      updateDoc(doc(aliceDb(), 'agents/tower'), {
        ...ON,
        homeRouteDesired: 'OFF',
        homeRouteRequestedAt: '2026-11-15T09:05:00.000Z',
      }),
    );
  });

  it('requires every press to carry a fresh time', async () => {
    // Like the worker's wish: a press is an event, and one the agent can tell
    // from a stale document only by its time.
    await assertSucceeds(updateDoc(doc(aliceDb(), 'agents/tower'), ON));
    await assertFails(
      updateDoc(doc(aliceDb(), 'agents/tower'), {
        ...ON,
        homeRouteDesired: 'OFF',
      }),
    );
  });

  it('denies an approved member who is not an admin', async () => {
    // Bob may start the worker from his phone; he may not open a port.
    await assertFails(
      updateDoc(doc(bobDb(), 'agents/tower'), {
        ...ON,
        homeRouteRequestedBy: BOB,
      }),
    );
  });

  it('denies an admin whose account is no longer approved', async () => {
    await seed(`users/${ALICE}`, userProfile(ALICE, { role: 'ADMIN', status: 'DISABLED' }));
    await assertFails(updateDoc(doc(aliceDb(), 'agents/tower'), ON));
  });

  it('denies filing it under somebody else, undated, or as anything but ON or OFF', async () => {
    await assertFails(
      updateDoc(doc(aliceDb(), 'agents/tower'), {
        ...ON,
        homeRouteRequestedBy: BOB,
      }),
    );
    const { homeRouteRequestedAt, ...undated } = ON;
    await assertFails(updateDoc(doc(aliceDb(), 'agents/tower'), undated));
    await assertFails(
      updateDoc(doc(aliceDb(), 'agents/tower'), {
        ...ON,
        homeRouteDesired: 'SOMETIMES',
      }),
    );
  });

  it('denies writing where the route is, which only the agent may say', async () => {
    // A phone probes only what the agent advertised. A client that could write
    // `home.url` could point every phone in the house at an address of its
    // choosing.
    const forged = {
      home: { state: 'SERVING', url: 'https://evil.example:8768' },
    };
    await assertFails(updateDoc(doc(aliceDb(), 'agents/tower'), { ...ON, ...forged }));
    await assertFails(updateDoc(doc(aliceDb(), 'agents/tower'), forged));
  });

  it('cannot ride along with a worker wish or a ping', async () => {
    const wish = {
      desired: 'RUNNING',
      requestedBy: ALICE,
      requestedAt: '2026-11-15T09:00:00.000Z',
    };
    await assertFails(updateDoc(doc(aliceDb(), 'agents/tower'), { ...wish, ...ON }));
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Pings: "are you there?", asked rather than beaten.
//
// Neither the agent nor the worker writes on a timer any more. A client that
// wants to know whether the machine is on writes a fresh token and watches for
// it to come back. See docs/adr/0030-status-on-events-and-pings.md.
// ─────────────────────────────────────────────────────────────────────────────

describe('pinging a machine', () => {
  const ping = (uid: string, token = 'token-0123456789') => ({
    pingToken: token,
    pingRequestedAt: serverTimestamp(),
    pingRequestedBy: uid,
  });

  beforeEach(async () => {
    await seed('agents/tower', agentReport());
  });

  it('lets any approved member ask', async () => {
    await assertSucceeds(updateDoc(doc(aliceDb(), 'agents/tower'), ping(ALICE)));
  });

  it('refuses a second ping inside fifteen seconds, whoever sends it', async () => {
    // One per machine, not one per person: the throttle is what keeps a page
    // reloaded in a loop from becoming a write-and-relay loop on the machine.
    // The client refused waits on the token already there instead.
    await assertSucceeds(updateDoc(doc(aliceDb(), 'agents/tower'), ping(ALICE, 'token-first-one')));
    await assertFails(updateDoc(doc(aliceDb(), 'agents/tower'), ping(ALICE, 'token-second-one')));
    await assertFails(updateDoc(doc(bobDb(), 'agents/tower'), ping(BOB, 'token-from-bob-1')));
  });

  it('allows the next ping once fifteen seconds have passed', async () => {
    await seed(
      'agents/tower',
      agentReport({
        pingToken: 'token-old-enough',
        pingRequestedAt: Timestamp.fromMillis(Date.now() - 20_000),
        pingRequestedBy: BOB,
      }),
    );
    await assertSucceeds(updateDoc(doc(aliceDb(), 'agents/tower'), ping(ALICE)));
  });

  it('refuses a ping stamped with a time the client chose', async () => {
    // The throttle is only worth having if the clock it reads is the server's.
    await assertFails(
      updateDoc(doc(aliceDb(), 'agents/tower'), {
        ...ping(ALICE),
        pingRequestedAt: Timestamp.fromMillis(Date.now()),
      }),
    );
    await assertFails(
      updateDoc(doc(aliceDb(), 'agents/tower'), {
        ...ping(ALICE),
        pingRequestedAt: new Date().toISOString(),
      }),
    );
  });

  it('refuses a ping filed under somebody else', async () => {
    await assertFails(updateDoc(doc(aliceDb(), 'agents/tower'), ping(BOB)));
  });

  it('refuses a token that is missing, too short, too long or not a string', async () => {
    const { pingToken, ...untokened } = ping(ALICE);
    await assertFails(updateDoc(doc(aliceDb(), 'agents/tower'), untokened));
    for (const token of ['short', 'x'.repeat(65), 12345678]) {
      await assertFails(
        updateDoc(doc(aliceDb(), 'agents/tower'), {
          ...ping(ALICE),
          pingToken: token,
        }),
      );
    }
  });

  it('refuses a ping that also tries to say something else', async () => {
    // Its own write: a wish rides its own rule, and the answer is the agent's.
    for (const extra of [
      {
        desired: 'RUNNING',
        requestedBy: ALICE,
        requestedAt: '2026-09-12T09:00:00.000Z',
      },
      { answeredPing: 'token-0123456789' },
      { lastSeenAt: '2026-09-12T09:00:00.000Z' },
      { state: 'RUNNING' },
    ]) {
      await assertFails(updateDoc(doc(aliceDb(), 'agents/tower'), { ...ping(ALICE), ...extra }));
    }
  });

  it('does not let a ping overwrite who last asked for a start or a stop', async () => {
    await assertFails(
      updateDoc(doc(aliceDb(), 'agents/tower'), {
        ...ping(ALICE),
        requestedBy: ALICE,
      }),
    );
  });

  it('refuses anyone who is not signed in', async () => {
    await assertFails(updateDoc(doc(anonDb(), 'agents/tower'), ping(ALICE)));
  });

  it('cannot answer its own ping on the worker document either', async () => {
    await seed('workers/tower', heartbeat('local', { workerId: 'tower' }));
    await assertFails(
      updateDoc(doc(aliceDb(), 'workers/tower'), {
        answeredPing: 'token-0123456789',
      }),
    );
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

/**
 * Tidying up.
 *
 * Deleting a record and removing a file are separate acts on purpose: whether a
 * clip belongs in the review queue is decided dozens of times a day and is cheap
 * to get wrong, and whether the 400 MB behind it is still wanted is decided
 * rarely and is expensive to get wrong. These rules govern only the first —
 * nothing in Firestore knows what is on a particular disk, and removing media
 * lives on the worker's loopback API.
 */
describe('deleting records to keep the database tidy', () => {
  beforeEach(async () => {
    await seed('sources/src-1', source(ALICE));
    await seed('clips/clip-del', pendingClip(ALICE, { id: 'clip-del' }));
    await seed('candidates/cand-del', candidate(ALICE, { id: 'cand-del' }));
  });

  it('lets an approved member delete a clip', async () => {
    await assertSucceeds(deleteDoc(doc(aliceDb(), 'clips/clip-del')));
  });

  it('lets another approved member delete it too, because the workspace is shared', async () => {
    await assertSucceeds(deleteDoc(doc(bobDb(), 'clips/clip-del')));
  });

  it('lets a candidate be deleted', async () => {
    await assertSucceeds(deleteDoc(doc(aliceDb(), 'candidates/cand-del')));
  });

  it('still refuses to let a candidate be edited', async () => {
    /**
     * Deleting fabricates nothing. Writing does: a user who could edit a score
     * would corrupt the Phase 9 feedback loop, which is what this collection is
     * read-only for.
     */
    await assertFails(
      setDoc(
        doc(aliceDb(), 'candidates/cand-del'),
        candidate(ALICE, { id: 'cand-del', total: 99 }),
      ),
    );
  });

  it('lets a source be deleted', async () => {
    await assertSucceeds(deleteDoc(doc(aliceDb(), 'sources/src-1')));
  });

  it('refuses a signed-out visitor', async () => {
    await assertFails(
      deleteDoc(doc(testEnv.unauthenticatedContext().firestore(), 'clips/clip-del')),
    );
  });

  it('keeps a publication when its clip goes', async () => {
    /**
     * The record of what was actually posted, and where. An audit trail that
     * vanishes when somebody tidies their queue is not an audit trail — so
     * publications are not deletable at all.
     */
    await seed('clips/clip-del/publications/pub-1', {
      id: 'pub-1',
      uid: ALICE,
      clipId: 'clip-del',
      platform: 'YOUTUBE',
      state: 'PUBLISHED',
      createdAt: '2026-09-01T09:00:00.000Z',
    });
    await assertFails(deleteDoc(doc(aliceDb(), 'clips/clip-del/publications/pub-1')));
  });

  it('still refuses to delete a preference', async () => {
    /**
     * Unchanged and deliberate: a rejection is itself the thing worth
     * remembering, and removing the row would let the same suggestion come back
     * on the next correction.
     */
    await seed('preferences/pref-1', {
      id: 'pref-1',
      uid: ALICE,
      scope: 'SOURCE',
      category: 'FRAMING',
      lesson: 'follow the ball',
      status: 'REJECTED',
      createdAt: '2026-09-01T09:00:00.000Z',
    });
    await assertFails(deleteDoc(doc(aliceDb(), 'preferences/pref-1')));
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Tracks: generated music. The worker makes them; a person keeps them or not.
// ─────────────────────────────────────────────────────────────────────────────

describe('tracks', () => {
  const TRACK = {
    id: 'trk_1',
    title: 'Lo-fi (30 s)',
    prompt: 'warm lo-fi hip hop, instrumental',
    seconds: 30,
    instrumental: true,
    model: 'ACE-Step 1.5',
    licence: 'MIT',
    localPath: 'P:/workspace/music/generated/trk_1.wav',
    status: 'NEW',
    createdAt: '2026-11-15T12:00:00.000Z',
  };
  const KEEP = {
    status: 'KEPT',
    decidedBy: ALICE,
    decidedAt: '2026-11-15T13:00:00.000Z',
  };

  beforeEach(async () => {
    await seed('tracks/trk_1', TRACK);
  });

  it('lets any approved member listen and decide', async () => {
    await assertSucceeds(getDoc(doc(bobDb(), 'tracks/trk_1')));
    await assertSucceeds(updateDoc(doc(aliceDb(), 'tracks/trk_1'), KEEP));
    await assertSucceeds(
      updateDoc(doc(aliceDb(), 'tracks/trk_1'), {
        ...KEEP,
        status: 'DISCARDED',
      }),
    );
  });

  it('denies anyone who is not approved', async () => {
    await assertFails(getDoc(doc(anonDb(), 'tracks/trk_1')));
    await assertFails(updateDoc(doc(anonDb(), 'tracks/trk_1'), KEEP));
  });

  it('denies a decision filed under somebody else, or one that is not a decision', async () => {
    await assertFails(updateDoc(doc(aliceDb(), 'tracks/trk_1'), { ...KEEP, decidedBy: BOB }));
    await assertFails(
      updateDoc(doc(aliceDb(), 'tracks/trk_1'), {
        ...KEEP,
        status: 'PUBLISHED',
      }),
    );
    const { decidedAt, ...undated } = KEEP;
    await assertFails(updateDoc(doc(aliceDb(), 'tracks/trk_1'), undated));
  });

  it('denies pointing a track at another file, or rewriting what it is', async () => {
    // `localPath` is what the home route serves: a client that could change it
    // could ask the PC for any file the route allows.
    await assertFails(
      updateDoc(doc(aliceDb(), 'tracks/trk_1'), {
        ...KEEP,
        localPath: 'P:/workspace/clips/x.mp4',
      }),
    );
    await assertFails(
      updateDoc(doc(aliceDb(), 'tracks/trk_1'), {
        ...KEEP,
        prompt: 'something else',
      }),
    );
  });

  it('denies making a track: the worker owns them', async () => {
    await assertFails(setDoc(doc(aliceDb(), 'tracks/trk_2'), { ...TRACK, id: 'trk_2' }));
  });

  it('lets a member delete a track, and nobody else; the PC bins its file', async () => {
    await assertFails(deleteDoc(doc(anonDb(), 'tracks/trk_1')));
    await assertSucceeds(deleteDoc(doc(aliceDb(), 'tracks/trk_1')));
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Notifications: each person's own devices, switches and list.
// ─────────────────────────────────────────────────────────────────────────────

describe('notifications', () => {
  const DEVICE = {
    id: 'dev-1',
    token: 'fcm-token-abc',
    label: 'Android Chrome',
    createdAt: '2026-11-15T12:00:00.000Z',
    lastSeenAt: '2026-11-15T12:00:00.000Z',
  };

  it('lets a member register, refresh and remove their own device', async () => {
    const own = doc(aliceDb(), `users/${ALICE}/devices/dev-1`);
    await assertSucceeds(setDoc(own, DEVICE));
    await assertSucceeds(updateDoc(own, { lastSeenAt: '2026-11-16T12:00:00.000Z' }));
    await assertSucceeds(getDoc(own));
    await assertSucceeds(deleteDoc(own));
  });

  it("denies touching anyone else's devices, even as an admin", async () => {
    await seed(`users/${BOB}`, userProfile(BOB, { role: 'ADMIN' }));
    await seed(`users/${ALICE}/devices/dev-1`, DEVICE);
    await assertFails(getDoc(doc(bobDb(), `users/${ALICE}/devices/dev-1`)));
    await assertFails(
      setDoc(doc(bobDb(), `users/${ALICE}/devices/dev-2`), {
        ...DEVICE,
        id: 'dev-2',
      }),
    );
  });

  it('denies a device record that carries anything else, or lies about its id', async () => {
    const own = doc(aliceDb(), `users/${ALICE}/devices/dev-1`);
    await assertFails(setDoc(own, { ...DEVICE, admin: true }));
    await assertFails(setDoc(own, { ...DEVICE, id: 'dev-9' }));
  });

  it('denies a pending account registering a phone before it is let in', async () => {
    await seed(`users/${ALICE}`, userProfile(ALICE, { status: 'PENDING' }));
    await assertFails(setDoc(doc(aliceDb(), `users/${ALICE}/devices/dev-1`), DEVICE));
  });

  it('lets a member set their own switches and nobody else', async () => {
    const mine = doc(aliceDb(), `users/${ALICE}/settings/notifications`);
    await assertSucceeds(
      setDoc(mine, {
        kinds: { CLIPS_READY: false },
        updatedAt: '2026-11-15T12:00:00.000Z',
      }),
    );
    await assertFails(setDoc(mine, { kinds: {}, role: 'ADMIN' }));
    await assertFails(
      setDoc(doc(bobDb(), `users/${ALICE}/settings/notifications`), {
        kinds: { JOB_FAILED: false },
      }),
    );
  });

  it('lets a member mark their own notification read, and nothing more', async () => {
    await seed(`users/${ALICE}/notifications/n1`, {
      id: 'n1',
      kind: 'JOB_FAILED',
      title: 'Clipping failed',
      body: '',
      link: '/jobs/j1',
      createdAt: '2026-11-15T12:00:00.000Z',
      readAt: null,
    });
    const own = doc(aliceDb(), `users/${ALICE}/notifications/n1`);
    await assertSucceeds(updateDoc(own, { readAt: '2026-11-15T12:05:00.000Z' }));
    await assertFails(updateDoc(own, { title: 'Everything is fine' }));
    await assertFails(setDoc(doc(aliceDb(), `users/${ALICE}/notifications/n2`), { id: 'n2' }));
    await assertFails(getDoc(doc(bobDb(), `users/${ALICE}/notifications/n1`)));
    await assertSucceeds(deleteDoc(own));
  });

  it('lets a member ask for a test for themselves, once per 30 seconds', async () => {
    await seed('agents/tower', agentReport());
    const agent = doc(aliceDb(), 'agents/tower');
    await assertSucceeds(
      updateDoc(agent, {
        notifyTestRequestedBy: ALICE,
        notifyTestRequestedAt: serverTimestamp(),
      }),
    );
    await assertFails(
      updateDoc(agent, {
        notifyTestRequestedBy: ALICE,
        notifyTestRequestedAt: serverTimestamp(),
      }),
    );
    await assertFails(
      updateDoc(doc(bobDb(), 'agents/tower'), {
        notifyTestRequestedBy: ALICE,
        notifyTestRequestedAt: serverTimestamp(),
      }),
    );
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Logs: admins triage what the PC recorded; nobody else sees it.
// ─────────────────────────────────────────────────────────────────────────────

describe('logGroups', () => {
  const GROUP = {
    fingerprint: '0123456789abcdef',
    app: 'worker',
    level: 'error',
    category: null,
    message: 'stage.failed: ffmpeg exited 1',
    stack: null,
    count: 3,
    firstSeenAt: '2026-11-15T12:00:00.000Z',
    lastSeenAt: '2026-11-15T12:05:00.000Z',
    status: 'unresolved',
    resolvedAt: null,
    resolvedBy: null,
    watched: false,
  };

  beforeEach(async () => {
    await seed(`users/${ALICE}`, userProfile(ALICE, { role: 'ADMIN' }));
    await seed('logGroups/0123456789abcdef', GROUP);
    await seed('logGroups/0123456789abcdef/occurrences/o1', {
      timestamp: '2026-11-15T12:05:00.000Z',
      extra: {},
    });
  });

  it('lets an admin read, resolve, watch and delete', async () => {
    const group = doc(aliceDb(), 'logGroups/0123456789abcdef');
    await assertSucceeds(getDoc(group));
    await assertSucceeds(
      updateDoc(group, {
        status: 'resolved',
        resolvedAt: '2026-11-15T13:00:00.000Z',
        resolvedBy: ALICE,
      }),
    );
    await assertSucceeds(updateDoc(group, { watched: true }));
    await assertSucceeds(deleteDoc(doc(aliceDb(), 'logGroups/0123456789abcdef/occurrences/o1')));
    await assertSucceeds(deleteDoc(group));
  });

  it('keeps members out entirely', async () => {
    await assertFails(getDoc(doc(bobDb(), 'logGroups/0123456789abcdef')));
    await assertFails(getDocs(collection(bobDb(), 'logGroups')));
  });

  it('denies an admin rewriting what happened, or writing an error of their own', async () => {
    const group = doc(aliceDb(), 'logGroups/0123456789abcdef');
    await assertFails(updateDoc(group, { message: 'all fine', count: 0 }));
    await assertFails(updateDoc(group, { status: 'resolved', resolvedBy: BOB }));
    await assertFails(setDoc(doc(aliceDb(), 'logGroups/fedcba9876543210'), GROUP));
    await assertFails(
      setDoc(doc(aliceDb(), 'logGroups/0123456789abcdef/occurrences/o2'), {
        timestamp: 'x',
      }),
    );
  });
});

describe('usage', () => {
  it('lets any member read what the project used, and nobody write it', async () => {
    await seed('usage/current', {
      measuredAt: '2026-11-15T12:00:00.000Z',
      days: [],
    });
    await assertSucceeds(getDoc(doc(bobDb(), 'usage/current')));
    await assertFails(getDoc(doc(anonDb(), 'usage/current')));
    await assertFails(setDoc(doc(aliceDb(), 'usage/current'), { measuredAt: 'x', days: [] }));
  });
});

describe('music video jobs', () => {
  const job = (options: Record<string, unknown>) =>
    queuedJob(ALICE, {
      id: 'mv-1',
      type: 'MUSIC_VIDEO',
      stages: [{ name: 'VISUALIZE', lane: 'CPU', status: 'PENDING' }],
      musicVideoOptions: options,
    });
  const track = (status: string) => ({
    id: 'trk_1',
    title: 'Cinematic (180 s)',
    prompt: 'uplifting cinematic build',
    seconds: 180,
    instrumental: true,
    model: 'ACE-Step 1.5',
    licence: 'MIT',
    localPath: 'P:/x.wav',
    status,
    createdAt: '2026-11-17T00:00:00.000Z',
  });

  it('lets a member make videos of a kept track', async () => {
    await seed('tracks/trk_1', track('KEPT'));
    await assertSucceeds(setDoc(doc(aliceDb(), 'jobs/mv-1'), job({ trackId: 'trk_1' })));
  });

  it('denies a track that was discarded, is undecided, or does not exist', async () => {
    await seed('tracks/trk_1', track('DISCARDED'));
    await seed('tracks/trk_2', track('NEW'));
    await assertFails(setDoc(doc(aliceDb(), 'jobs/mv-1'), job({ trackId: 'trk_1' })));
    await assertFails(setDoc(doc(aliceDb(), 'jobs/mv-1'), job({ trackId: 'trk_2' })));
    await assertFails(setDoc(doc(aliceDb(), 'jobs/mv-1'), job({ trackId: 'nope' })));
  });

  it('denies options the worker cannot make', async () => {
    await seed('tracks/trk_1', track('KEPT'));
    for (const options of [
      { trackId: 'trk_1', full: false, short: false },
      { trackId: 'trk_1', shortSeconds: 90 },
      { trackId: 'trk_1', shortSeconds: 5 },
      { trackId: 'trk_1', upload: true },
    ]) {
      await assertFails(setDoc(doc(aliceDb(), 'jobs/mv-1'), job(options)));
    }
  });
});

describe('song jobs', () => {
  const job = (options: Record<string, unknown>, id = 'song-1') =>
    queuedJob(ALICE, {
      id,
      type: 'GENERATE_MUSIC',
      stages: [
        { name: 'LYRICS', lane: 'GPU', status: 'PENDING' },
        { name: 'SING', lane: 'GPU', status: 'PENDING' },
        { name: 'ARTWORK', lane: 'GPU', status: 'PENDING' },
      ],
      generateMusicOptions: options,
    });
  const style = 'amapiano, log drum, warm piano';

  it('lets a member ask for a song from an idea, notes, a trend, a brief or nothing', async () => {
    await seed('trends/t1', { topic: 'something', uid: ALICE });
    for (const [n, options] of [
      { lyricsFrom: 'IDEA', style, idea: 'a slow Sunday' },
      {
        lyricsFrom: 'NOTES',
        style,
        notes: 'Photosynthesis turns light into sugar.',
        takes: 1,
      },
      {
        lyricsFrom: 'TREND',
        style,
        trendId: 't1',
        vocal: 'FEMALE',
        seconds: 180,
      },
      { lyricsFrom: 'IDEA', style, idea: 'a long one', seconds: null, maxSeconds: 600 },
      {
        lyricsFrom: 'NONE',
        style,
        takes: 3,
        seconds: 30,
        series: 'Lo-fi',
        seed: 7,
      },
      {
        lyricsFrom: 'NOTES',
        style,
        notesFile: `notes/${ALICE}/u1/Week 3.pdf`,
        notesFileName: 'Week 3.pdf',
        focus: 'momentum',
      },
      { lyricsFrom: 'BRIEF', brief: '[Verse 1]\nA pasted song\n\nStyle: amapiano' },
      { lyricsFrom: 'BRIEF', brief: 'x'.repeat(10000), style, takes: 1 },
      { lyricsFrom: 'REFERENCE', referenceUrl: 'https://youtu.be/Msdsdts3NlI?si=iuI21QuXZLHi456j' },
      {
        lyricsFrom: 'REFERENCE',
        referenceUrl: 'https://www.youtube.com/watch?v=Msdsdts3NlI',
        takes: 3,
      },
    ].entries()) {
      await assertSucceeds(setDoc(doc(aliceDb(), `jobs/song-${n}`), job(options, `song-${n}`)));
    }
  });

  it('denies a song with nothing to say, or more than the worker will make', async () => {
    for (const options of [
      { lyricsFrom: 'IDEA', style },
      { lyricsFrom: 'IDEA', style, idea: '' },
      { lyricsFrom: 'NOTES', style },
      { lyricsFrom: 'TREND', style, trendId: 'gone' },
      { lyricsFrom: 'IDEA', style: '', idea: 'x' },
      { lyricsFrom: 'IDEA', idea: 'no sound given' },
      { lyricsFrom: 'BRIEF' },
      { lyricsFrom: 'BRIEF', brief: '' },
      { lyricsFrom: 'BRIEF', brief: 'x'.repeat(10001) },
      { lyricsFrom: 'BRIEF', brief: 'x', style: '' },
      { lyricsFrom: 'REFERENCE' },
      { lyricsFrom: 'REFERENCE', referenceUrl: 'https://example.com/song.mp3' },
      { lyricsFrom: 'REFERENCE', referenceUrl: 'file:///C:/secret.wav' },
      { lyricsFrom: 'REFERENCE', referenceUrl: 'https://youtu.be/' + 'x'.repeat(300) },
      { lyricsFrom: 'IDEA', style, idea: 'x', takes: 4 },
      { lyricsFrom: 'IDEA', style, idea: 'x', seconds: 601 },
      { lyricsFrom: 'IDEA', style, idea: 'x', maxSeconds: 601 },
      { lyricsFrom: 'IDEA', style, idea: 'x', maxSeconds: 30 },
      { lyricsFrom: 'IDEA', style, idea: 'x', vocal: 'ROBOT' },
      { lyricsFrom: 'IDEA', style, idea: 'x', publish: true },
      { lyricsFrom: 'SOMETHING', style },
      { lyricsFrom: 'NOTES', style, notesFile: 'notes/someone-else/u1/x.pdf' },
      { lyricsFrom: 'NOTES', style, notesFile: 'clips/x.mp4' },
      {
        lyricsFrom: 'NOTES',
        style,
        notesFile: `notes/${ALICE}/u1/x.pdf`,
        focus: 'x'.repeat(301),
      },
    ]) {
      await assertFails(setDoc(doc(aliceDb(), 'jobs/song-1'), job(options)));
    }
  });
});
