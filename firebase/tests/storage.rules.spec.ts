import {
  assertFails,
  assertSucceeds,
  type RulesTestEnvironment,
} from '@firebase/rules-unit-testing';
import { doc, setDoc } from 'firebase/firestore';
import { deleteObject, getBytes, ref, uploadBytes } from 'firebase/storage';
import { afterAll, beforeAll, beforeEach, describe, it } from 'vitest';

import { ALICE, BOB, createTestEnvironment, userProfile } from './helpers.js';

/**
 * The bucket holds rendered clips, briefly, so a phone can review them.
 *
 * Three things these assert, in order of how much they would cost to get wrong:
 *
 * 1. **Approval gates the bucket**, exactly as it gates Firestore. The rules
 *    read the caller's `users/{uid}` profile to decide, so an unapproved or
 *    anonymous caller reaches nothing.
 * 2. **No client may write.** Uploads are the worker's, through the Admin SDK.
 *    A client that could upload could put a 2 GB file in a billed bucket.
 * 3. **Any approved member may delete.** Deleting the bucket copy destroys
 *    nothing — the render is still on the worker — and it is how a reviewed
 *    clip stops costing storage before the lifecycle rule notices.
 */

let testEnv: RulesTestEnvironment;

beforeAll(async () => {
  testEnv = await createTestEnvironment();
});

afterAll(async () => {
  await testEnv.cleanup();
});

beforeEach(async () => {
  await testEnv.clearStorage();
  await testEnv.clearFirestore();
});

// Mirrors the blob key the render stage writes: `clips/{uid}/{clipId}.mp4`.
// The uid organises the bucket; it decides nothing.
const ALICE_CLIP = `clips/${ALICE}/clip-1.mp4`;

/** Upload as the worker would, bypassing rules exactly as the Admin SDK does. */
async function seedObject(path: string): Promise<void> {
  await testEnv.withSecurityRulesDisabled(async (ctx) => {
    await uploadBytes(ref(ctx.storage(), path), new Uint8Array([1, 2, 3]));
  });
}

/** Approval lives in Firestore, and the storage rules go and read it. */
async function approve(uid: string, overrides: Record<string, unknown> = {}): Promise<void> {
  await testEnv.withSecurityRulesDisabled(async (ctx) => {
    await setDoc(doc(ctx.firestore(), `users/${uid}`), { ...userProfile(uid), ...overrides });
  });
}

const storageAs = (uid: string) => testEnv.authenticatedContext(uid).storage();
const anonStorage = () => testEnv.unauthenticatedContext().storage();

describe('reading a clip from the bucket', () => {
  beforeEach(async () => {
    await seedObject(ALICE_CLIP);
  });

  it('lets an approved member read it', async () => {
    await approve(ALICE);
    await assertSucceeds(getBytes(ref(storageAs(ALICE), ALICE_CLIP)));
  });

  it('lets a different approved member read it — one shared library', async () => {
    // The uid in the path is Alice's. It is not an access decision, and this is
    // the test that would have failed under the rules this replaced.
    await approve(BOB);
    await assertSucceeds(getBytes(ref(storageAs(BOB), ALICE_CLIP)));
  });

  it('denies a signed-in account that has not been approved', async () => {
    await approve(BOB, { status: 'PENDING' });
    await assertFails(getBytes(ref(storageAs(BOB), ALICE_CLIP)));
  });

  it('denies an account with no profile at all', async () => {
    await assertFails(getBytes(ref(storageAs(BOB), ALICE_CLIP)));
  });

  it('denies an anonymous caller', async () => {
    await assertFails(getBytes(ref(anonStorage(), ALICE_CLIP)));
  });
});

describe('writing to the bucket', () => {
  it('denies an approved member uploading a clip', async () => {
    // Uploads are the worker's. A client that could write here could put a
    // source video — gigabytes, billed — into a bucket meant for 5-20 MB clips.
    await approve(ALICE);
    await assertFails(uploadBytes(ref(storageAs(ALICE), ALICE_CLIP), new Uint8Array([9])));
  });

  it('denies an approved member overwriting an existing clip', async () => {
    await approve(ALICE);
    await seedObject(ALICE_CLIP);
    await assertFails(uploadBytes(ref(storageAs(ALICE), ALICE_CLIP), new Uint8Array([9])));
  });

  it('denies writing outside the declared layout', async () => {
    await approve(ALICE);
    await assertFails(
      uploadBytes(ref(storageAs(ALICE), 'sources/huge.mp4'), new Uint8Array([9])),
    );
  });

  it('denies reading outside the declared layout', async () => {
    await approve(ALICE);
    await seedObject('sources/huge.mp4');
    await assertFails(getBytes(ref(storageAs(ALICE), 'sources/huge.mp4')));
  });
});

describe('deleting a clip from the bucket', () => {
  beforeEach(async () => {
    await seedObject(ALICE_CLIP);
  });

  it('lets an approved member delete it once reviewed', async () => {
    await approve(ALICE);
    await assertSucceeds(deleteObject(ref(storageAs(ALICE), ALICE_CLIP)));
  });

  it('lets a member delete a clip somebody else submitted', async () => {
    await approve(BOB);
    await assertSucceeds(deleteObject(ref(storageAs(BOB), ALICE_CLIP)));
  });

  it('denies an unapproved account deleting anything', async () => {
    await approve(BOB, { status: 'PENDING' });
    await assertFails(deleteObject(ref(storageAs(BOB), ALICE_CLIP)));
  });

  it('denies an anonymous caller deleting anything', async () => {
    await assertFails(deleteObject(ref(anonStorage(), ALICE_CLIP)));
  });
});
