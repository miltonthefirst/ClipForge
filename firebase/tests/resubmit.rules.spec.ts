import {
  assertFails,
  assertSucceeds,
  type RulesTestEnvironment,
} from '@firebase/rules-unit-testing';
import { doc, setDoc, updateDoc } from 'firebase/firestore';
import { afterAll, beforeAll, beforeEach, describe, it } from 'vitest';

import {
  ALICE,
  BOB,
  createTestEnvironment,
  pendingClip,
  queuedJob,
  userProfile,
} from './helpers.js';

/**
 * Resubmitting: a person sends a FAILED job back to the queue.
 *
 * The same job, the same options, the same stages — only the fields that say
 * "this failed" are reset. Mirrors `lease.resubmit` in the worker, and the
 * write `JobsRepository.resubmit` makes.
 */

let testEnv: RulesTestEnvironment;

beforeAll(async () => {
  testEnv = await createTestEnvironment();
});

afterAll(async () => {
  await testEnv.cleanup();
});

beforeEach(async () => {
  await testEnv.clearFirestore();
  await seed(`users/${ALICE}`, userProfile(ALICE));
  await seed(`users/${BOB}`, userProfile(BOB));
});

const aliceDb = () => testEnv.authenticatedContext(ALICE).firestore();
const bobDb = () => testEnv.authenticatedContext(BOB).firestore();

async function seed(path: string, data: Record<string, unknown>): Promise<void> {
  await testEnv.withSecurityRulesDisabled(async (ctx) => {
    await setDoc(doc(ctx.firestore(), path), data);
  });
}

/** A job as the worker leaves it when it gives up: one stage done, one failed. */
function failedJob(overrides: Record<string, unknown> = {}) {
  return queuedJob(ALICE, {
    status: 'FAILED',
    stages: [
      { name: 'ECHO_ONE', lane: 'CPU', status: 'DONE' },
      { name: 'ECHO_TWO', lane: 'CPU', status: 'FAILED' },
    ],
    attempts: 3,
    error: { type: 'Boom', message: 'it broke', retryable: false },
    startedAt: '2026-09-08T12:01:00.000Z',
    endedAt: '2026-09-08T12:05:00.000Z',
    ...overrides,
  });
}

/** Exactly what the PWA writes. */
function resubmission(overrides: Record<string, unknown> = {}) {
  return {
    status: 'QUEUED',
    attempts: 0,
    error: null,
    startedAt: null,
    endedAt: null,
    resubmittedAt: '2026-09-08T14:00:00.000Z',
    updatedAt: '2026-09-08T14:00:00.000Z',
    ...overrides,
  };
}

