import { describe, expect, it } from 'vitest';

import {
  DEFAULT_BRIEF,
  DURATION_PRESETS,
  briefFromTrend,
  briefProblems,
  describeBrief,
  presetFor,
  toClipOptions,
} from './clip-brief';

/**
 * The brief the Jobs page writes, and the sentences it reads back.
 *
 * `toClipOptions` matters most: an untouched panel has to write null, so a
 * job submitted without opening it is the same document as one submitted
 * before the panel existed.
 */

describe('toClipOptions', () => {
  it('writes nothing when nothing was touched', () => {
    expect(toClipOptions(DEFAULT_BRIEF)).toBeNull();
    expect(toClipOptions({ ...DEFAULT_BRIEF, instructions: '   ' })).toBeNull();
  });

  it('writes the whole brief once anything is', () => {
    expect(toClipOptions({ ...DEFAULT_BRIEF, instructions: ' the goals ' })).toEqual({
      instructions: 'the goals',
      maxClips: 5,
      minDurationSec: 15,
      maxDurationSec: 75,
    });
    expect(toClipOptions({ ...DEFAULT_BRIEF, maxClips: 3 })).toEqual({
      instructions: null,
      maxClips: 3,
      minDurationSec: 15,
      maxDurationSec: 75,
    });
  });
});

describe('presets', () => {
  it('name the pair they set, and custom for any other', () => {
    for (const preset of DURATION_PRESETS) {
      expect(presetFor(preset.min, preset.max)).toBe(preset.key);
    }
    expect(presetFor(15, 76)).toBe('custom');
  });

  it('every preset is a range the rules accept', () => {
    for (const preset of DURATION_PRESETS) {
      expect(preset.min).toBeGreaterThanOrEqual(5);
      expect(preset.max).toBeLessThanOrEqual(180);
      expect(preset.min).toBeLessThanOrEqual(preset.max);
    }
  });
});

describe('briefProblems', () => {
  it('is empty for the defaults', () => {
    expect(briefProblems(DEFAULT_BRIEF)).toEqual([]);
  });

  it('names what the rules would refuse', () => {
    expect(briefProblems({ ...DEFAULT_BRIEF, maxClips: 0 })).toEqual([
      'Ask for between 1 and 20 clips.',
    ]);
    expect(briefProblems({ ...DEFAULT_BRIEF, minDurationSec: 60, maxDurationSec: 30 })).toEqual([
      'The shortest length is longer than the longest.',
    ]);
    expect(briefProblems({ ...DEFAULT_BRIEF, maxDurationSec: 600 })).toEqual([
      'Clip lengths have to be between 5 and 180 seconds.',
    ]);
    expect(briefProblems({ ...DEFAULT_BRIEF, instructions: 'x'.repeat(1001) })).toEqual([
      'The brief is longer than 1000 characters.',
    ]);
  });
});

describe('describeBrief', () => {
  it('is one line, and nothing for a job without one', () => {
    expect(describeBrief(null)).toBeNull();
    expect(describeBrief(undefined)).toBeNull();
    expect(
      describeBrief({
        instructions: 'the goals',
        maxClips: 3,
        minDurationSec: 20,
        maxDurationSec: 45,
      }),
    ).toBe('up to 3 clips · 20–45 s · “the goals”');
    expect(describeBrief({ maxClips: 1 })).toBe('up to 1 clip · 15–75 s');
  });
});

describe('briefFromTrend', () => {
  it('turns the model’s angle into the brief when it thought there was a clip', () => {
    expect(
      briefFromTrend({ angle: 'The winning goal, and the bench reacting.', worthClipping: true }),
    ).toEqual({
      instructions: 'The winning goal, and the bench reacting.',
      maxClips: 3,
      minDurationSec: 15,
      maxDurationSec: 75,
    });
  });

  it('writes no brief when the model doubted it, or said nothing', () => {
    expect(
      briefFromTrend({ angle: 'Missing evidence of any goal.', worthClipping: false }),
    ).toBeNull();
    expect(briefFromTrend({ angle: null, worthClipping: true })).toBeNull();
  });
});
