import {
  assertFails,
  assertSucceeds,
  type RulesTestEnvironment,
} from '@firebase/rules-unit-testing';
import { doc, getDoc, getDocs, collection, setDoc, updateDoc } from 'firebase/firestore';
import { afterAll, beforeAll, beforeEach, describe, it } from 'vitest';

import {
  ALICE,
  BOB,
  createTestEnvironment,
  heartbeat,
  queuedJob,
  pendingClip,
  userProfile,
} from './helpers.js';

/**
 * Registration is open. Access is not.
 *
 * Anyone with the URL can create an account; what that account can *see* is
 * nothing until an admin approves it. These assert both halves — that a pending
 * account is inert, and that it cannot make itself otherwise.
 *
 * The second half is the one worth the most care. A sign-up that could choose
 * its own role or status would make the entire gate decorative, and it would
 * look exactly like a working one right up until someone tried.
 */

const CAROL = 'carol-uid';

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

const db = (uid: string) => testEnv.authenticatedContext(uid).firestore();
const anonDb = () => testEnv.unauthenticatedContext().firestore();

async function seed(path: string, data: Record<string, unknown>): Promise<void> {
  await testEnv.withSecurityRulesDisabled(async (ctx) => {
    await setDoc(doc(ctx.firestore(), path), data);
  });
}

describe('an account with no profile at all', () => {
  it('cannot read data even though it is signed in', async () => {
    // Authentication is not authorisation. This is the case that would slip
    // through if the rules checked `signedIn()` and stopped there.
    await seed('jobs/job-1', queuedJob(ALICE));
    await assertFails(getDoc(doc(db(ALICE), 'jobs/job-1')));
  });

  it('cannot create work', async () => {
    await assertFails(setDoc(doc(db(ALICE), 'jobs/job-1'), queuedJob(ALICE)));
  });
});

describe('a pending account', () => {
  beforeEach(async () => {
    await seed(`users/${ALICE}`, userProfile(ALICE, { status: 'PENDING', decidedAt: null, decidedBy: null }));
  });

  it('may read its own profile, so it can be told it is pending', async () => {
    await assertSucceeds(getDoc(doc(db(ALICE), `users/${ALICE}`)));
  });

  it('cannot read its own data yet', async () => {
    await seed('clips/clip-1', pendingClip(ALICE));
    await assertFails(getDoc(doc(db(ALICE), 'clips/clip-1')));
  });

  it('cannot enqueue a job', async () => {
    await assertFails(setDoc(doc(db(ALICE), 'jobs/job-1'), queuedJob(ALICE)));
  });

  it('cannot see whether a worker is running', async () => {
    // Worker heartbeats are readable by *any* approved member rather than by an
    // owner, which makes it worth stating that "any" still stops at the gate.
    await seed('workers/worker-1', heartbeat('local'));
    await assertFails(getDoc(doc(db(ALICE), 'workers/worker-1')));
  });

  it('cannot approve itself', async () => {
    await assertFails(
      updateDoc(doc(db(ALICE), `users/${ALICE}`), {
        status: 'APPROVED',
        decidedAt: new Date().toISOString(),
        decidedBy: ALICE,
      }),
    );
  });

  it('cannot promote itself to admin', async () => {
    await assertFails(updateDoc(doc(db(ALICE), `users/${ALICE}`), { role: 'ADMIN' }));
  });
});

describe('a disabled account', () => {
  it('loses access it previously had', async () => {
    await seed(`users/${ALICE}`, userProfile(ALICE, { status: 'DISABLED' }));
    await seed('clips/clip-1', pendingClip(ALICE));
    await assertFails(getDoc(doc(db(ALICE), 'clips/clip-1')));
  });
});

