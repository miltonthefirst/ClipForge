import {
  assertFails,
  assertSucceeds,
  type RulesTestEnvironment,
} from '@firebase/rules-unit-testing';
import { doc, setDoc } from 'firebase/firestore';
import { afterAll, beforeAll, beforeEach, describe, it } from 'vitest';

import {
  ALICE,
  BOB,
  createTestEnvironment,
  pendingClip,
  queuedJob,
  source,
  userProfile,
} from './helpers.js';

/**
 * The rules half of the REMAKE gate.
 *
 * A remake's options are not decoration: they become ffmpeg filter expressions
 * and a call into a speech synthesiser on the worker. The rule refuses what the
 * worker could not render, so a request that cannot succeed fails at the point
 * of asking rather than after a re-encode — which on a phone is the difference
 * between a typo and a job that sits QUEUED and then FAILED with no obvious
 * cause.
 *
 * The worker validates the same values independently: it authenticates with the
 * Admin SDK and bypasses these rules completely, so neither copy is redundant.
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
  await seed('clips/clip-1', pendingClip(ALICE, { id: 'clip-1' }));
  await seed('sources/src-1', source(ALICE));
});

const aliceDb = () => testEnv.authenticatedContext(ALICE).firestore();
const bobDb = () => testEnv.authenticatedContext(BOB).firestore();

async function seed(path: string, data: Record<string, unknown>): Promise<void> {
  await testEnv.withSecurityRulesDisabled(async (ctx) => {
    await setDoc(doc(ctx.firestore(), path), data);
  });
}

/** A remake job as the clip page would create it. */
function remakeJob(uid: string, options: Record<string, unknown>, overrides = {}) {
  return queuedJob(uid, {
    id: 'job-remake-1',
    type: 'REMAKE',
    clipId: 'clip-1',
    notBefore: null,
    stages: [{ name: 'REMAKE', lane: 'CPU', status: 'PENDING' }],
    remakeOptions: options,
    ...overrides,
  });
}

const create = (db: ReturnType<typeof aliceDb>, job: Record<string, unknown>) =>
  setDoc(doc(db, 'jobs', job.id as string), job);

describe('what a remake may ask for', () => {
  it('accepts a note on its own, because that is the common case', async () => {
    await assertSucceeds(
      create(aliceDb(), remakeJob(ALICE, { notes: 'it keeps losing the ball' })),
    );
  });

  it('accepts an empty request, which means "re-cut it unchanged"', async () => {
    await assertSucceeds(create(aliceDb(), remakeJob(ALICE, {})));
  });

  it('accepts each framing mode', async () => {
    for (const mode of ['AS_RENDERED', 'FIT', 'PAN', 'TRACK']) {
      await assertSucceeds(
        create(aliceDb(), remakeJob(ALICE, { framing: { mode } }, { id: `job-${mode}` })),
      );
    }
  });

  it('refuses a framing mode the worker has no code for', async () => {
    await assertFails(create(aliceDb(), remakeJob(ALICE, { framing: { mode: 'MAGIC' } })));
  });

  it('refuses a crop anchor that is not one of the three', async () => {
    await assertFails(
      create(aliceDb(), remakeJob(ALICE, { framing: { mode: 'AS_RENDERED', crop: 'middle' } })),
    );
  });

  it('refuses a zoom past what the pixels support', async () => {
    await assertFails(
      create(aliceDb(), remakeJob(ALICE, { framing: { mode: 'AS_RENDERED', zoom: 4 } })),
    );
  });

  it('refuses a zoom below 1, which is a wider shot and not this mode', async () => {
    await assertFails(
      create(aliceDb(), remakeJob(ALICE, { framing: { mode: 'AS_RENDERED', zoom: 0.5 } })),
    );
  });

  it('caps the keyframe list, because it becomes one nested filter expression', async () => {
    const tooMany = Array.from({ length: 61 }, (_, i) => ({ atSec: i, xPct: 50 }));
    await assertFails(
      create(aliceDb(), remakeJob(ALICE, { framing: { mode: 'PAN', keyframes: tooMany } })),
    );
  });

  it('accepts a keyframe list within the cap', async () => {
    const points = Array.from({ length: 6 }, (_, i) => ({ atSec: i, xPct: 10 * i }));
    await assertSucceeds(
      create(aliceDb(), remakeJob(ALICE, { framing: { mode: 'PAN', keyframes: points } })),
    );
  });

  it('refuses a field the contract does not have', async () => {
    await assertFails(
      create(
        aliceDb(),
        remakeJob(ALICE, { framing: { mode: 'FIT', shutterAngle: 180 } }),
      ),
    );
  });
});

