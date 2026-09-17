import type { Clip } from '@clipforge/contracts';
import { describe, expect, it } from 'vitest';

import { checkPublishable } from './publishable';

/**
 * The UI's copy of the publish gate.
 *
 * These assert that it agrees with apps/worker/clipforge/publish/gate.py — the
 * same refusal codes for the same inputs. This copy is advisory and the worker's
 * is enforcing, so a disagreement is not a security hole; it is a user being
 * promised a publish that will then fail, which is its own kind of bad.
 */

const NOW = new Date('2026-06-01T12:00:00.000Z');

function clip(overrides: Partial<Clip> = {}): Clip {
  return {
    id: 'clip-1',
    uid: 'user-1',
    candidateId: 'cand-1',
    sourceId: 'src-1',
    jobId: 'job-1',
    location: 'LOCAL',
    localPath: 'P:/workspace/clips/clip-1.mp4',
    playbackUrl: null,
    storagePath: null,
    durationSec: 38,
    widthPx: 1080,
    heightPx: 1920,
    sizeBytes: 4096,
    renderProfile: 'default',
    title: 'A hook',
    description: 'why',
    review: 'APPROVED',
    createdAt: NOW.toISOString(),
    ...overrides,
  } as Clip;
}

describe('checkPublishable', () => {
  it('passes a clip somebody approved', () => {
    expect(checkPublishable(clip())).toBeNull();
  });

  it('refuses a clip nobody has watched yet', () => {
    const pending = clip({ review: 'PENDING' });
    expect(checkPublishable(pending)?.code).toBe('NOT_APPROVED');
  });

  it('refuses a clip somebody turned down', () => {
    const rejected = clip({ review: 'REJECTED' });
    expect(checkPublishable(rejected)?.code).toBe('NOT_APPROVED');
  });

  it('names the state it refused, so the message says what to do about it', () => {
    expect(checkPublishable(clip({ review: 'PENDING' }))?.message).toContain('PENDING');
  });

  it('asks nothing about rights, which is no longer a field', () => {
    // The removal, asserted rather than assumed. A clip carrying a stale
    // attestation from before the change is published on the same terms as one
    // that never had one: whether a person approved it.
    const legacy = clip({ rights: { basis: 'PUBLIC_DOMAIN' } } as Partial<Clip>);
    expect(checkPublishable(legacy)).toBeNull();
    expect(checkPublishable(clip())).toBeNull();
  });
});
