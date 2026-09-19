import {
  assertFails,
  assertSucceeds,
  type RulesTestEnvironment,
} from "@firebase/rules-unit-testing";
import { deleteDoc, doc, getDoc, setDoc, updateDoc } from "firebase/firestore";
import { afterAll, beforeAll, beforeEach, describe, it } from "vitest";

import {
  ALICE,
  BOB,
  createTestEnvironment,
  queuedJob,
  userProfile,
} from "./helpers.js";

/**
 * The rules half of trend research and compilation.
 *
 * Two job types a client may create, and one collection the worker writes
 * that a client may decide about. The options are validated for the same
 * reason MUSIC's and REMAKE's are: they reach yt-dlp, three outside services
 * and ffmpeg on the worker, and a request that cannot succeed should fail at
 * the point of asking rather than after a network round and a render.
 *
 * The worker validates the same values independently — it bypasses these
 * rules entirely — so neither copy is redundant.
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

async function seed(
  path: string,
  data: Record<string, unknown>,
): Promise<void> {
  await testEnv.withSecurityRulesDisabled(async (ctx) => {
    await setDoc(doc(ctx.firestore(), path), data);
  });
}

const create = (db: ReturnType<typeof aliceDb>, job: Record<string, unknown>) =>
  setDoc(doc(db, "jobs", job.id as string), job);

/** A research job as the Trends page creates it. */
function researchJob(
  uid: string,
  options: Record<string, unknown> | null,
  overrides = {},
) {
  return queuedJob(uid, {
    id: "job-research-1",
    type: "RESEARCH",
    stages: [
      { name: "RESEARCH", lane: "CPU", status: "PENDING" },
      { name: "CURATE", lane: "GPU", status: "PENDING" },
    ],
    researchOptions: options,
    ...overrides,
  });
}

const ITEMS = [
  { submission: "https://www.youtube.com/watch?v=aaaaaaaaaaa" },
  { submission: "https://www.youtube.com/watch?v=bbbbbbbbbbb" },
];

/** A compile job as the Compile page creates it. */
function compileJob(
  uid: string,
  options: Record<string, unknown> | null,
  overrides = {},
) {
  return queuedJob(uid, {
    id: "job-compile-1",
    type: "COMPILE",
    stages: [
      { name: "GATHER", lane: "CPU", status: "PENDING" },
      { name: "SELECT", lane: "GPU", status: "PENDING" },
      { name: "ASSEMBLE", lane: "CPU", status: "PENDING" },
    ],
    compileOptions: options,
    ...overrides,
  });
}

/** A trend as the worker would have written it. */
function trend(uid: string, overrides: Record<string, unknown> = {}) {
  return {
    id: "trend-1",
    uid,
    jobId: "job-research-1",
    topic: "brewers vs orioles",
    rank: 1,
    score: 72,
    signals: [
      {
        source: "GOOGLE_TRENDS",
        strength: 0.6,
        detail: "200K+ searches",
        url: null,
      },
    ],
    videos: [],
    matchedTopics: [],
    angle: null,
    relevance: null,
    worthClipping: null,
    compilationTitle: null,
    curated: false,
    status: "NEW",
    decidedAt: null,
    decidedBy: null,
    createdAt: "2026-09-19T12:00:00.000Z",
    ...overrides,
  };
}

// ── Research jobs ────────────────────────────────────────────────────────────

