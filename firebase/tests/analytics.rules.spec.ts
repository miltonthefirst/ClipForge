import {
  assertFails,
  assertSucceeds,
  type RulesTestEnvironment,
} from '@firebase/rules-unit-testing';
import { deleteDoc, doc, getDoc, setDoc, updateDoc } from 'firebase/firestore';
import { afterAll, beforeAll, beforeEach, describe, it } from 'vitest';

import { ALICE, BOB, createTestEnvironment, userProfile } from './helpers.js';

/**
 * Measured performance is evidence, and a client may only read it.
 *
 * Phase 9 is the one part of this project that can say the scoring was wrong.
 * That only means anything if the numbers it reads are the numbers that
 * happened — so `metrics` and `calibrations` have no client write surface at
 * all, in any shape. The worker writes both through the Admin SDK, which
 * bypasses rules entirely; these tests exist to prove there is nothing else
 * that can.
 *
 * The interesting cases are not "can Alice read her own" — they are the four
 * ways a write could sneak in: create, update, delete, and a merge that looks
 * like an update.
 */

let testEnv: RulesTestEnvironment;

beforeAll(async () => {
  testEnv = await createTestEnvironment();
});

afterAll(async () => {
  await testEnv.cleanup();
});

const SNAPSHOT = {
  id: 'pub-1_2026-09-10',
  uid: ALICE,
  clipId: 'clip-1',
  publicationId: 'pub-1',
  platform: 'YOUTUBE',
  externalId: 'vid123',
  channelId: null,
  date: '2026-09-10',
  daysSincePublish: 3,
  views: 412,
  likes: 19,
  comments: 2,
  shares: 5,
  subscribersGained: 1,
  estimatedMinutesWatched: 38.4,
  averageViewDurationSec: 14.2,
  averageViewPercentage: 47.5,
  retention: [
    { elapsedRatio: 0, audienceWatchRatio: 1 },
    { elapsedRatio: 0.5, audienceWatchRatio: 0.61 },
    { elapsedRatio: 1, audienceWatchRatio: 0.24 },
  ],
  partial: false,
  fetchedAt: '2026-09-14T06:00:00.000Z',
};

const REPORT = {
  id: '20260914T060000Z',
  uid: ALICE,
  generatedAt: '2026-09-14T06:00:00.000Z',
  n: 4,
  windowDays: 28,
  correlations: [
    {
      outcome: 'views',
      method: 'SPEARMAN',
      n: 4,
      coefficient: 0.2,
      ciLow: null,
      ciHigh: null,
      interpretation: 'n=4 is below the 20 this report will draw a conclusion from.',
    },
  ],
  cohorts: [],
  baselineWeights: {
    hook: 0.25,
    curiosity: 0.2,
    standalone: 0.2,
    emotion: 0.15,
    pacing: 0.1,
    shareability: 0.1,
  },
  fittedWeights: null,
  underpowered: true,
  notes: ['n=4 supports nothing.'],
};

beforeEach(async () => {
  await testEnv.clearFirestore();
  await seed(`users/${ALICE}`, userProfile(ALICE));
  await seed(`users/${BOB}`, userProfile(BOB));
  await seed('metrics/pub-1_2026-09-10', SNAPSHOT);
  await seed('calibrations/20260914T060000Z', REPORT);
});

const aliceDb = () => testEnv.authenticatedContext(ALICE).firestore();

async function seed(path: string, data: Record<string, unknown>): Promise<void> {
  await testEnv.withSecurityRulesDisabled(async (ctx) => {
    await setDoc(doc(ctx.firestore(), path), data);
  });
}

describe('reading measured performance', () => {
  it('lets an approved member read a snapshot', async () => {
    await assertSucceeds(getDoc(doc(aliceDb(), 'metrics', 'pub-1_2026-09-10')));
  });

  it('lets an approved member read a calibration report', async () => {
    await assertSucceeds(getDoc(doc(aliceDb(), 'calibrations', '20260914T060000Z')));
  });

  it('refuses a signed-out reader', async () => {
    const anon = testEnv.unauthenticatedContext().firestore();
    await assertFails(getDoc(doc(anon, 'metrics', 'pub-1_2026-09-10')));
  });

  it('refuses a signed-in account nobody has approved', async () => {
    await seed(`users/${BOB}`, userProfile(BOB, { status: 'PENDING' }));
    const bobDb = testEnv.authenticatedContext(BOB).firestore();
    await assertFails(getDoc(doc(bobDb, 'metrics', 'pub-1_2026-09-10')));
  });
});

describe('a client cannot write a metric snapshot', () => {
  it('refuses a new one', async () => {
    await assertFails(
      setDoc(doc(aliceDb(), 'metrics', 'pub-2_2026-09-10'), { ...SNAPSHOT, id: 'pub-2_2026-09-10' }),
    );
  });

  it('refuses editing the views on one that exists', async () => {
    await assertFails(updateDoc(doc(aliceDb(), 'metrics', 'pub-1_2026-09-10'), { views: 99_999 }));
  });

  it('refuses flattening the retention curve', async () => {
    await assertFails(updateDoc(doc(aliceDb(), 'metrics', 'pub-1_2026-09-10'), { retention: [] }));
  });

  it('refuses marking a settled day partial so it can be rewritten', async () => {
    await assertFails(updateDoc(doc(aliceDb(), 'metrics', 'pub-1_2026-09-10'), { partial: true }));
  });

  it('refuses a merge that adds a field rather than changing one', async () => {
    await assertFails(
      setDoc(doc(aliceDb(), 'metrics', 'pub-1_2026-09-10'), { note: 'mine now' }, { merge: true }),
    );
  });

  it('refuses deleting one', async () => {
    await assertFails(deleteDoc(doc(aliceDb(), 'metrics', 'pub-1_2026-09-10')));
  });

  it('refuses a write to a snapshot belonging to somebody else', async () => {
    const bobDb = testEnv.authenticatedContext(BOB).firestore();
    await assertFails(updateDoc(doc(bobDb, 'metrics', 'pub-1_2026-09-10'), { views: 0 }));
  });
});

describe('a client cannot write a calibration report', () => {
  it('refuses writing a new one', async () => {
    await assertFails(
      setDoc(doc(aliceDb(), 'calibrations', 'forged'), { ...REPORT, id: 'forged' }),
    );
  });

  it('refuses turning an underpowered report into a confident one', async () => {
    await assertFails(
      updateDoc(doc(aliceDb(), 'calibrations', '20260914T060000Z'), { underpowered: false }),
    );
  });

  it('refuses rewriting a coefficient', async () => {
    await assertFails(
      updateDoc(doc(aliceDb(), 'calibrations', '20260914T060000Z'), {
        correlations: [{ ...REPORT.correlations[0], coefficient: 0.98 }],
      }),
    );
  });

  it('refuses adopting fitted weights by writing them onto the report', async () => {
    await assertFails(
      updateDoc(doc(aliceDb(), 'calibrations', '20260914T060000Z'), {
        fittedWeights: REPORT.baselineWeights,
      }),
    );
  });

  it('refuses deleting a report, so a conclusion cannot be un-drawn', async () => {
    await assertFails(deleteDoc(doc(aliceDb(), 'calibrations', '20260914T060000Z')));
  });
});
