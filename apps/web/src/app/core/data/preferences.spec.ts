import type { ObscureRegion, Preference } from '@clipforge/contracts';
import { describe, expect, it } from 'vitest';

import { obscureRuleFrom, preferencesSpec } from './preferences';

/**
 * Two decisions worth pinning down.
 *
 * The bound on the query, because an unbounded listen over a collection the
 * worker writes to is a quota bill nobody notices until the day's reads are
 * gone. And the rule that decides whether accepting a preference also writes
 * onto the source, because both of its failure modes are silent: writing when
 * it should not puts rectangles on every future clip from a channel nobody
 * asked about, and not writing when it should leaves the reviewer being asked
 * again about a logo they have already decided on.
 */

const REGION: ObscureRegion = { xPct: 2, yPct: 3, wPct: 12, hPct: 8, label: 'channel bug' };

function preference(overrides: Partial<Preference> = {}): Preference {
  return {
    id: 'pref-1',
    uid: 'user-1',
    scope: 'SOURCE',
    sourceId: 'source-1',
    category: 'OBSCURE',
    lesson: "this channel's bug sits in the top left of every upload",
    status: 'PROPOSED',
    createdAt: '2026-09-17T09:00:00.000Z',
    defaults: { obscure: { auto: false, regions: [REGION] } },
    ...overrides,
  };
}

describe('preferencesSpec', () => {
  it('asks only for preferences in the status it was given', () => {
    expect(preferencesSpec('PROPOSED').where).toEqual([['status', '==', 'PROPOSED']]);
    expect(preferencesSpec('ACCEPTED').where).toEqual([['status', '==', 'ACCEPTED']]);
  });

  it('stays bounded at the fifty documents this listener has always delivered', () => {
    expect(preferencesSpec('PROPOSED').limit).toBe(50);
  });

  it('sorts nothing, because an ordered version of it would need an index that is not deployed', () => {
    expect(preferencesSpec('PROPOSED').orderBy).toBeUndefined();
  });
});

describe('obscureRuleFrom', () => {
  it("makes an accepted preference's rectangles a property of its source", () => {
    const rule = obscureRuleFrom(preference(), 'ACCEPTED');

    expect(rule?.sourceId).toBe('source-1');
    expect(rule?.obscure.regions).toEqual([REGION]);
  });

  it('leaves detection off, since the boxes it would look for are already known', () => {
    expect(obscureRuleFrom(preference(), 'ACCEPTED')?.obscure.auto).toBe(false);
  });

  it('names no method or strength, so the pipeline still chooses how to hide them', () => {
    const rule = obscureRuleFrom(preference(), 'ACCEPTED');

    expect(rule?.obscure.method).toBeNull();
    expect(rule?.obscure.strength).toBeNull();
  });

  it('writes nothing onto the source when the preference is turned down', () => {
    expect(obscureRuleFrom(preference(), 'REJECTED')).toBeNull();
  });

  it('writes nothing onto the source for a preference that is only a sentence', () => {
    const lessonOnly = preference({
      category: 'FRAMING',
      lesson: "this channel's wide shots lose the ball unless the window follows it",
      defaults: { framingMode: 'TRACK' },
    });

    expect(obscureRuleFrom(lessonOnly, 'ACCEPTED')).toBeNull();
  });

  it('writes nothing onto the source when the rectangles list arrived empty', () => {
    const noRegions = preference({ defaults: { obscure: { auto: true, regions: [] } } });

    expect(obscureRuleFrom(noRegions, 'ACCEPTED')).toBeNull();
  });

  it('writes nothing onto the source for an EVERYTHING preference, which names none', () => {
    const unattached = preference({ scope: 'EVERYTHING', sourceId: null });

    expect(obscureRuleFrom(unattached, 'ACCEPTED')).toBeNull();
  });
});