describe("what a research run may ask for", () => {
  it('accepts an empty request, which means "whatever is trending"', async () => {
    await assertSucceeds(create(aliceDb(), researchJob(ALICE, {})));
  });

  it("accepts a fully specified request", async () => {
    await assertSucceeds(
      create(
        aliceDb(),
        researchJob(ALICE, {
          topics: ["premier league", "formula 1"],
          region: "GB",
          category: "football",
          lookbackHours: 24,
          videosPerTopic: 3,
          maxTrends: 10,
          sources: ["GOOGLE_TRENDS", "YOUTUBE"],
          subreddits: ["soccer"],
          curate: false,
        }),
      ),
    );
  });

  it("refuses a research job with no options block at all", async () => {
    await assertFails(create(aliceDb(), researchJob(ALICE, null)));
  });

  it("refuses a region that is not a two-letter code", async () => {
    await assertFails(
      create(aliceDb(), researchJob(ALICE, { region: "United States" })),
    );
  });

  it("accepts no region and no category, which means the worker's defaults", async () => {
    await assertSucceeds(
      create(
        aliceDb(),
        researchJob(ALICE, { region: null, category: null, topics: [] }),
      ),
    );
  });

  it("refuses a category that is not shaped like a catalogue code", async () => {
    await assertFails(
      create(aliceDb(), researchJob(ALICE, { category: "Sports & Games" })),
    );
    await assertFails(
      create(aliceDb(), researchJob(ALICE, { category: "x".repeat(41) })),
    );
    await assertFails(create(aliceDb(), researchJob(ALICE, { category: 7 })));
  });

  it("refuses a lookback outside six hours to a week", async () => {
    await assertFails(
      create(aliceDb(), researchJob(ALICE, { lookbackHours: 2 })),
    );
    await assertFails(
      create(aliceDb(), researchJob(ALICE, { lookbackHours: 1000 })),
    );
  });

  it("caps the number of topics, because each one is a search", async () => {
    const topics = Array.from({ length: 13 }, (_, i) => `topic ${i}`);
    await assertFails(create(aliceDb(), researchJob(ALICE, { topics })));
  });

  it("refuses a field the worker does not read", async () => {
    await assertFails(
      create(aliceDb(), researchJob(ALICE, { autoPublish: true })),
    );
  });

  it("refuses a source that is not one of the three", async () => {
    await assertFails(
      create(aliceDb(), researchJob(ALICE, { sources: ["TIKTOK"] })),
    );
  });

  it("refuses a subreddit name with a path in it", async () => {
    await assertFails(
      create(aliceDb(), researchJob(ALICE, { subreddits: ["../admin"] })),
    );
  });

  it("refuses a research job filed under somebody else's name", async () => {
    await assertFails(create(aliceDb(), researchJob(BOB, {})));
  });
});

// ── Where a job came from ────────────────────────────────────────────────────

describe("a job that remembers its trend", () => {
  const clipJob = (overrides: Record<string, unknown>) =>
    queuedJob(ALICE, {
      type: "CLIP",
      submission: "https://www.youtube.com/watch?v=aaaaaaaaaaa",
      stages: [{ name: "DOWNLOAD", lane: "CPU", status: "PENDING" }],
      ...overrides,
    });

  it("accepts a trend id, and its absence", async () => {
    await assertSucceeds(create(aliceDb(), clipJob({ trendId: "trend-1" })));
    await assertSucceeds(
      create(aliceDb(), clipJob({ id: "job-2", trendId: null })),
    );
    await assertSucceeds(create(aliceDb(), clipJob({ id: "job-3" })));
  });

  it("refuses a trend id that is not a string", async () => {
    await assertFails(create(aliceDb(), clipJob({ trendId: 7 })));
    await assertFails(
      create(aliceDb(), clipJob({ trendId: { id: "trend-1" } })),
    );
  });

  it("does not require the trend to exist, so a deleted list orphans nothing", async () => {
    await assertSucceeds(
      create(aliceDb(), clipJob({ trendId: "never-written" })),
    );
  });
});

// ── Compile jobs ─────────────────────────────────────────────────────────────

