import {
  assertFails,
  assertSucceeds,
  type RulesTestEnvironment,
} from '@firebase/rules-unit-testing';
import { doc, getDoc, setDoc, updateDoc } from 'firebase/firestore';
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
 * Phase 8, exit criterion 2 — the rules half of the publish gate.
 *
 * The worker half is in apps/worker/tests/unit/test_publish_stage.py. Both are
 * required
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
  // Approval gating means an account with no profile row can read nothing at
  // all. Both test users are approved members unless a test says otherwise;
  // the gate itself is exercised in access.rules.spec.ts.
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

const PUBLISHED_AT = '2026-09-01T09:00:00.000Z';

/**
 * A rights attestation, as clips written before its removal still carry.
 *
 * Kept here only to assert the field is now inert — never to satisfy a rule.
 */
function legacyAttestation(overrides: Record<string, unknown> = {}) {
  return {
    basis: 'OWN_CONTENT',
    attestedBy: ALICE,
    attestedAt: PUBLISHED_AT,
    note: null,
    ...overrides,
  };
}

/** A publish job as the PWA would create it for an approved clip. */
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

/** A music job as the clip page would create it. */
function musicJob(uid: string, overrides: Record<string, unknown> = {}) {
  return queuedJob(uid, {
    id: 'job-music-1',
    type: 'MUSIC',
    clipId: 'clip-1',
    notBefore: null,
    stages: [{ name: 'MUSIC', lane: 'CPU', status: 'PENDING' }],
    musicOptions: {
      source: 'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
      mode: 'BED',
      captions: 'KEEP',
    },
    ...overrides,
  });
}

/** An upload job as the review page would create it from a phone. */
function uploadJob(uid: string, overrides: Record<string, unknown> = {}) {
  return queuedJob(uid, {
    id: 'job-upload-1',
    type: 'UPLOAD',
    clipId: 'clip-1',
    notBefore: null,
    stages: [{ name: 'UPLOAD', lane: 'CPU', status: 'PENDING' }],
    ...overrides,
  });
}

