import type { Page } from '@playwright/test';

export const PROJECT_ID = 'demo-clipforge';
export const FIRESTORE_HOST = '127.0.0.1:8080';
export const AUTH_HOST = '127.0.0.1:9099';

const BASE = `http://${FIRESTORE_HOST}/v1/projects/${PROJECT_ID}/databases/(default)/documents`;

/**
 * Write documents straight into the emulator over its REST API.
 *
 * This is the stubbed worker. Security rules do not apply to the REST API when
 * talking to the emulator without a token, which is exactly right here: the
 * worker uses Admin credentials and bypasses rules too, so this reproduces the
 * real write path rather than pretending a client wrote them.
 */
export async function wipe(): Promise<void> {
  await fetch(
    `http://${FIRESTORE_HOST}/emulator/v1/projects/${PROJECT_ID}/databases/(default)/documents`,
    { method: 'DELETE' },
  );
  // Accounts survive a Firestore wipe, so the second test's sign-up would fail
  // with EMAIL_EXISTS and every test after the first would be signed out.
  await fetch(`http://${AUTH_HOST}/emulator/v1/projects/${PROJECT_ID}/accounts`, {
    method: 'DELETE',
  });
}

type Value = string | number | boolean | null | Value[] | { [k: string]: Value };

/** Firestore's REST API wants values tagged with their type. */
function encode(value: Value): unknown {
  if (value === null) return { nullValue: null };
  if (typeof value === 'string') return { stringValue: value };
  if (typeof value === 'boolean') return { booleanValue: value };
  if (typeof value === 'number') {
    return Number.isInteger(value)
      ? { integerValue: String(value) }
      : { doubleValue: value };
  }
  if (Array.isArray(value)) {
    return { arrayValue: { values: value.map(encode) } };
  }
  return { mapValue: { fields: encodeFields(value) } };
}

function encodeFields(data: Record<string, Value>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(data).map(([k, v]) => [k, encode(v)]));
}

export async function write(path: string, data: Record<string, Value>): Promise<void> {
  const response = await fetch(`${BASE}/${path}`, {
    method: 'PATCH',
    headers: {
      'Content-Type': 'application/json',
      // The emulator's REST API DOES enforce security rules — unlike the Admin
      // SDK, which bypasses them. `Bearer owner` is the emulator's documented
      // way to write as an administrator, which is what the real worker is.
      Authorization: 'Bearer owner',
    },
    body: JSON.stringify({ fields: encodeFields(data) }),
  });
  if (!response.ok) {
    throw new Error(`seed failed for ${path}: ${response.status} ${await response.text()}`);
  }
}

export async function readDoc(path: string): Promise<Record<string, unknown> | null> {
  const response = await fetch(`${BASE}/${path}`, {
    headers: { Authorization: 'Bearer owner' },
  });
  if (!response.ok) return null;
  const body = (await response.json()) as { fields?: Record<string, unknown> };
  return body.fields ?? null;
}

/**
 * A port nothing listens on, so the "no playable URL" path is deterministic.
 * Port 1 is reserved and never bindable in practice.
 */
export const UNREACHABLE_LOCAL_SERVER = 'http://127.0.0.1:1';

export const EMAIL = 'e2e@example.com';
export const PASSWORD = 'clipforge-e2e';

/**
 * Sign in against the Auth emulator, returning the uid that was created.
 *
 * The uid is read back rather than dictated: the emulator assigns it, and
 * seeding documents against a uid we merely hoped for is how a suite ends up
 * silently testing an empty queue.
 *
 * Not a popup — that would exercise Google's OAuth flow, which is not ours and
 * does not run against the emulator. Not a storage injection either: Firebase
 * persists to IndexedDB, so writing localStorage achieves nothing.
 */