describe("what a compilation may ask for", () => {
  it("accepts two items and a theme", async () => {
    await assertSucceeds(
      create(
        aliceDb(),
        compileJob(ALICE, { theme: "best goals of the week", items: ITEMS }),
      ),
    );
  });

  it("accepts every optional field at its edges", async () => {
    await assertSucceeds(
      create(
        aliceDb(),
        compileJob(ALICE, {
          theme: "x",
          title: "A title",
          items: [
            {
              submission: "https://youtu.be/aaaaaaaaaaa",
              startSec: 10,
              endSec: 25,
              note: "the goal",
            },
            {
              submission: "C:/clips/local.mp4",
              startSec: null,
              endSec: null,
              note: null,
            },
          ],
          targetDurationSec: 180,
          maxSegmentSec: 60,
          captions: false,
          titleCard: false,
          transition: "CUT",
          trendId: "trend-1",
        }),
      ),
    );
  });

  it("refuses a compile job with no options block", async () => {
    await assertFails(create(aliceDb(), compileJob(ALICE, null)));
  });

  it("refuses one item, because that is a clip and not a compilation", async () => {
    await assertFails(
      create(aliceDb(), compileJob(ALICE, { theme: "x", items: [ITEMS[0]] })),
    );
  });

  it("caps the items at twelve", async () => {
    const items = Array.from({ length: 13 }, (_, i) => ({
      submission: `https://youtu.be/${i}`,
    }));
    await assertFails(
      create(aliceDb(), compileJob(ALICE, { theme: "x", items })),
    );
  });

  it("refuses an empty theme", async () => {
    await assertFails(
      create(aliceDb(), compileJob(ALICE, { theme: "", items: ITEMS })),
    );
  });

  it("refuses an item with no submission", async () => {
    await assertFails(
      create(
        aliceDb(),
        compileJob(ALICE, { theme: "x", items: [ITEMS[0], { note: "hi" }] }),
      ),
    );
  });

  it("refuses a target length the segments could not fill", async () => {
    await assertFails(
      create(
        aliceDb(),
        compileJob(ALICE, { theme: "x", items: ITEMS, targetDurationSec: 5 }),
      ),
    );
    await assertFails(
      create(
        aliceDb(),
        compileJob(ALICE, { theme: "x", items: ITEMS, targetDurationSec: 600 }),
      ),
    );
  });

  it("refuses a transition the assembler has no code for", async () => {
    await assertFails(
      create(
        aliceDb(),
        compileJob(ALICE, { theme: "x", items: ITEMS, transition: "WIPE" }),
      ),
    );
  });

  it("refuses a field the worker does not read", async () => {
    await assertFails(
      create(
        aliceDb(),
        compileJob(ALICE, { theme: "x", items: ITEMS, narrate: true }),
      ),
    );
  });

  it("validates every item, not only the first two", async () => {
    const items = [...ITEMS, ...ITEMS, { submission: "" }];
    await assertFails(
      create(aliceDb(), compileJob(ALICE, { theme: "x", items })),
    );
  });
});

// ── Trends ───────────────────────────────────────────────────────────────────

describe("trends", () => {
  beforeEach(async () => {
    await seed("trends/trend-1", trend(ALICE));
  });

  it("lets any approved member read the list", async () => {
    await assertSucceeds(getDoc(doc(bobDb(), "trends/trend-1")));
  });

  it("denies the client inventing a trend", async () => {
    await assertFails(
      setDoc(doc(aliceDb(), "trends/trend-2"), trend(ALICE, { id: "trend-2" })),
    );
  });

  it("lets a member promote or dismiss a trend", async () => {
    await assertSucceeds(
      updateDoc(doc(aliceDb(), "trends/trend-1"), {
        status: "PROMOTED",
        decidedAt: "2026-09-19T13:00:00.000Z",
        decidedBy: ALICE,
      }),
    );
    await assertSucceeds(
      updateDoc(doc(bobDb(), "trends/trend-1"), {
        status: "DISMISSED",
        decidedAt: "2026-09-19T13:00:00.000Z",
        decidedBy: BOB,
      }),
    );
  });

  it("denies a decision recorded under somebody else", async () => {
    await assertFails(
      updateDoc(doc(aliceDb(), "trends/trend-1"), {
        status: "DISMISSED",
        decidedAt: "2026-09-19T13:00:00.000Z",
        decidedBy: BOB,
      }),
    );
  });

  it("denies a status that is not one of the three", async () => {
    await assertFails(
      updateDoc(doc(aliceDb(), "trends/trend-1"), { status: "PUBLISHED" }),
    );
  });

  it("denies rewriting the score, the rank or the videos", async () => {
    await assertFails(
      updateDoc(doc(aliceDb(), "trends/trend-1"), { score: 100 }),
    );
    await assertFails(
      updateDoc(doc(aliceDb(), "trends/trend-1"), { rank: 5, status: "NEW" }),
    );
    await assertFails(
      updateDoc(doc(aliceDb(), "trends/trend-1"), {
        videos: [
          { url: "x", externalId: "x", title: "x", score: 1, via: "YOUTUBE" },
        ],
      }),
    );
  });

  it("denies rewriting what the model said", async () => {
    await assertFails(
      updateDoc(doc(aliceDb(), "trends/trend-1"), { angle: "anything I like" }),
    );
  });

  it("lets a member delete a stale list", async () => {
    await assertSucceeds(deleteDoc(doc(bobDb(), "trends/trend-1")));
  });

  it("denies an anonymous caller entirely", async () => {
    const anon = testEnv.unauthenticatedContext().firestore();
    await assertFails(getDoc(doc(anon, "trends/trend-1")));
  });
});
