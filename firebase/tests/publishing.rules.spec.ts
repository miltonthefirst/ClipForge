import {
  assertFails,
  assertSucceeds,
  type RulesTestEnvironment,
} from '@firebase/rules-unit-testing';
import { doc, getDoc, setDoc, updateDoc } from 'firebase/firestore';
import { afterAll, beforeAll, beforeEach, describe, it } from 'vitest';

import { ALICE, BOB, createTestEnvironment, pendingClip, queuedJob } from './helpers.js';

/**
 * Phase 8, exit criterion 2 — the rules half of the rights gate.
 *
 * The worker half is in apps/worker/tests/unit/test_rights.py. Both are required
 * and neither is redundant: the worker authenticates with the Admin SDK and
 * bypasses these rules entirely, so rules alone would protect nothing on the
 * path that actually performs the upload — and conversely a rule is the only
 * thing that can stop a client enqueueing publish work in the first place.
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
});

const aliceDb = () => testEnv.authenticatedContext(ALICE).firestore();
const bobDb = () => testEnv.authenticatedContext(BOB).firestore();

async function seed(path: string, data: Record<string, unknown>): Promise<void> {
  await testEnv.withSecurityRulesDisabled(async (ctx) => {
    await setDoc(doc(ctx.firestore(), path), data);
  });
}

const ATTESTED_AT = '2026-09-01T09:00:00.000Z';

function attestation(overrides: Record<string, unknown> = {}) {
  return {
    basis: 'OWN_CONTENT',
    attestedBy: ALICE,
    attestedAt: ATTESTED_AT,
    note: null,
    ...overrides,
  };
}

/** A publish job as the PWA would create it for an approved, attested clip. */
function publishJob(uid: string, overrides: Record<string, unknown> = {}) {
  return queuedJob(uid, {
    id: 'job-pub-1',
    type: 'PUBLISH',
    clipId: 'clip-1',
    notBefore: null,
    stages: [{ name: 'PUBLISH', lane: 'CPU', status: 'PENDING' }],
    ...overrides,
  });
}

describe('the rights gate on publish jobs', () => {
  it('accepts a publish job for an approved, attested clip', async () => {
    await seed('clips/clip-1', pendingClip(ALICE, { review: 'APPROVED', rights: attestation() }));

    await assertSucceeds(
      setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE)),
    );
  });

  it('refuses a publish job for a clip with no attestation at all', async () => {
    // The case the gate exists for: rendered, approved, and nobody has said why
    // publishing it would be legitimate.
    await seed('clips/clip-1', pendingClip(ALICE, { review: 'APPROVED', rights: null }));

    await assertFails(setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE)));
  });

  it('refuses a publish job for a clip that was never approved', async () => {
    await seed('clips/clip-1', pendingClip(ALICE, { review: 'PENDING', rights: attestation() }));

    await assertFails(setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE)));
  });

  it('refuses a publish job for a clip that was rejected', async () => {
    await seed('clips/clip-1', pendingClip(ALICE, { review: 'REJECTED', rights: attestation() }));

    await assertFails(setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE)));
  });

  it('refuses an attestation with an invented basis', async () => {
    // The enum is enforced here as well as in the schema, because the schema is
    // a generator input and the client is what actually writes this field.
    await seed(
      'clips/clip-1',
      pendingClip(ALICE, { review: 'APPROVED', rights: attestation({ basis: 'PROBABLY_FINE' }) }),
    );

    await assertFails(setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE)));
  });

  it('refuses an attestation with nobody’s name against it', async () => {
    // An unsigned attestation cannot answer the one question the audit log
    // exists to answer.
    await seed(
      'clips/clip-1',
      pendingClip(ALICE, { review: 'APPROVED', rights: attestation({ attestedBy: '' }) }),
    );

    await assertFails(setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE)));
  });

  it('refuses an undated attestation', async () => {
    await seed(
      'clips/clip-1',
      pendingClip(ALICE, { review: 'APPROVED', rights: attestation({ attestedAt: null }) }),
    );

    await assertFails(setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE)));
  });

  it('refuses a publish job that names no clip', async () => {
    await assertFails(
      setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE, { clipId: null })),
    );
  });

  it('refuses a publish job for a clip the caller does not own', async () => {
    // Bob's clip is perfectly publishable — by Bob.
    await seed('clips/clip-1', pendingClip(BOB, { review: 'APPROVED', rights: attestation({ attestedBy: BOB }) }));

    await assertFails(setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE)));
  });

  it('refuses a publish job for a clip that does not exist', async () => {
    await assertFails(setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE)));
  });

  it('still lets an ordinary CLIP job through without any clip or attestation', async () => {
    // The gate must apply to PUBLISH jobs only. Ingestion is private use and
    // needs no rights basis — that distinction is the whole design.
    await assertSucceeds(
      setDoc(
        doc(aliceDb(), 'jobs/job-1'),
        queuedJob(ALICE, { type: 'CLIP', submission: 'https://youtu.be/x' }),
      ),
    );
  });
});

describe('publications are the worker’s to write', () => {
  beforeEach(async () => {
    await seed('clips/clip-1', pendingClip(ALICE, { review: 'APPROVED', rights: attestation() }));
    await seed('clips/clip-1/publications/pub-1', {
      id: 'pub-1',
      clipId: 'clip-1',
      uid: ALICE,
      platform: 'YOUTUBE',
      state: 'PUBLISHED',
      externalId: 'vid-1',
      externalUrl: 'https://www.youtube.com/watch?v=vid-1',
      rights: attestation(),
      createdAt: ATTESTED_AT,
    });
  });

  it('lets the owner read their own audit trail', async () => {
    // Exit criterion 5 depends on this being readable: "who authorised this and
    // on what basis" has to be answerable from the phone, not from worker logs.
    await assertSucceeds(getDoc(doc(aliceDb(), 'clips/clip-1/publications/pub-1')));
  });

  it('hides one user’s publications from another', async () => {
    await assertFails(getDoc(doc(bobDb(), 'clips/clip-1/publications/pub-1')));
  });

  it('refuses a client that tries to forge a publication record', async () => {
    // A client that could write here could claim a clip was published on a
    // basis nobody attested — which would make the audit log worse than none.
    await assertFails(
      setDoc(doc(aliceDb(), 'clips/clip-1/publications/pub-2'), {
        id: 'pub-2',
        clipId: 'clip-1',
        uid: ALICE,
        platform: 'YOUTUBE',
        state: 'PUBLISHED',
        createdAt: ATTESTED_AT,
      }),
    );
  });

  it('refuses a client rewriting an existing publication', async () => {
    await assertFails(
      updateDoc(doc(aliceDb(), 'clips/clip-1/publications/pub-1'), { state: 'CANCELLED' }),
    );
  });
});
