import type { Clip, RightsAttestation } from '@clipforge/contracts';
import { describe, expect, it } from 'vitest';

import {
  MAX_ATTESTATION_AGE_DAYS,
  RIGHTS_BASES,
  checkPublishable,
  draftIsComplete,
} from './rights';

/**
 * The UI's copy of the rights gate.
 *
 * These assert that it agrees with apps/worker/clipforge/publish/rights.py — the
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
    rights: null,
    createdAt: NOW.toISOString(),
    ...overrides,
  } as Clip;
}

function attestation(overrides: Partial<RightsAttestation> = {}): RightsAttestation {
  return {
    basis: 'OWN_CONTENT',
    attestedBy: 'user-1',
    attestedAt: NOW.toISOString(),
    note: null,
    ...overrides,
  } as RightsAttestation;
}

describe('checkPublishable', () => {
  it('passes a clip that is approved and properly attested', () => {
    expect(checkPublishable(clip({ rights: attestation() }), { now: NOW })).toBeNull();
  });

  it('refuses a clip with no attestation', () => {
    expect(checkPublishable(clip(), { now: NOW })?.code).toBe('NO_ATTESTATION');
  });

  it('refuses a clip that is not approved, however good its paperwork', () => {
    const pending = clip({ review: 'PENDING', rights: attestation() });
    expect(checkPublishable(pending, { now: NOW })?.code).toBe('NOT_APPROVED');
  });

  it('refuses an attestation with nobody against it', () => {
    const anon = clip({ rights: attestation({ attestedBy: null }) });
    expect(checkPublishable(anon, { now: NOW })?.code).toBe('NO_ATTESTOR');
  });

  it('refuses an undated attestation', () => {
    const undated = clip({ rights: attestation({ attestedAt: null }) });
    expect(checkPublishable(undated, { now: NOW })?.code).toBe('NO_ATTESTATION_TIME');
  });

  it('refuses an attestation older than a year, because rights change', () => {
    const old = new Date(NOW.getTime() - (MAX_ATTESTATION_AGE_DAYS + 1) * 86_400_000);
    const stale = clip({ rights: attestation({ attestedAt: old.toISOString() }) });
    expect(checkPublishable(stale, { now: NOW })?.code).toBe('ATTESTATION_STALE');
  });

  it('accepts an attestation exactly at the age limit', () => {
    const edge = new Date(NOW.getTime() - MAX_ATTESTATION_AGE_DAYS * 86_400_000);
    const boundary = clip({ rights: attestation({ attestedAt: edge.toISOString() }) });
    expect(checkPublishable(boundary, { now: NOW })).toBeNull();
  });

  it('refuses a bare fair-use assertion and accepts a reasoned one', () => {
    const bare = clip({ rights: attestation({ basis: 'FAIR_USE_ASSERTED' }) });
    expect(checkPublishable(bare, { now: NOW })?.code).toBe('FAIR_USE_NEEDS_REASONING');

    const reasoned = clip({
      rights: attestation({ basis: 'FAIR_USE_ASSERTED', note: '30s of a 90m lecture' }),
    });
    expect(checkPublishable(reasoned, { now: NOW })).toBeNull();
  });

  it('refuses a basis the contract does not define', () => {
    const invented = clip({ rights: attestation({ basis: 'PROBABLY_FINE' as never }) });
    expect(checkPublishable(invented, { now: NOW })?.code).toBe('UNKNOWN_BASIS');
  });
});

describe('the offered bases', () => {
  it('offers exactly the five the contract defines', () => {
    expect(RIGHTS_BASES.map((b) => b.value).sort()).toEqual([
      'FAIR_USE_ASSERTED',
      'LICENSED',
      'OWN_CONTENT',
      'PERMISSION_GRANTED',
      'PUBLIC_DOMAIN',
    ]);
  });

  it('gives every basis a hint, because none of them are self-explanatory', () => {
    for (const basis of RIGHTS_BASES) {
      expect(basis.hint.length).toBeGreaterThan(10);
    }
  });
});

describe('draftIsComplete', () => {
  it('requires a basis', () => {
    expect(draftIsComplete(null, 'anything')).toBe(false);
  });

  it('requires a note only for fair use', () => {
    expect(draftIsComplete('OWN_CONTENT', '')).toBe(true);
    expect(draftIsComplete('FAIR_USE_ASSERTED', '')).toBe(false);
    expect(draftIsComplete('FAIR_USE_ASSERTED', '   ')).toBe(false);
    expect(draftIsComplete('FAIR_USE_ASSERTED', 'transformative commentary')).toBe(true);
  });
});
