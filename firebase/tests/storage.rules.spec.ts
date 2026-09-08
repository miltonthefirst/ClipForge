import {
  assertFails,
  assertSucceeds,
  type RulesTestEnvironment,
} from '@firebase/rules-unit-testing';
import { getBytes, ref, uploadBytes } from 'firebase/storage';
import { afterAll, beforeAll, beforeEach, describe, it } from 'vitest';

import { ALICE, BOB, createTestEnvironment } from './helpers.js';

let testEnv: RulesTestEnvironment;

beforeAll(async () => {
  testEnv = await createTestEnvironment();
});

afterAll(async () => {
  await testEnv.cleanup();
});

beforeEach(async () => {
  await testEnv.clearStorage();
});

const ALICE_CLIP = `users/${ALICE}/clips/clip-1.mp4`;
const ALICE_THUMB = `users/${ALICE}/thumbnails/clip-1.jpg`;

/** Upload as the worker would, bypassing rules exactly as the Admin SDK does. */
async function seedObject(path: string): Promise<void> {
  await testEnv.withSecurityRulesDisabled(async (ctx) => {
    await uploadBytes(ref(ctx.storage(), path), new Uint8Array([1, 2, 3]));
  });
}

describe('storage per-user isolation', () => {
  beforeEach(async () => {
    await seedObject(ALICE_CLIP);
    await seedObject(ALICE_THUMB);
  });

  it('lets the owner read their own clip', async () => {
    await assertSucceeds(getBytes(ref(testEnv.authenticatedContext(ALICE).storage(), ALICE_CLIP)));
  });

  it('lets the owner read their own thumbnail', async () => {
    await assertSucceeds(getBytes(ref(testEnv.authenticatedContext(ALICE).storage(), ALICE_THUMB)));
  });

  it("denies another user reading it", async () => {
    await assertFails(getBytes(ref(testEnv.authenticatedContext(BOB).storage(), ALICE_CLIP)));
  });

  it('denies an anonymous caller reading it', async () => {
    await assertFails(getBytes(ref(testEnv.unauthenticatedContext().storage(), ALICE_CLIP)));
  });

  it("denies another user overwriting it", async () => {
    await assertFails(
      uploadBytes(
        ref(testEnv.authenticatedContext(BOB).storage(), ALICE_CLIP),
        new Uint8Array([9]),
      ),
    );
  });
});

describe('clips are produced by the worker, never the client', () => {
  it('denies even the owner uploading into their own clip path', async () => {
    await assertFails(
      uploadBytes(
        ref(testEnv.authenticatedContext(ALICE).storage(), ALICE_CLIP),
        new Uint8Array([9]),
      ),
    );
  });

  it('denies writing outside the declared layout', async () => {
    await assertFails(
      uploadBytes(
        ref(testEnv.authenticatedContext(ALICE).storage(), 'anything/else.mp4'),
        new Uint8Array([9]),
      ),
    );
  });

  it('denies reading outside the declared layout', async () => {
    await seedObject('somewhere/private.mp4');
    await assertFails(
      getBytes(ref(testEnv.authenticatedContext(ALICE).storage(), 'somewhere/private.mp4')),
    );
  });
});