describe('what a remake may say to the synthesiser', () => {
  const voice = (overrides: Record<string, unknown> = {}) => ({
    mode: 'REPLACE',
    voice: 'af_heart',
    language: 'es',
    captions: 'REBUILD',
    ...overrides,
  });

  it('accepts an ordinary voice request', async () => {
    await assertSucceeds(create(aliceDb(), remakeJob(ALICE, { voice: voice() })));
  });

  it('refuses a speech mode that is not one of the two', async () => {
    await assertFails(
      create(aliceDb(), remakeJob(ALICE, { voice: voice({ mode: 'WHISPER' }) })),
    );
  });

  it('refuses a caption choice the render has no meaning for', async () => {
    await assertFails(
      create(aliceDb(), remakeJob(ALICE, { voice: voice({ captions: 'MAYBE' }) })),
    );
  });

  it('refuses a script longer than the contract allows', async () => {
    await assertFails(
      create(aliceDb(), remakeJob(ALICE, { voice: voice({ script: 'x'.repeat(4001) }) })),
    );
  });

  it('refuses a speed outside what a synthesiser will do', async () => {
    await assertFails(create(aliceDb(), remakeJob(ALICE, { voice: voice({ speed: 5 }) })));
  });

  it('refuses a duck that lifts the original instead of lowering it', async () => {
    await assertFails(create(aliceDb(), remakeJob(ALICE, { voice: voice({ duckDb: 6 }) })));
  });

  it('accepts a duck that lowers it', async () => {
    await assertSucceeds(create(aliceDb(), remakeJob(ALICE, { voice: voice({ duckDb: -18 }) })));
  });

  it('refuses a language tag that is not one', async () => {
    await assertFails(create(aliceDb(), remakeJob(ALICE, { voice: voice({ language: 'e' }) })));
  });
});

describe('trims', () => {
  it('accepts a nudge in either direction', async () => {
    await assertSucceeds(
      create(aliceDb(), remakeJob(ALICE, { startDeltaSec: -3, endDeltaSec: 2.5 })),
    );
  });

  it('refuses a nudge beyond what a clip could absorb', async () => {
    await assertFails(create(aliceDb(), remakeJob(ALICE, { startDeltaSec: -120 })));
  });
});

/**
 * The music a scored clip already carries.
 *
 * A remake re-renders, and the music is baked into the rendered file rather
 * than kept beside it, so the remake has to mix the track again or the clip
 * comes back silent. `keepMusic` is the opt-OUT from that: absent or true means
 * carry it, and only false drops it. The rule exists because the field would
 * otherwise fall foul of `hasOnly` and deny the create with a message that
 * names nothing the reviewer did.
 */
describe('the music a remake carries', () => {
  it('accepts an explicit request to drop the soundtrack', async () => {
    await assertSucceeds(create(aliceDb(), remakeJob(ALICE, { keepMusic: false })));
  });

  it('accepts an untouched control, which carries it', async () => {
    await assertSucceeds(create(aliceDb(), remakeJob(ALICE, { keepMusic: null })));
  });

  it('refuses a string where the worker reads a boolean', async () => {
    await assertFails(create(aliceDb(), remakeJob(ALICE, { keepMusic: 'no' })));
  });
});

describe('who may ask, and for what', () => {
  it('refuses a remake of a clip that does not exist', async () => {
    await assertFails(
      create(aliceDb(), remakeJob(ALICE, {}, { id: 'job-x', clipId: 'clip-missing' })),
    );
  });

  it('refuses a remake with no clip named at all', async () => {
    await assertFails(create(aliceDb(), remakeJob(ALICE, {}, { id: 'job-y', clipId: null })));
  });

  it('refuses a job created under someone else’s uid', async () => {
    await assertFails(create(bobDb(), remakeJob(ALICE, {})));
  });

  it('does not require the clip to be approved first', async () => {
    /**
     * The whole point of a remake is that the clip is not right yet. Requiring
     * approval would mean approving something in order to say it is wrong.
     */
    await assertSucceeds(
      create(aliceDb(), remakeJob(ALICE, { notes: 'the framing is wrong' })),
    );
  });

  it('refuses a job that arrives already running', async () => {
    await assertFails(
      create(aliceDb(), remakeJob(ALICE, {}, { id: 'job-z', status: 'RUNNING' })),
    );
  });

  it('refuses a job that arrives holding a lease', async () => {
    await assertFails(
      create(
        aliceDb(),
        remakeJob(ALICE, {}, { id: 'job-w', leaseExpiresAt: '2026-09-01T09:00:00.000Z' }),
      ),
    );
  });

  it('lets another approved member remake a clip, because the workspace is shared', async () => {
    await assertSucceeds(
      create(bobDb(), remakeJob(BOB, { notes: 'try it tracked' }, { id: 'job-bob' })),
    );
  });
});

/**
 * Hiding a fixed mark: a channel bug, a score bar, a burnt-in caption.
 *
 * Each region becomes a split/crop/overlay triple or a delogo in the
 * filtergraph, so the rule caps how many may be asked for and checks that the
 * percentages are percentages. A region with a negative width collapses to
 * nothing on the worker, which would hide neither the logo nor the reason.
 */
