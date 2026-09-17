import { describe, expect, it } from 'vitest';

import { accountsSpec, decisionPatch, ownProfilePatch, type AccountDecision } from './admin';

/**
 * The account-decision writes.
 *
 * Approving an account hands over the entire library, so the write that does it
 * is worth pinning down: it records who decided, it may not be told who
 * decided, and it must not reach any field beyond the ones the rules allow. All
 * three fail silently in the wrong direction — a forged `decidedBy` looks like a
 * normal audit entry, and a stray key comes back as a permission error nobody
 * reading the UI can explain.
 */

describe('accountsSpec', () => {
  it('asks for the newest accounts first', () => {
    expect(accountsSpec().orderBy).toEqual([['createdAt', 'desc']]);
  });

  it('bounds the listen even though users is a small collection', () => {
    expect(accountsSpec().limit).toBe(200);
  });
});

describe('decisionPatch', () => {
  const AT = '2026-09-17T10:00:00.000Z';

  it('stamps who decided and when alongside the change', () => {
    expect(decisionPatch('admin-1', { status: 'APPROVED' }, AT)).toEqual({
      status: 'APPROVED',
      decidedAt: AT,
      decidedBy: 'admin-1',
    });
  });

  it('records the signed-in admin, not a decidedBy handed in with the change', () => {
    const forged = { status: 'APPROVED', decidedBy: 'someone-else' } as AccountDecision;

    expect(decisionPatch('admin-1', forged, AT)).toEqual({
      status: 'APPROVED',
      decidedAt: AT,
      decidedBy: 'admin-1',
    });
  });

  it('leaves out a field the caller did not decide rather than sending undefined', () => {
    const patch = decisionPatch('admin-1', { role: 'ADMIN' }, AT);

    expect(patch).not.toHaveProperty('status');
    expect(patch?.role).toBe('ADMIN');
  });

  it('changes status and role together when both were decided', () => {
    expect(decisionPatch('admin-1', { status: 'APPROVED', role: 'ADMIN' }, AT)).toEqual({
      status: 'APPROVED',
      role: 'ADMIN',
      decidedAt: AT,
      decidedBy: 'admin-1',
    });
  });

  it('refuses to stamp a decision that decides nothing', () => {
    expect(decisionPatch('admin-1', {}, AT)).toBeNull();
    expect(decisionPatch('admin-1', { status: undefined, role: undefined }, AT)).toBeNull();
  });
});

describe('ownProfilePatch', () => {
  it('carries the display name and nothing else', () => {
    const forged = { displayName: 'Ada', role: 'ADMIN', status: 'APPROVED' } as {
      displayName?: string;
    };

    expect(ownProfilePatch(forged)).toEqual({ displayName: 'Ada' });
  });

  it('is nothing to write when no name was given', () => {
    expect(ownProfilePatch({})).toBeNull();
  });
});
