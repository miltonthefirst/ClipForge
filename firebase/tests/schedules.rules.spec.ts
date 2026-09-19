import {
  assertFails,
  assertSucceeds,
  type RulesTestEnvironment,
} from "@firebase/rules-unit-testing";
import { deleteDoc, doc, getDoc, setDoc, updateDoc } from "firebase/firestore";
import { afterAll, beforeAll, beforeEach, describe, it } from "vitest";

import { ALICE, BOB, createTestEnvironment, userProfile } from "./helpers.js";

/**
 * The rules half of automatic research.
 *
 * A schedule is the one document that makes the worker start work with nobody
 * watching, so its shape is checked the way a job's options are — everything
 * in it reaches three outside services and yt-dlp on a timer. The client owns
 * the request and the worker owns the record of what it did with it, and the
 * rules keep those halves apart.
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

const NOW = "2026-09-19T12:00:00.000Z";

/** A schedule as the Trends page creates it. */
function schedule(uid: string, overrides: Record<string, unknown> = {}) {
  return {
    id: "sched-1",
    uid,
    name: "Mornings",
    enabled: true,
    cadence: "DAILY",
    everyHours: null,
    at: "07:30",
    timezone: "Europe/London",
    options: {
      topics: ["premier league"],
      region: "GB",
      lookbackHours: 24,
      curate: true,
    },
    nextDueAt: "2026-09-20T06:30:00.000Z",
    lastRunAt: null,
    lastJobId: null,
    lastOutcome: null,
    createdAt: NOW,
    updatedAt: NOW,
    ...overrides,
  };
}

const create = (
  db: ReturnType<typeof aliceDb>,
  data: Record<string, unknown>,
) => setDoc(doc(db, "schedules", data.id as string), data);

describe("setting up a schedule", () => {
  it("accepts a daily schedule", async () => {
    await assertSucceeds(create(aliceDb(), schedule(ALICE)));
  });

  it("accepts an interval schedule with no time of day", async () => {
    await assertSucceeds(
      create(
        aliceDb(),
        schedule(ALICE, { cadence: "INTERVAL", everyHours: 12, at: null }),
      ),
    );
  });

  it("accepts the smallest possible request: a name, a cadence, and nothing to look for", async () => {
    await assertSucceeds(
      create(
        aliceDb(),
        schedule(ALICE, {
          cadence: "INTERVAL",
          everyHours: 24,
          at: null,
          options: {},
        }),
      ),
    );
  });

  it("refuses a schedule filed under somebody else's name", async () => {
    await assertFails(create(aliceDb(), schedule(BOB)));
  });

  it("refuses a daily schedule with no time of day", async () => {
    await assertFails(create(aliceDb(), schedule(ALICE, { at: null })));
  });

  it("refuses an interval schedule with no interval", async () => {
    await assertFails(
      create(
        aliceDb(),
        schedule(ALICE, { cadence: "INTERVAL", everyHours: null }),
      ),
    );
  });

  it("refuses an interval shorter than six hours, because the feeds are daily", async () => {
    await assertFails(
      create(
        aliceDb(),
        schedule(ALICE, { cadence: "INTERVAL", everyHours: 1 }),
      ),
    );
  });

  it("refuses a time of day that is not HH:MM", async () => {
    await assertFails(create(aliceDb(), schedule(ALICE, { at: "7:30am" })));
    await assertFails(create(aliceDb(), schedule(ALICE, { at: "25:00" })));
  });

  it("refuses a cadence the worker has no code for", async () => {
    await assertFails(
      create(aliceDb(), schedule(ALICE, { cadence: "WEEKLY" })),
    );
  });

  it("checks the options the way a research job is checked", async () => {
    await assertFails(
      create(
        aliceDb(),
        schedule(ALICE, { options: { region: "United Kingdom" } }),
      ),
    );
    await assertFails(
      create(
        aliceDb(),
        schedule(ALICE, { options: { category: "Not A Code" } }),
      ),
    );
    await assertFails(
      create(aliceDb(), schedule(ALICE, { options: { autoPublish: true } })),
    );
    await assertSucceeds(
      create(
        aliceDb(),
        schedule(ALICE, {
          options: { topics: [], region: null, category: "football" },
        }),
      ),
    );
  });

  it("refuses a schedule that arrives claiming to have already run", async () => {
    await assertFails(
      create(aliceDb(), schedule(ALICE, { lastJobId: "job-9" })),
    );
    await assertFails(create(aliceDb(), schedule(ALICE, { lastRunAt: NOW })));
  });

  it("refuses an empty name", async () => {
    await assertFails(create(aliceDb(), schedule(ALICE, { name: "" })));
  });

  it("refuses a field the worker does not read", async () => {
    await assertFails(
      create(aliceDb(), schedule(ALICE, { autoCompile: true })),
    );
  });
});

describe("changing a schedule", () => {
  beforeEach(async () => {
    await seed(
      "schedules/sched-1",
      schedule(ALICE, { lastJobId: "job-1", lastRunAt: NOW }),
    );
  });

  it("lets any member switch it off and on, and move its next run", async () => {
    await assertSucceeds(
      updateDoc(doc(bobDb(), "schedules/sched-1"), {
        enabled: false,
        nextDueAt: null,
        updatedAt: NOW,
      }),
    );
  });

  it("lets a member change what it looks for and how often", async () => {
    await assertSucceeds(
      updateDoc(doc(aliceDb(), "schedules/sched-1"), {
        cadence: "INTERVAL",
        everyHours: 48,
        options: { topics: ["nba"], region: "US" },
        updatedAt: NOW,
      }),
    );
  });

  it("denies rewriting the worker's record of what it did", async () => {
    await assertFails(
      updateDoc(doc(aliceDb(), "schedules/sched-1"), { lastJobId: "job-2" }),
    );
    // A different instant from the seeded one: writing the value already there
    // changes nothing, and a rule sees no change at all.
    await assertFails(
      updateDoc(doc(aliceDb(), "schedules/sched-1"), {
        lastRunAt: "2026-09-20T12:00:00.000Z",
      }),
    );
    await assertFails(
      updateDoc(doc(aliceDb(), "schedules/sched-1"), { lastOutcome: "fired" }),
    );
  });

  it("denies reassigning who set it up", async () => {
    await assertFails(
      updateDoc(doc(bobDb(), "schedules/sched-1"), {
        uid: BOB,
        updatedAt: NOW,
      }),
    );
  });

  it("still checks the shape on an edit", async () => {
    await assertFails(
      updateDoc(doc(aliceDb(), "schedules/sched-1"), {
        at: "noon",
        updatedAt: NOW,
      }),
    );
  });

  it("lets any approved member read and delete", async () => {
    await assertSucceeds(getDoc(doc(bobDb(), "schedules/sched-1")));
    await assertSucceeds(deleteDoc(doc(bobDb(), "schedules/sched-1")));
  });

  it("denies an anonymous caller entirely", async () => {
    const anon = testEnv.unauthenticatedContext().firestore();
    await assertFails(getDoc(doc(anon, "schedules/sched-1")));
  });
});
