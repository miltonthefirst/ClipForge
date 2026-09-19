import type { Trend } from '@clipforge/contracts';
import { describe, expect, it } from 'vitest';

import {
  COMPOSE_LENGTHS,
  COMPOSE_VOICES,
  DEFAULT_COMPOSE,
  composeFromTrend,
  composeProblems,
  contextFromTrend,
  describeCompose,
} from './compose-plan';

/**
 * The request a trend becomes. What matters most is what goes where: the
 * curator's doubt is context the script must respect, not an angle it must
 * follow, and the person's own words travel verbatim or not at all.
 */

function trend(overrides: Partial<Trend> = {}): Trend {
  return {
    id: 'trend-1',
    uid: 'u1',
    jobId: 'job-research-1',
    topic: 'Australia could follow Canada into associated EU membership',
    rank: 12,
    score: 41,
    signals: [
      { source: 'GOOGLE_TRENDS', strength: 0.4, detail: '20K+ searches', url: null },
      { source: 'REDDIT', strength: 0.2, detail: 'r/europe · #14 today', url: null },
    ],
    videos: [
      {
        url: 'https://www.youtube.com/watch?v=omByeVY4_S8',
        externalId: 'omByeVY4_S8',
        title: 'Metsola tells Euronews',
        channel: 'euronews',
        durationSec: 298,
        viewCount: 1200,
        uploadedAt: null,
        thumbnailUrl: null,
        viewsPerHour: null,
        score: 30,
        via: 'YOUTUBE',
      },
    ],
    matchedTopics: ['europe'],
    angle: 'The evidence only confirms the topic exists.',
    relevance: 3,
    worthClipping: false,
    compilationTitle: null,
    curated: true,
    status: 'NEW',
    decidedAt: null,
    decidedBy: null,
    createdAt: '2026-09-19T12:00:00.000Z',
    ...overrides,
  } as Trend;
}

describe('contextFromTrend', () => {
  it('lists the evidence, the videos and the curator’s note as facts', () => {
    expect(contextFromTrend(trend())).toBe(
      [
        '- Google Trends · 20K+ searches',
        '- Reddit · r/europe · #14 today',
        '- Video: Metsola tells Euronews — euronews',
        "- The curator's note: The evidence only confirms the topic exists.",
        '- This channel is about: europe',
      ].join('\n'),
    );
  });

  it('stays within the contract’s limit however much the trend carries', () => {
    const long = trend({
      videos: Array.from({ length: 6 }, (_, i) => ({
        ...trend().videos[0]!,
        externalId: `v${i}`,
        title: 'x'.repeat(300),
      })) as Trend['videos'],
    });
    expect(contextFromTrend(long).length).toBeLessThanOrEqual(2000);
  });
});

describe('composeFromTrend', () => {
  it('keeps the doubt out of the angle and the person’s words verbatim', () => {
    const options = composeFromTrend(trend(), {
      ...DEFAULT_COMPOSE,
      angle: '  Explain what associated membership would mean.  ',
      script: '',
    });
    expect(options.topic).toBe('Australia could follow Canada into associated EU membership');
    expect(options.angle).toBe('Explain what associated membership would mean.');
    expect(options.script).toBeNull();
    expect(options.context).toContain("The curator's note");
    expect(options.trendId).toBe('trend-1');
    expect(options.style).toBe('STICK');
    expect(options.targetDurationSec).toBe(45);
    expect(options.voice).toBeNull();
  });

  it('seeds the same trend the same way, and different trends differently', () => {
    const a = composeFromTrend(trend(), DEFAULT_COMPOSE);
    const b = composeFromTrend(trend(), DEFAULT_COMPOSE);
    const c = composeFromTrend(trend({ id: 'trend-2' }), DEFAULT_COMPOSE);
    expect(a.seed).toBe(b.seed);
    expect(a.seed).not.toBe(c.seed);
    expect(a.seed).toBeGreaterThanOrEqual(0);
  });

  it('clamps the length to what the contract allows', () => {
    expect(
      composeFromTrend(trend(), { ...DEFAULT_COMPOSE, lengthSec: 500 }).targetDurationSec,
    ).toBe(90);
    expect(composeFromTrend(trend(), { ...DEFAULT_COMPOSE, lengthSec: 3 }).targetDurationSec).toBe(
      15,
    );
  });
});

describe('composeProblems', () => {
  it('is empty for the defaults and every offered length', () => {
    expect(composeProblems(DEFAULT_COMPOSE)).toEqual([]);
    for (const length of COMPOSE_LENGTHS) {
      expect(composeProblems({ ...DEFAULT_COMPOSE, lengthSec: length.seconds })).toEqual([]);
    }
  });

  it('names what the rules would refuse', () => {
    expect(composeProblems({ ...DEFAULT_COMPOSE, script: 'x'.repeat(2001) })).toEqual([
      'The script is longer than 2000 characters.',
    ]);
    expect(composeProblems({ ...DEFAULT_COMPOSE, angle: 'x'.repeat(501) })).toEqual([
      'The steer is longer than 500 characters.',
    ]);
    expect(composeProblems({ ...DEFAULT_COMPOSE, lengthSec: 5 })).toEqual([
      'A drawn video runs between 15 and 90 seconds.',
    ]);
  });
});

describe('the lists', () => {
  it('offer a default voice first and lengths the contract accepts', () => {
    expect(COMPOSE_VOICES[0]?.id).toBeNull();
    expect(COMPOSE_VOICES.length).toBeGreaterThanOrEqual(5);
    for (const length of COMPOSE_LENGTHS) {
      expect(length.seconds).toBeGreaterThanOrEqual(15);
      expect(length.seconds).toBeLessThanOrEqual(90);
    }
  });
});

describe('describeCompose', () => {
  it('says how many scenes, whose words, and which voice', () => {
    expect(describeCompose({ scenes: [1, 2, 3], voice: 'af_heart', scriptBy: 'MODEL' })).toBe(
      '3 drawn scenes · a script the model wrote · voice af_heart',
    );
    expect(describeCompose({ scenes: [1], voice: 'bm_george', scriptBy: 'OPERATOR' })).toBe(
      '1 drawn scene · your words · voice bm_george',
    );
    expect(describeCompose(null)).toBeNull();
  });
});