export async function signIn(page: Page): Promise<string> {
  // Pin the local-server origin per test rather than letting the result depend
  // on whether a worker happens to be running on this machine. A real worker on
  // 8765 would otherwise flip the playback fallback and fail an unrelated test —
  // which is exactly what it did the first time this suite ran.
  await page.addInitScript((origin) => {
    (window as unknown as { __clipforge?: Record<string, unknown> }).__clipforge = {
      ...((window as unknown as { __clipforge?: Record<string, unknown> }).__clipforge ?? {}),
      localServerOrigin: origin,
    };
  }, UNREACHABLE_LOCAL_SERVER);

  const response = await fetch(
    `http://${AUTH_HOST}/identitytoolkit.googleapis.com/v1/accounts:signUp?key=demo-key`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: EMAIL, password: PASSWORD, returnSecureToken: true }),
    },
  );
  if (!response.ok) {
    throw new Error(`could not create the test account: ${await response.text()}`);
  }
  const account = (await response.json()) as { localId: string };

  // Approval gating: a signed-in account with no `users/{uid}` row can read
  // nothing, so without this every test would land on the waiting screen. Written
  // as the worker would — the client is not allowed to approve itself, which is
  // the whole point of the gate.
  await write(`users/${account.localId}`, {
    uid: account.localId,
    email: EMAIL,
    displayName: 'E2E User',
    photoUrl: null,
    role: 'ADMIN',
    status: 'APPROVED',
    createdAt: NOW,
    decidedAt: NOW,
    decidedBy: account.localId,
  });

  await page.goto('/review');
  await page.waitForFunction(
    () => '__clipforgeSignIn' in (window as unknown as Record<string, unknown>),
  );
  await page.evaluate(
    ([email, password]) =>
      (
        window as unknown as {
          __clipforgeSignIn: (e: string, p: string) => Promise<unknown>;
        }
      ).__clipforgeSignIn(email, password),
    [EMAIL, PASSWORD] as const,
  );
  await page.waitForSelector('nav', { timeout: 15_000 });
  return account.localId;
}

const NOW = '2026-09-08T12:00:00.000Z';

export function job(uid: string, overrides: Record<string, Value> = {}): Record<string, Value> {
  return {
    id: 'job-1',
    uid,
    type: 'CLIP',
    status: 'RUNNING',
    submission: 'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
    sourceId: null,
    stages: [
      { name: 'DOWNLOAD', lane: 'CPU', status: 'DONE', attempts: 1 },
      { name: 'TRANSCRIBE', lane: 'GPU', status: 'RUNNING', attempts: 1 },
      { name: 'ANALYZE', lane: 'GPU', status: 'PENDING', attempts: 0 },
      { name: 'RENDER', lane: 'CPU', status: 'PENDING', attempts: 0 },
    ],
    workerId: 'e2e-worker',
    leaseExpiresAt: null,
    attempts: 0,
    maxAttempts: 3,
    error: null,
    createdAt: NOW,
    updatedAt: NOW,
    startedAt: NOW,
    endedAt: null,
    ...overrides,
  };
}

export function clip(uid: string, overrides: Record<string, Value> = {}): Record<string, Value> {
  return {
    id: 'clip-1',
    uid,
    candidateId: 'cand-1',
    sourceId: 'src-1',
    jobId: 'job-1',
    location: 'LOCAL',
    localPath: 'P:/workspace/clips/user/clip-1.mp4',
    playbackUrl: null,
    storagePath: null,
    thumbnailPath: null,
    durationSec: 38.2,
    widthPx: 1080,
    heightPx: 1920,
    sizeBytes: 4_200_000,
    renderProfile: 'default:v1',
    title: 'Most developers never realise this',
    description: 'Strong hook with a clear payoff inside forty seconds.',
    review: 'PENDING',
    reviewedAt: null,
    rights: null,
    createdAt: NOW,
    ...overrides,
  };
}

export function candidate(uid: string, overrides: Record<string, Value> = {}): Record<string, Value> {
  return {
    id: 'cand-1',
    uid,
    sourceId: 'src-1',
    jobId: 'job-1',
    startSec: 142.4,
    endSec: 180.6,
    proposedStartSec: null,
    proposedEndSec: null,
    subScores: {
      hook: 22,
      curiosity: 18,
      standalone: 19,
      emotion: 11,
      pacing: 8,
      shareability: 9,
    },
    total: 87,
    hook: 'Most developers never realise this',
    reason: 'Strong hook with a clear payoff inside forty seconds.',
    transcriptExcerpt: 'The thing nobody tells you about running your own hardware…',
    modelVersion: 'ollama:qwen3.5:4b',
    promptVersion: 'v1',
    createdAt: NOW,
    ...overrides,
  };
}

/** A 1x1 JPEG, base64 — enough to prove the poster renders. */
export const TINY_JPEG =
  '/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a' +
  'HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA' +
  'AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==';

export function preview(overrides: Record<string, Value> = {}): Record<string, Value> {
  return {
    clipId: 'clip-1',
    posterBase64: TINY_JPEG,
    filmstripBase64: null,
    widthPx: 360,
    heightPx: 640,
    byteSize: TINY_JPEG.length,
    createdAt: NOW,
    ...overrides,
  };
}