describe('the publish gate on publish jobs', () => {
  it('accepts a publish job for an approved clip', async () => {
    await seed('clips/clip-1', pendingClip(ALICE, { review: 'APPROVED' }));

    await assertSucceeds(setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE)));
  });

  it('refuses a publish job for a clip that was never approved', async () => {
    await seed('clips/clip-1', pendingClip(ALICE, { review: 'PENDING' }));

    await assertFails(setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE)));
  });

  it('refuses a publish job for a clip that was rejected', async () => {
    await seed('clips/clip-1', pendingClip(ALICE, { review: 'REJECTED' }));

    await assertFails(setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE)));
  });

  it('asks nothing about rights, which is no longer a field', async () => {
    // The removal, asserted rather than assumed: approval alone gets through.
    await seed('clips/clip-1', pendingClip(ALICE, { review: 'APPROVED', rights: null }));

    await assertSucceeds(setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE)));
  });

  it('accepts a clip written before the removal that still carries one', async () => {
    // Live documents were not migrated. A field the rules no longer read has to
    // be inert, not a reason to refuse — including one that would have failed
    // the old gate, since nothing evaluates it any more.
    await seed(
      'clips/clip-1',
      pendingClip(ALICE, {
        review: 'APPROVED',
        rights: legacyAttestation({ basis: 'PROBABLY_FINE', attestedBy: '' }),
      }),
    );

    await assertSucceeds(setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE)));
  });

  it('refuses a publish job that names no clip', async () => {
    await assertFails(
      setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE, { clipId: null })),
    );
  });

  it('accepts a publish job for a clip somebody else submitted', async () => {
    // One workspace, one library. What gates publishing is the state of the
    // clip — approved — not which account happened to submit the job that
    // produced it.
    await seed('clips/clip-1', pendingClip(BOB, { review: 'APPROVED' }));

    await assertSucceeds(setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE)));
  });

  it("still refuses somebody else's clip that nobody has approved", async () => {
    // Sharing widens who may publish. It does not remove the gate.
    await seed('clips/clip-1', pendingClip(BOB, { review: 'PENDING' }));

    await assertFails(setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE)));
  });

  it('refuses a publish job for a clip that does not exist', async () => {
    await assertFails(setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE)));
  });

  it('accepts a publish job carrying per-upload overrides', async () => {
    // The whole point of the options block: title, description, privacy,
    // category, tags and destination, chosen for this one upload.
    await seed('clips/clip-1', pendingClip(ALICE, { review: 'APPROVED' }));

    await assertSucceeds(
      setDoc(
        doc(aliceDb(), 'jobs/job-pub-1'),
        publishJob(ALICE, {
          publishOptions: {
            channelId: 'youtube-primary',
            title: 'A hook worth watching',
            description: 'why it matters',
            privacy: 'public',
            categoryId: '27',
            tags: ['angular', 'signals'],
          },
        }),
      ),
    );
  });

  it('accepts an options block that overrides nothing', async () => {
    // The shape the UI writes when the operator never opens the panel. All-null
    // must be as acceptable as absent, or the common case would be the refused
    // one.
    await seed('clips/clip-1', pendingClip(ALICE, { review: 'APPROVED' }));

    await assertSucceeds(
      setDoc(
        doc(aliceDb(), 'jobs/job-pub-1'),
        publishJob(ALICE, {
          publishOptions: {
            channelId: null,
            title: null,
            description: null,
            privacy: null,
            categoryId: null,
            tags: null,
          },
        }),
      ),
    );
  });

  it('refuses a privacy setting that is not one of the three', async () => {
    // 'public' is not recoverable, so the set of things that field may say is
    // worth pinning down in the one place the client cannot edit.
    await seed('clips/clip-1', pendingClip(ALICE, { review: 'APPROVED' }));

    await assertFails(
      setDoc(
        doc(aliceDb(), 'jobs/job-pub-1'),
        publishJob(ALICE, { publishOptions: { privacy: 'everyone' } }),
      ),
    );
  });

  it('refuses a title longer than YouTube would accept', async () => {
    await seed('clips/clip-1', pendingClip(ALICE, { review: 'APPROVED' }));

    await assertFails(
      setDoc(
        doc(aliceDb(), 'jobs/job-pub-1'),
        publishJob(ALICE, { publishOptions: { title: 'x'.repeat(101) } }),
      ),
    );
  });

  it('refuses an unrecognised field in the options block', async () => {
    // A field nobody validates is a field the worker's resolver has never seen.
    await seed('clips/clip-1', pendingClip(ALICE, { review: 'APPROVED' }));

    await assertFails(
      setDoc(
        doc(aliceDb(), 'jobs/job-pub-1'),
        publishJob(ALICE, { publishOptions: { madeForKids: true } }),
      ),
    );
  });

  it('refuses a tag list long enough to be document bloat', async () => {
    // Only the first twenty are ever sent, so a longer list is storage that
    // will never be read.
    await seed('clips/clip-1', pendingClip(ALICE, { review: 'APPROVED' }));

    await assertFails(
      setDoc(
        doc(aliceDb(), 'jobs/job-pub-1'),
        publishJob(ALICE, {
          publishOptions: { tags: Array.from({ length: 21 }, (_, i) => `t${i}`) },
        }),
      ),
    );
  });

  it('applies the options check to every job type, not only PUBLISH', async () => {
    // A CLIP job has no business carrying publish options, and a rule that only
    // looked at PUBLISH jobs would let one through unvalidated — where it would
    // sit until someone wrote code that read it.
    await assertFails(
      setDoc(
        doc(aliceDb(), 'jobs/job-1'),
        queuedJob(ALICE, {
          type: 'CLIP',
          submission: 'https://youtu.be/x',
          publishOptions: { privacy: 'everyone' },
        }),
      ),
    );
  });

  it('still lets an ordinary CLIP job through without naming a clip at all', async () => {
    // The gate must apply to PUBLISH jobs only. Ingesting a video is not
    // publishing one, and nothing about it needs approving first.
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
    await seed('clips/clip-1', pendingClip(ALICE, { review: 'APPROVED' }));
    await seed('clips/clip-1/publications/pub-1', {
      id: 'pub-1',
      clipId: 'clip-1',
      uid: ALICE,
      platform: 'YOUTUBE',
      state: 'PUBLISHED',
      externalId: 'vid-1',
      externalUrl: 'https://www.youtube.com/watch?v=vid-1',
      createdAt: PUBLISHED_AT,
    });
  });

  it('lets the owner read their own audit trail', async () => {
    // Exit criterion 5 depends on this being readable: "what went out, and
    // where" has to be answerable from the phone, not from worker logs.
    await assertSucceeds(getDoc(doc(aliceDb(), 'clips/clip-1/publications/pub-1')));
  });

  it('shows the publication history to any approved member', async () => {
    // The audit trail answers "what did we post, and where" — a question the
    // whole team has, not only whoever pressed the button.
    await assertSucceeds(getDoc(doc(bobDb(), 'clips/clip-1/publications/pub-1')));
  });

  it('refuses a client that tries to forge a publication record', async () => {
    // A client that could write here could claim a clip was published that
    // never was — which would make the audit log worse than none.
    await assertFails(
      setDoc(doc(aliceDb(), 'clips/clip-1/publications/pub-2'), {
        id: 'pub-2',
        clipId: 'clip-1',
        uid: ALICE,
        platform: 'YOUTUBE',
        state: 'PUBLISHED',
        createdAt: PUBLISHED_AT,
      }),
    );
  });

  it('refuses a client rewriting an existing publication', async () => {
    await assertFails(
      updateDoc(doc(aliceDb(), 'clips/clip-1/publications/pub-1'), { state: 'CANCELLED' }),
    );
  });
});