describe('registering', () => {
  it('lets a signed-in user create their own pending profile', async () => {
    await assertSucceeds(
      setDoc(doc(db(CAROL), `users/${CAROL}`), userProfile(CAROL, { status: 'PENDING', decidedAt: null, decidedBy: null })),
    );
  });

  it('refuses a profile that admits itself', async () => {
    await assertFails(
      setDoc(doc(db(CAROL), `users/${CAROL}`), userProfile(CAROL, { status: 'APPROVED' })),
    );
  });

  it('refuses a profile that makes itself an admin', async () => {
    await assertFails(
      setDoc(
        doc(db(CAROL), `users/${CAROL}`),
        userProfile(CAROL, { role: 'ADMIN', status: 'PENDING', decidedAt: null, decidedBy: null }),
      ),
    );
  });

  it('refuses a profile created for somebody else', async () => {
    await assertFails(
      setDoc(doc(db(CAROL), `users/${ALICE}`), userProfile(ALICE, { status: 'PENDING', decidedAt: null, decidedBy: null })),
    );
  });

  it('refuses an anonymous visitor', async () => {
    await assertFails(setDoc(doc(anonDb(), `users/${CAROL}`), userProfile(CAROL)));
  });
});

describe('an approved member', () => {
  beforeEach(async () => {
    await seed(`users/${ALICE}`, userProfile(ALICE));
    await seed(`users/${BOB}`, userProfile(BOB));
  });

  it('can read its own data again', async () => {
    await seed('clips/clip-1', pendingClip(ALICE));
    await assertSucceeds(getDoc(doc(db(ALICE), 'clips/clip-1')));
  });

  it('may change its display name but not its role', async () => {
    await assertSucceeds(updateDoc(doc(db(ALICE), `users/${ALICE}`), { displayName: 'Alice' }));
    await assertFails(updateDoc(doc(db(ALICE), `users/${ALICE}`), { role: 'ADMIN' }));
  });

  it('cannot read anyone else’s profile', async () => {
    await assertFails(getDoc(doc(db(ALICE), `users/${BOB}`)));
  });

  it('cannot list the people page', async () => {
    await assertFails(getDocs(collection(db(ALICE), 'users')));
  });

  it('cannot approve anyone', async () => {
    await seed(`users/${CAROL}`, userProfile(CAROL, { status: 'PENDING' }));
    await assertFails(
      updateDoc(doc(db(ALICE), `users/${CAROL}`), {
        status: 'APPROVED',
        decidedAt: new Date().toISOString(),
        decidedBy: ALICE,
      }),
    );
  });
});

describe('an admin', () => {
  beforeEach(async () => {
    await seed(`users/${ALICE}`, userProfile(ALICE, { role: 'ADMIN' }));
    await seed(`users/${CAROL}`, userProfile(CAROL, { status: 'PENDING', decidedAt: null, decidedBy: null }));
  });

  it('can list everyone', async () => {
    await assertSucceeds(getDocs(collection(db(ALICE), 'users')));
  });

  it('can read another person’s profile', async () => {
    await assertSucceeds(getDoc(doc(db(ALICE), `users/${CAROL}`)));
  });

  it('can approve a pending account', async () => {
    await assertSucceeds(
      updateDoc(doc(db(ALICE), `users/${CAROL}`), {
        status: 'APPROVED',
        decidedAt: new Date().toISOString(),
        decidedBy: ALICE,
      }),
    );
  });

  it('cannot attribute the decision to someone else', async () => {
    // The audit trail is only worth keeping if its author is the person who
    // actually acted.
    await assertFails(
      updateDoc(doc(db(ALICE), `users/${CAROL}`), {
        status: 'APPROVED',
        decidedAt: new Date().toISOString(),
        decidedBy: BOB,
      }),
    );
  });

  it('cannot change its own role or status', async () => {
    // What stops the last admin locking everybody out — including themselves —
    // with a single mistaken tap.
    await assertFails(
      updateDoc(doc(db(ALICE), `users/${ALICE}`), {
        status: 'DISABLED',
        decidedAt: new Date().toISOString(),
        decidedBy: ALICE,
      }),
    );
  });

  it('reads the shared library like any other approved member', async () => {
    // Being an admin adds nothing here: the library is shared with everyone
    // approved, and administering access is a separate power from having it.
    await seed(`users/${BOB}`, userProfile(BOB));
    await seed('clips/bob-clip', pendingClip(BOB));
    await assertSucceeds(getDoc(doc(db(ALICE), 'clips/bob-clip')));
  });
});