describe('what a remake may ask to hide', () => {
  const region = (overrides: Record<string, unknown> = {}) => ({
    xPct: 83.8,
    yPct: 4.4,
    wPct: 13.8,
    hPct: 8.9,
    ...overrides,
  });

  it('accepts asking the worker to go and find the marks itself', async () => {
    await assertSucceeds(
      create(
        aliceDb(),
        remakeJob(ALICE, { notes: 'blur the canal+', obscure: { auto: true } }),
      ),
    );
  });

  it('accepts a rectangle the reviewer drew', async () => {
    await assertSucceeds(
      create(aliceDb(), remakeJob(ALICE, { obscure: { regions: [region()] } })),
    );
  });

  it('accepts a method and a strength on the region', async () => {
    await assertSucceeds(
      create(
        aliceDb(),
        remakeJob(ALICE, {
          obscure: { regions: [region({ method: 'DELOGO', strength: 0.9, label: 'canal+' })] },
        }),
      ),
    );
  });

  it('refuses a rectangle that runs off the frame', async () => {
    await assertFails(
      create(aliceDb(), remakeJob(ALICE, { obscure: { regions: [region({ xPct: 140 })] } })),
    );
  });

  it('refuses a rectangle with no width, which would hide nothing silently', async () => {
    await assertFails(
      create(aliceDb(), remakeJob(ALICE, { obscure: { regions: [region({ wPct: 0 })] } })),
    );
  });

  it('accepts a method it does not itself check, which the worker rejects', async () => {
    /**
     * Deliberate. Validating the enum here costs expressions the rule does not
     * have (see `obscureRegionOk`), and an unrecognised method fails on the
     * worker against the generated model with a message naming the field. The
     * rule's job is the key set and the geometry.
     */
    await assertSucceeds(
      create(
        aliceDb(),
        remakeJob(ALICE, { obscure: { regions: [region({ method: 'INPAINT' })] } }),
      ),
    );
  });

  it('refuses a field nobody recognises, on the region', async () => {
    await assertFails(
      create(aliceDb(), remakeJob(ALICE, { obscure: { regions: [region({ feather: 4 })] } })),
    );
  });

  it('refuses more regions than the filtergraph should carry', async () => {
    await assertFails(
      create(
        aliceDb(),
        remakeJob(ALICE, {
          obscure: { regions: Array.from({ length: 7 }, () => region()) },
        }),
      ),
    );
  });

  it('accepts exactly the cap, so the limit is the number it says', async () => {
    await assertSucceeds(
      create(
        aliceDb(),
        remakeJob(ALICE, {
          obscure: { regions: Array.from({ length: 6 }, () => region()) },
        }),
      ),
    );
  });

  it('checks every region, not just the first', async () => {
    await assertFails(
      create(
        aliceDb(),
        remakeJob(ALICE, {
          obscure: { regions: [region(), region(), region({ hPct: -3 })] },
        }),
      ),
    );
  });
});

/**
 * The half that makes it stop being a chore.
 *
 * A broadcaster's bug is in the same place on every video it publishes, so the
 * rectangle belongs to the source rather than to a clip. This is the only field
 * on a source a person may write, and the rule pins it to that one name: the
 * rest of a source describes a file on the worker, and a client that could
 * write those could point a render somewhere nobody ingested.
 */
describe('remembering what to hide on a whole channel', () => {
  const always = { regions: [{ xPct: 83.8, yPct: 4.4, wPct: 13.8, hPct: 8.9 }] };

  const update = (db: ReturnType<typeof aliceDb>, data: Record<string, unknown>) =>
    setDoc(doc(db, 'sources', 'src-1'), { ...source(ALICE), ...data });

  it('lets the owner say what this channel always burns into the picture', async () => {
    await assertSucceeds(update(aliceDb(), { obscure: always }));
  });

  it('lets another approved member say it too, because the workspace is shared', async () => {
    await assertSucceeds(update(bobDb(), { obscure: always }));
  });

  it('lets it be cleared', async () => {
    await seed('sources/src-1', { ...source(ALICE), obscure: always });
    await assertSucceeds(update(aliceDb(), { obscure: null }));
  });

  it('refuses a write that also moves the file', async () => {
    await assertFails(
      setDoc(doc(aliceDb(), 'sources', 'src-1'), {
        ...source(ALICE),
        obscure: always,
        localPath: 'P:/somewhere/else.mp4',
      }),
    );
  });

  it('refuses a write that reassigns who owns the source', async () => {
    await assertFails(
      setDoc(doc(aliceDb(), 'sources', 'src-1'), { ...source(BOB), obscure: always }),
    );
  });

  it('refuses a rectangle it would not accept on a job either', async () => {
    await assertFails(
      update(aliceDb(), { obscure: { regions: [{ xPct: 0, yPct: 0, wPct: 0, hPct: 9 }] } }),
    );
  });

  it('still refuses deleting a source', async () => {
    await assertFails(update(aliceDb(), { provider: 'youtube' }));
  });
});
