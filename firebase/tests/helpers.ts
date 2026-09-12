import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  initializeTestEnvironment,
  type RulesTestEnvironment,
} from '@firebase/rules-unit-testing';

const here = dirname(fileURLToPath(import.meta.url));
const firebaseDir = resolve(here, '..');

/** The two users every isolation test is written against. */
export const ALICE = 'alice-uid';
export const BOB = 'bob-uid';

/**
 * `demo-` prefixed project ids put the emulator in fully offline mode: no
 * credentials, no billing, no real project, no network. That is what lets these
 * tests run in CI with no Firebase authentication at all.
 */
export const PROJECT_ID = 'demo-clipforge';

export async function createTestEnvironment(): Promise<RulesTestEnvironment> {
  return initializeTestEnvironment({
    projectId: PROJECT_ID,
    firestore: {
      rules: readFileSync(resolve(firebaseDir, 'firestore.rules'), 'utf8'),
      host: '127.0.0.1',
      port: 8080,
    },
    storage: {
      rules: readFileSync(resolve(firebaseDir, 'storage.rules'), 'utf8'),
      host: '127.0.0.1',
      port: 9199,
    },
  });
}

const TIMESTAMP = '2026-09-08T12:00:00.000Z';

/** A job document as the PWA would legitimately create it. */
export function queuedJob(uid: string, overrides: Record<string, unknown> = {}) {
  return {
    id: 'job-1',
    uid,
    type: 'ECHO',
    status: 'QUEUED',
    sourceId: null,
    stages: [{ name: 'ECHO_ONE', lane: 'CPU', status: 'PENDING' }],
    workerId: null,
    leaseExpiresAt: null,
    attempts: 0,
    maxAttempts: 3,
    error: null,
    createdAt: TIMESTAMP,
    updatedAt: TIMESTAMP,
    startedAt: null,
    endedAt: null,
    ...overrides,
  };
}

/** A clip as the worker would have written it, awaiting review. */
export function pendingClip(uid: string, overrides: Record<string, unknown> = {}) {
  return {
    id: 'clip-1',
    uid,
    candidateId: 'cand-1',
    sourceId: 'src-1',
    jobId: 'job-1',
    location: 'LOCAL',
    localPath: 'P:/workspace/clips/clip-1.mp4',
    playbackUrl: null,
    storagePath: null,
    thumbnailPath: null,
    durationSec: 38.2,
    review: 'PENDING',
    reviewedAt: null,
    rights: null,
    createdAt: TIMESTAMP,
    ...overrides,
  };
}

export function source(uid: string, overrides: Record<string, unknown> = {}) {
  return {
    id: 'src-1',
    uid,
    provider: 'local',
    createdAt: TIMESTAMP,
    ...overrides,
  };
}

export function candidate(uid: string, overrides: Record<string, unknown> = {}) {
  return {
    id: 'cand-1',
    uid,
    sourceId: 'src-1',
    startSec: 142.4,
    endSec: 181.7,
    subScores: {
      hook: 22,
      curiosity: 18,
      standalone: 19,
      emotion: 11,
      pacing: 8,
      shareability: 9,
    },
    total: 87,
    createdAt: TIMESTAMP,
    ...overrides,
  };
}

/**
 * An approved member's profile.
 *
 * Every other fixture here is inert without one: since approval gating landed,
 * a signed-in user with no `users/{uid}` row can read nothing at all, which is
 * exactly the point of it.
 */
export function userProfile(uid: string, overrides: Record<string, unknown> = {}) {
  return {
    uid,
    email: `${uid}@example.com`,
    displayName: null,
    photoUrl: null,
    role: 'MEMBER',
    status: 'APPROVED',
    createdAt: TIMESTAMP,
    decidedAt: TIMESTAMP,
    decidedBy: 'admin-uid',
    ...overrides,
  };
}

export function heartbeat(uid: string, overrides: Record<string, unknown> = {}) {
  return {
    workerId: 'worker-1',
    uid,
    status: 'ONLINE',
    capabilities: { whisper: true, llm: true, render: true, publish: false },
    gpu: null,
    version: '0.0.1',
    lastSeenAt: TIMESTAMP,
    ...overrides,
  };
}
