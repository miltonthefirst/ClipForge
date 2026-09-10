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
      rights: attestation(),
    },
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

  it('accepts a publish job for a clip somebody else submitted', async () => {
    // One workspace, one library. What gates publishing is the state of the
    // clip — approved, and attested by a named person — not which account
    // happened to submit the job that produced it.
    await seed('clips/clip-1', pendingClip(BOB, { review: 'APPROVED', rights: attestation({ attestedBy: BOB }) }));

    await assertSucceeds(setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE)));
  });

  it("still refuses somebody else's clip that nobody has attested", async () => {
    // Sharing widens who may publish. It does not remove the rights gate.
    await seed('clips/clip-1', pendingClip(BOB, { review: 'APPROVED' }));

    await assertFails(setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE)));
  });

  it('refuses a publish job for a clip that does not exist', async () => {
    await assertFails(setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE)));
  });

  it('accepts a publish job carrying per-upload overrides', async () => {
    // The whole point of the options block: title, description, privacy,
    // category, tags and destination, chosen for this one upload.
    await seed('clips/clip-1', pendingClip(ALICE, { review: 'APPROVED', rights: attestation() }));

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
    await seed('clips/clip-1', pendingClip(ALICE, { review: 'APPROVED', rights: attestation() }));

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
    await seed('clips/clip-1', pendingClip(ALICE, { review: 'APPROVED', rights: attestation() }));

    await assertFails(
      setDoc(
        doc(aliceDb(), 'jobs/job-pub-1'),
        publishJob(ALICE, { publishOptions: { privacy: 'everyone' } }),
      ),
    );
  });

  it('refuses a title longer than YouTube would accept', async () => {
    await seed('clips/clip-1', pendingClip(ALICE, { review: 'APPROVED', rights: attestation() }));

    await assertFails(
      setDoc(
        doc(aliceDb(), 'jobs/job-pub-1'),
        publishJob(ALICE, { publishOptions: { title: 'x'.repeat(101) } }),
      ),
    );
  });

  it('refuses an unrecognised field in the options block', async () => {
    // A field nobody validates is a field the worker's resolver has never seen.
    await seed('clips/clip-1', pendingClip(ALICE, { review: 'APPROVED', rights: attestation() }));

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
    await seed('clips/clip-1', pendingClip(ALICE, { review: 'APPROVED', rights: attestation() }));

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

  it('shows the publication history to any approved member', async () => {
    // The audit trail answers "who authorised this, and on what basis" — a
    // question the whole team has, and one the record itself still answers
    // because `attestedBy` names a person.
    await assertSucceeds(getDoc(doc(bobDb(), 'clips/clip-1/publications/pub-1')));
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

  it('refuses one whose music has no rights recorded', async () => {
    // The whole point of gating the music separately: a Content ID match does
    // not care which half of the file it came from.
    await seed('clips/clip-1', pendingClip(ALICE));

    await assertFails(
      setDoc(
        doc(aliceDb(), 'jobs/job-music-1'),
        musicJob(ALICE, {
          musicOptions: { source: 'https://youtu.be/x', mode: 'BED', captions: 'KEEP' },
        }),
      ),
    );
  });

  it("refuses one attesting the music in somebody else's name", async () => {
    await seed('clips/clip-1', pendingClip(ALICE));

    await assertFails(
      setDoc(
        doc(aliceDb(), 'jobs/job-music-1'),
        musicJob(ALICE, {
          musicOptions: {
            source: 'https://youtu.be/x',
            mode: 'BED',
            captions: 'KEEP',
            rights: attestation({ attestedBy: BOB }),
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
              rights: attestation(),
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
    rights: attestation(),
    ...overrides,
  });

  it('accepts one where both the footage and the track are attested', async () => {
    await seed(
      'clips/clip-1',
      pendingClip(ALICE, { review: 'APPROVED', rights: attestation(), music: withMusic() }),
    );

    await assertSucceeds(setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE)));
  });

  it('refuses one whose track nobody vouched for', async () => {
    // The footage is fine. The music is not, and that is enough.
    await seed(
      'clips/clip-1',
      pendingClip(ALICE, {
        review: 'APPROVED',
        rights: attestation(),
        music: withMusic({ rights: null }),
      }),
    );

    await assertFails(setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE)));
  });

  it('still refuses one whose footage nobody vouched for, music or not', async () => {
    await seed(
      'clips/clip-1',
      pendingClip(ALICE, { review: 'APPROVED', rights: null, music: withMusic() }),
    );

    await assertFails(setDoc(doc(aliceDb(), 'jobs/job-pub-1'), publishJob(ALICE)));
  });
});
