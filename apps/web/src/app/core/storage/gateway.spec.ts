import { TestBed } from '@angular/core/testing';
import { deleteObject, getDownloadURL } from 'firebase/storage';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// The interesting cases here are all refusals, and a real client cannot be
// asked for one. Same shape as playback.spec.ts, which mocks it for the same
// reason.
vi.mock('firebase/storage', () => ({
  getDownloadURL: vi.fn(),
  deleteObject: vi.fn(),
  ref: vi.fn((_storage: unknown, path: string) => ({ fullPath: path })),
}));

import { FirebaseService } from '../firebase';
import { StorageGateway } from './gateway';

/**
 * Telling three storage failures apart.
 *
 * Which one it is decides three different things: whether a document may stop
 * pointing at a bucket copy, whether a viewer is shown a poster or an
 * explanation, and which explanation. Collapsing them is what the code this
 * replaces did, and it cost a release — every bucket read in the project was
 * denied by a missing IAM grant, and each refusal arrived looking like a clip
 * that had never been uploaded.
 */

/** A Firebase Storage error, which carries its reason in `code`. */
function storageError(code: string): Error & { code: string } {
  return Object.assign(new Error(code), { code });
}

let gateway: StorageGateway;

beforeEach(() => {
  vi.mocked(getDownloadURL).mockReset();
  vi.mocked(deleteObject).mockReset();
  TestBed.configureTestingModule({
    providers: [{ provide: FirebaseService, useValue: { storage: {} } }],
  });
  gateway = TestBed.inject(StorageGateway);
});

describe('classifying a storage failure', () => {
  // The classification is exercised through the public surface rather than
  // exported on its own: what matters is the answer `remove` and `downloadUrl`
  // give, not the shape of the function behind them.

  it('treats an object the lifecycle rule already removed as released', async () => {
    // Already collected is the common case and not a failure — the rule removes
    // the object after the retention window and reports to nobody — so the
    // document should stop pointing at it either way.
    vi.mocked(deleteObject).mockRejectedValue(storageError('storage/object-not-found'));
    await expect(gateway.remove('clips/a.mp4')).resolves.toBe(true);
  });

  it('keeps the pointer when the bucket refused the delete', async () => {
    // The bytes are still there. Clearing `storagePath` would leave a document
    // saying the copy is gone and a bucket still billing for it.
    vi.mocked(deleteObject).mockRejectedValue(storageError('storage/unauthorized'));
    await expect(gateway.remove('clips/a.mp4')).resolves.toBe(false);
  });

  it('keeps the pointer when the failure carries no code at all', async () => {
    vi.mocked(deleteObject).mockRejectedValue(new Error('network request failed'));
    await expect(gateway.remove('clips/a.mp4')).resolves.toBe(false);
  });

  it('reports a refusal and an absence as different things', async () => {
    vi.mocked(getDownloadURL).mockRejectedValueOnce(storageError('storage/unauthorized'));
    expect((await gateway.downloadUrl('clips/a.mp4')).refusal).toBe('denied');

    vi.mocked(getDownloadURL).mockRejectedValueOnce(storageError('storage/object-not-found'));
    expect((await gateway.downloadUrl('clips/a.mp4')).refusal).toBe('missing');
  });

  it('hands back the raw code, because the advice differs per code', async () => {
    // An over-quota project, a download worth retrying and a rules refusal are
    // all outside the three buckets and are three different sentences to the
    // person holding the phone. A gate that decides how much of an error its
    // callers may see has started making their decisions.
    vi.mocked(getDownloadURL).mockRejectedValue(storageError('storage/quota-exceeded'));
    const resolved = await gateway.downloadUrl('clips/a.mp4');

    expect(resolved.refusal).toBe('unknown');
    expect(resolved.code).toBe('storage/quota-exceeded');
  });
});