describe('music jobs', () => {
  it('accepts one for a clip that has not been approved yet', async () => {
    // Choosing a track is part of deciding whether the clip is any good. The
    // scored version comes back PENDING and is reviewed on its own merits.
    await seed('clips/clip-1', pendingClip(ALICE));

    await assertSucceeds(setDoc(doc(aliceDb(), 'jobs/job-music-1'), musicJob(ALICE)));
  });

  it('accepts one for a clip somebody else submitted', async () => {
    await seed('clips/clip-1', pendingClip(BOB));

    await assertSucceeds(setDoc(doc(aliceDb(), 'jobs/job-music-1'), musicJob(ALICE)));
  });

  it('refuses one naming a clip that does not exist', async () => {
    await assertFails(setDoc(doc(aliceDb(), 'jobs/job-music-1'), musicJob(ALICE)));
  });

  it('asks for nothing but a source, a mode and a caption choice', async () => {
    // A track used to need its own attestation before this rule would pass.
    // It does not any more, and the minimal request is the ordinary one.
    await seed('clips/clip-1', pendingClip(ALICE));

    await assertSucceeds(
      setDoc(
        doc(aliceDb(), 'jobs/job-music-1'),
        musicJob(ALICE, {
          musicOptions: { source: 'https://youtu.be/x', mode: 'BED', captions: 'KEEP' },
        }),
      ),
    );
  });

  it('refuses one carrying a field the options no longer have', async () => {
    // `hasOnly` is doing this, and it matters more after a removal than before:
    // a client still sending `rights` is a client running old code, and a
    // silently-accepted unknown field is how a schema and its writers drift.
    await seed('clips/clip-1', pendingClip(ALICE));

    await assertFails(
      setDoc(
        doc(aliceDb(), 'jobs/job-music-1'),
        musicJob(ALICE, {
          musicOptions: {
            source: 'https://youtu.be/x',
            mode: 'BED',
            captions: 'KEEP',
            rights: legacyAttestation(),
          },
        }),
      ),
    );
  });

  it('refuses a mode or caption choice it does not recognise', async () => {
    await seed('clips/clip-1', pendingClip(ALICE));

    for (const bad of [{ mode: 'SIDECHAIN' }, { captions: 'BLUR' }]) {
      await assertFails(
        setDoc(
          doc(aliceDb(), 'jobs/job-music-1'),
          musicJob(ALICE, {
            musicOptions: {
              source: 'https://youtu.be/x',
              mode: 'BED',
              captions: 'KEEP',
              ...bad,
            },
          }),
        ),
      );
    }
  });
});

describe('publishing a clip that has music', () => {
  const withMusic = (overrides: Record<string, unknown> = {}) => ({
    mode: 'BED',
    captions: 'KEEP',
    source: 'https://youtu.be/x',
    ...overrides,
  });

  it('accepts one, because music changes nothing about the gate', async () => {
    // A scored clip used to have to answer for the track as well as the
    // footage. Both halves of that were attestations, and both are gone.
    await seed(
      'clips/clip-1',
      pendingClip(ALICE, { review: 'APPROVED', music: withMusic() }),
    );

    await assertSucceeds(setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE)));
  });

  it('still refuses one nobody has approved', async () => {
    // What music does not do is bypass the condition that remains.
    await seed('clips/clip-1', pendingClip(ALICE, { review: 'PENDING', music: withMusic() }));

    await assertFails(setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE)));
  });
});

describe('upload jobs', () => {
  it('lets a reviewer ask for a clip they cannot play', async () => {
    // The whole case: reviewing on a phone, the clip is on the worker's disk
    // only, and the reviewer is nowhere near that machine. No approval is
    // required — being unable to watch it is precisely why it has not been
    // reviewed yet.
    await seed('clips/clip-1', pendingClip(ALICE));

    await assertSucceeds(setDoc(doc(aliceDb(), 'jobs/job-upload-1'), uploadJob(ALICE)));
  });

  it('lets any approved member ask, not only whoever submitted it', async () => {
    // One shared workspace: whoever is holding a phone is the reviewer.
    await seed('clips/clip-1', pendingClip(ALICE));

    await assertSucceeds(
      setDoc(doc(bobDb(), 'jobs/job-upload-1'), uploadJob(BOB)),
    );
  });

  it('refuses one that names no clip', async () => {
    // The worker would claim it, fail, and burn attempts reporting a request
    // that could never have been satisfied.
    await assertFails(
      setDoc(doc(aliceDb(), 'jobs/job-upload-1'), uploadJob(ALICE, { clipId: null })),
    );
  });

  it('refuses one that names a clip that does not exist', async () => {
    await assertFails(
      setDoc(doc(aliceDb(), 'jobs/job-upload-1'), uploadJob(ALICE, { clipId: 'no-such-clip' })),
    );
  });

  it('refuses one that arrives already claimed', async () => {
    // The generic guard, restated for this type: a job may only be created in
    // the one state that means "not yet started".
    await seed('clips/clip-1', pendingClip(ALICE));

    await assertFails(
      setDoc(doc(aliceDb(), 'jobs/job-upload-1'), uploadJob(ALICE, { workerId: 'worker-1' })),
    );
  });
});