describe('resubmitting a failed job', () => {
  it('lets a member resubmit a failed job', async () => {
    await seed('jobs/job-1', failedJob());
    await assertSucceeds(updateDoc(doc(aliceDb(), 'jobs/job-1'), resubmission()));
  });

  it("lets a member resubmit someone else's failed job, as they may cancel it", async () => {
    await seed('jobs/job-1', failedJob());
    await assertSucceeds(updateDoc(doc(bobDb(), 'jobs/job-1'), resubmission()));
  });

  it('allows a job resubmitted before to be resubmitted again', async () => {
    await seed('jobs/job-1', failedJob({ resubmittedAt: '2026-09-08T13:00:00.000Z' }));
    await assertSucceeds(updateDoc(doc(aliceDb(), 'jobs/job-1'), resubmission()));
  });

  for (const status of ['QUEUED', 'RUNNING', 'COMPLETED', 'CANCELLED']) {
    it(`denies resubmitting a ${status} job`, async () => {
      await seed('jobs/job-1', failedJob({ status }));
      await assertFails(updateDoc(doc(aliceDb(), 'jobs/job-1'), resubmission()));
    });
  }

  it('denies resubmitting without resetting the attempts', async () => {
    await seed('jobs/job-1', failedJob());
    await assertFails(
      updateDoc(doc(aliceDb(), 'jobs/job-1'), resubmission({ attempts: 3 })),
    );
  });

  it('denies resubmitting with the old error still attached', async () => {
    await seed('jobs/job-1', failedJob());
    const { error: _dropped, ...rest } = resubmission();
    await assertFails(updateDoc(doc(aliceDb(), 'jobs/job-1'), rest));
  });

  it('denies resubmitting without saying when', async () => {
    await seed('jobs/job-1', failedJob());
    const { resubmittedAt: _dropped, ...rest } = resubmission();
    await assertFails(updateDoc(doc(aliceDb(), 'jobs/job-1'), rest));
  });

  it('denies rewriting stage results under cover of a resubmission', async () => {
    await seed('jobs/job-1', failedJob());
    await assertFails(
      updateDoc(
        doc(aliceDb(), 'jobs/job-1'),
        resubmission({
          stages: [
            { name: 'ECHO_ONE', lane: 'CPU', status: 'DONE' },
            { name: 'ECHO_TWO', lane: 'CPU', status: 'DONE' },
          ],
        }),
      ),
    );
  });

  it('denies changing what the job asks for under cover of a resubmission', async () => {
    await seed('jobs/job-1', failedJob({ submission: 'https://example.com/a' }));
    await assertFails(
      updateDoc(
        doc(aliceDb(), 'jobs/job-1'),
        resubmission({ submission: 'https://example.com/b' }),
      ),
    );
  });

  it('denies reassigning the job to whoever resubmitted it', async () => {
    await seed('jobs/job-1', failedJob());
    await assertFails(updateDoc(doc(bobDb(), 'jobs/job-1'), resubmission({ uid: BOB })));
  });

  it('denies granting itself a worker or a lease', async () => {
    await seed('jobs/job-1', failedJob());
    await assertFails(
      updateDoc(doc(aliceDb(), 'jobs/job-1'), resubmission({ workerId: 'worker-1' })),
    );
    await assertFails(
      updateDoc(
        doc(aliceDb(), 'jobs/job-1'),
        resubmission({ leaseExpiresAt: '2099-01-01T00:00:00.000Z' }),
      ),
    );
  });

  it('denies resubmitting straight to RUNNING', async () => {
    await seed('jobs/job-1', failedJob());
    await assertFails(
      updateDoc(doc(aliceDb(), 'jobs/job-1'), resubmission({ status: 'RUNNING' })),
    );
  });
});

describe('resubmitting a job that names a clip', () => {
  it('lets a failed PUBLISH be resubmitted while its clip is still approved', async () => {
    await seed('clips/clip-1', pendingClip(ALICE, { review: 'APPROVED' }));
    await seed('jobs/job-1', failedJob({ type: 'PUBLISH', clipId: 'clip-1' }));
    await assertSucceeds(updateDoc(doc(aliceDb(), 'jobs/job-1'), resubmission()));
  });

  it('denies resubmitting a PUBLISH whose clip has since been rejected', async () => {
    await seed('clips/clip-1', pendingClip(ALICE, { review: 'REJECTED' }));
    await seed('jobs/job-1', failedJob({ type: 'PUBLISH', clipId: 'clip-1' }));
    await assertFails(updateDoc(doc(aliceDb(), 'jobs/job-1'), resubmission()));
  });

  it('lets a failed REMAKE be resubmitted while its clip exists', async () => {
    await seed('clips/clip-1', pendingClip(ALICE));
    await seed(
      'jobs/job-1',
      failedJob({ type: 'REMAKE', clipId: 'clip-1', maxAttempts: 1, attempts: 1 }),
    );
    await assertSucceeds(updateDoc(doc(aliceDb(), 'jobs/job-1'), resubmission()));
  });

  it('denies resubmitting a REMAKE whose clip has been deleted', async () => {
    await seed('jobs/job-1', failedJob({ type: 'REMAKE', clipId: 'clip-1' }));
    await assertFails(updateDoc(doc(aliceDb(), 'jobs/job-1'), resubmission()));
  });
});
