import {
  assertFails,
  assertSucceeds,
  type RulesTestEnvironment,
} from '@firebase/rules-unit-testing';
import { doc, setDoc } from 'firebase/firestore';
import { afterAll, beforeAll, beforeEach, describe, it } from 'vitest';

import { ALICE, BOB, createTestEnvironment, queuedJob, userProfile } from './helpers.js';

/**
 * The rules half of the brief: what a CLIP job may ask for.
 *
 * The instructions go into a prompt on the worker and the numbers into the
 * ranking and the render loop, so the shape is checked here the way the
 * music and remake options are. Null is today's behaviour and always allowed.
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

async function seed(path: string, data: Record<string, unknown>): Promise<void> {
  await testEnv.withSecurityRulesDisabled(async (ctx) => {
    await setDoc(doc(ctx.firestore(), path), data);
  });
}

/** A clip job as the Jobs page creates it, with a brief. */
function clipJob(uid: string, options: Record<string, unknown> | null) {
  return queuedJob(uid, {
    id: 'job-clip-1',
    type: 'CLIP',
    submission: 'https://www.youtube.com/watch?v=aaaaaaaaaaa',
    clipOptions: options,
    stages: [
      { name: 'DOWNLOAD', lane: 'CPU', status: 'PENDING' },
      { name: 'TRANSCRIBE', lane: 'GPU', status: 'PENDING' },
      { name: 'ANALYZE', lane: 'GPU', status: 'PENDING' },
      { name: 'RENDER', lane: 'CPU', status: 'PENDING' },
    ],
  });
}

const create = (job: Record<string, unknown>) =>
  setDoc(doc(aliceDb(), 'jobs', job.id as string), job);

describe('what a clip job may ask for', () => {
  it('accepts no brief at all, which is every job before there was one', async () => {
    await assertSucceeds(create(clipJob(ALICE, null)));
  });

  it('accepts a full brief', async () => {
    await assertSucceeds(
      create(
        clipJob(ALICE, {
          instructions: 'the goals and the penalty shout',
          maxClips: 3,
          minDurationSec: 20,
          maxDurationSec: 45,
        }),
      ),
    );
  });

  it('accepts instructions on their own', async () => {
    await assertSucceeds(create(clipJob(ALICE, { instructions: 'only the jokes' })));
  });

  it('refuses a count outside one to twenty', async () => {
    await assertFails(create(clipJob(ALICE, { maxClips: 0 })));
    await assertFails(create(clipJob(ALICE, { maxClips: 1000 })));
  });

  it('refuses a length outside five seconds to three minutes', async () => {
    await assertFails(create(clipJob(ALICE, { minDurationSec: 1 })));
    await assertFails(create(clipJob(ALICE, { maxDurationSec: 600 })));
  });

  it('refuses a range that is upside down', async () => {
    await assertFails(create(clipJob(ALICE, { minDurationSec: 60, maxDurationSec: 30 })));
  });

  it('refuses a brief longer than a thousand characters', async () => {
    await assertFails(create(clipJob(ALICE, { instructions: 'x'.repeat(1001) })));
  });

  it('refuses a field the worker does not read', async () => {
    await assertFails(create(clipJob(ALICE, { instructions: 'x', autoPublish: true })));
  });

  it("refuses a job filed under somebody else's name, brief or no brief", async () => {
    await assertFails(create(clipJob(BOB, { instructions: 'x' })));
  });
});
