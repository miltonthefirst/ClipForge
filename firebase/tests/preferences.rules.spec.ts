import {
  assertFails,
  assertSucceeds,
  type RulesTestEnvironment,
} from '@firebase/rules-unit-testing';
import { deleteDoc, doc, setDoc, updateDoc } from 'firebase/firestore';
import { afterAll, beforeAll, beforeEach, describe, it } from 'vitest';

import { ALICE, BOB, createTestEnvironment, userProfile } from './helpers.js';

/**
 * The client's only power over a learned preference is the decision.
 *
 * A preference shapes every later clip once accepted, so the write surface is
 * deliberately one field wide: it cannot be created from the PWA (the worker
 * proposes them), its wording cannot be edited after the fact — a rule whose
 * text could be rewritten post-acceptance is a rule nobody agreed to — and it
 * cannot be deleted, because a rejected preference is kept precisely so the
 * same suggestion cannot come back on the next correction.
 */

let testEnv: RulesTestEnvironment;

beforeAll(async () => {
  testEnv = await createTestEnvironment();
});

afterAll(async () => {
  await testEnv.cleanup();
});

const PROPOSED = {
  id: 'pref-1',
  uid: ALICE,
  scope: 'SOURCE',
  sourceId: 'src-1',
  category: 'FRAMING',
  lesson: "this channel's wide shots need the window to follow the ball",
  defaults: null,
  status: 'PROPOSED',
  fromClipId: 'clip-1',
  fromNote: 'follow the ball',
  timesApplied: 0,
  createdAt: '2026-09-13T09:00:00.000Z',
  decidedAt: null,
  decidedBy: null,
};

beforeEach(async () => {
  await testEnv.clearFirestore();
  await seed(`users/${ALICE}`, userProfile(ALICE));
  await seed(`users/${BOB}`, userProfile(BOB));
  await seed('preferences/pref-1', PROPOSED);
});

const aliceDb = () => testEnv.authenticatedContext(ALICE).firestore();

async function seed(path: string, data: Record<string, unknown>): Promise<void> {
  await testEnv.withSecurityRulesDisabled(async (ctx) => {
    await setDoc(doc(ctx.firestore(), path), data);
  });
}

const decide = (status: string, extra: Record<string, unknown> = {}) =>
  updateDoc(doc(aliceDb(), 'preferences', 'pref-1'), {
    status,
    decidedAt: '2026-09-13T10:00:00.000Z',
    decidedBy: ALICE,
    ...extra,
  });

describe('deciding a preference', () => {
  it('lets an approved member keep one', async () => {
    await assertSucceeds(decide('ACCEPTED'));
  });

  it('lets an approved member turn one down', async () => {
    await assertSucceeds(decide('REJECTED'));
  });

  it('refuses a decision that does not say who made it', async () => {
    await assertFails(
      updateDoc(doc(aliceDb(), 'preferences', 'pref-1'), {
        status: 'ACCEPTED',
        decidedAt: '2026-09-13T10:00:00.000Z',
        decidedBy: BOB,
      }),
    );
  });

  it('refuses a status that is not a decision', async () => {
    await assertFails(decide('PROPOSED'));
  });

  it('refuses an edit to the wording', async () => {
    await assertFails(decide('ACCEPTED', { lesson: 'something else entirely' }));
  });

  it('refuses a change to what it would apply', async () => {
    await assertFails(decide('ACCEPTED', { defaults: { framingMode: 'FIT' } }));
  });

  it('refuses a widening of its scope', async () => {
    await assertFails(decide('ACCEPTED', { scope: 'EVERYTHING' }));
  });

  it('refuses a forged application count', async () => {
    await assertFails(decide('ACCEPTED', { timesApplied: 99 }));
  });
});

describe('what the client may never do', () => {
  it('cannot invent a preference', async () => {
    await assertFails(
      setDoc(doc(aliceDb(), 'preferences', 'pref-2'), { ...PROPOSED, id: 'pref-2' }),
    );
  });

  it('cannot delete one it has rejected', async () => {
    /**
     * Kept on purpose. The worker dedupes proposals against every status, so
     * deleting a rejection would let the same suggestion return on the next
     * correction — restarting the loop the rejection ended.
     */
    await assertFails(deleteDoc(doc(aliceDb(), 'preferences', 'pref-1')));
  });

  it('refuses an unapproved account entirely', async () => {
    await seed(`users/${BOB}`, userProfile(BOB, { status: 'PENDING' }));
    await assertFails(
      updateDoc(doc(testEnv.authenticatedContext(BOB).firestore(), 'preferences', 'pref-1'), {
        status: 'ACCEPTED',
        decidedAt: '2026-09-13T10:00:00.000Z',
        decidedBy: BOB,
      }),
    );
  });
});
