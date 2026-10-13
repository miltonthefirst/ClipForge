import { describe, expect, it } from 'vitest';

import {
  MAX_PARTS,
  clock,
  describeParts,
  evenSplits,
  nextPart,
  parseTime,
  reachesOutside,
  splitProblems,
  splitWindow,
} from './remake-split';

/**
 * The remake form's parts list.
 *
 * Asserted rather than eyeballed because every mistake here is paid for after
 * a render: a part the worker refuses fails the job once it has already
 * re-cut and narrated, and a time read wrongly produces a clip that starts in
 * the wrong place with nothing on screen to say why.
 */

describe('clock', () => {
  it('reads the way a player shows a time', () => {
    expect(clock(0)).toBe('0:00');
    expect(clock(35)).toBe('0:35');
    expect(clock(50.7)).toBe('0:50.7');
    expect(clock(62.5)).toBe('1:02.5');
    expect(clock(-5)).toBe('-0:05');
  });
});

describe('parseTime', () => {
  it('accepts seconds and minutes-and-seconds alike', () => {
    expect(parseTime('35')).toBe(35);
    expect(parseTime('35.5')).toBe(35.5);
    expect(parseTime('0:35')).toBe(35);
    expect(parseTime('1:02.5')).toBe(62.5);
    expect(parseTime(' -5 ')).toBe(-5);
  });

  it('refuses anything it would have to guess at', () => {
    expect(parseTime('')).toBeNull();
    expect(parseTime('0:3x')).toBeNull();
    expect(parseTime('1:75')).toBeNull();
    expect(parseTime('thirty')).toBeNull();
  });
});

describe('evenSplits', () => {
  it('cuts a clip into equal parts that meet exactly and end at the end', () => {
    expect(evenSplits(60, 3)).toEqual([
      { startSec: 0, endSec: 20 },
      { startSec: 20, endSec: 40 },
      { startSec: 40, endSec: 60 },
    ]);
  });

  it('ends the last part at the true length, not a rounded one', () => {
    const parts = evenSplits(50.7, 2);
    expect(parts.at(-1)?.endSec).toBe(50.7);
    expect(parts[0].endSec).toBe(parts[1].startSec);
  });

  it('never makes more parts than the rules allow', () => {
    expect(evenSplits(600, 50)).toHaveLength(MAX_PARTS);
  });
});

describe('nextPart', () => {
  it('starts with the whole clip, and then continues from where the last part ends', () => {
    expect(nextPart([], 50.7)).toEqual({ startSec: 0, endSec: 50.7 });
    expect(nextPart([{ startSec: 0, endSec: 35 }], 50.7)).toEqual({ startSec: 35, endSec: 50.7 });
  });

  it('still gives a cuttable part when the last one already reaches the end', () => {
    const next = nextPart([{ startSec: 0, endSec: 50.7 }], 50.7);
    expect(next.endSec - next.startSec).toBeGreaterThanOrEqual(1);
  });
});

describe('splitProblems', () => {
  it('passes the request that started this', () => {
    expect(
      splitProblems(
        [
          { startSec: 0, endSec: 35 },
          { startSec: 35, endSec: 50.7 },
        ],
        50.7,
      ),
    ).toEqual([]);
  });

  it('names the part that cannot be cut, as the worker would refuse it', () => {
    expect(splitProblems([{ startSec: 10, endSec: 10.5 }], 50)).toEqual([
      'Part 1 needs to end at least a second after it starts.',
    ]);
    expect(splitProblems([{ startSec: -40, endSec: 10 }], 50)[0]).toContain('before the clip');
    expect(splitProblems([{ startSec: 0, endSec: 90 }], 50)[0]).toContain('after the clip');
  });

  it('refuses more parts than the rules allow', () => {
    const many = Array.from({ length: MAX_PARTS + 1 }, (_, i) => ({
      startSec: i,
      endSec: i + 1,
    }));
    expect(splitProblems(many, 60)[0]).toContain(`At most ${MAX_PARTS}`);
  });
});

describe('describeParts', () => {
  it('says how many clips, in the order they will be numbered', () => {
    expect(
      describeParts([
        { startSec: 35, endSec: 50.7 },
        { startSec: 0, endSec: 35 },
      ]),
    ).toBe('2 clips: 0:00–0:35 and 0:35–0:50.7');
  });

  it('calls a single range what it is', () => {
    expect(describeParts([{ startSec: 5, endSec: 20 }])).toBe('One clip, cut to 0:05–0:20');
  });
});

describe('splitWindow and reachesOutside', () => {
  it('leaves the window alone for parts inside the clip', () => {
    const inside = [
      { startSec: 5, endSec: 20 },
      { startSec: 25, endSec: 40 },
    ];
    expect(splitWindow(inside, 50)).toEqual({ startDeltaSec: 0, endDeltaSec: 0 });
    expect(reachesOutside(inside, 50)).toBe(false);
  });

  it('widens it for a part that reaches outside, which then needs the source', () => {
    const outside = [
      { startSec: -5, endSec: 20 },
      { startSec: 20, endSec: 60 },
    ];
    expect(splitWindow(outside, 50)).toEqual({ startDeltaSec: -5, endDeltaSec: 10 });
    expect(reachesOutside(outside, 50)).toBe(true);
  });

  it('does not count a rounded edge as reaching outside, as the worker does not', () => {
    expect(reachesOutside([{ startSec: -0.4, endSec: 51 }], 50.7)).toBe(false);
    expect(splitWindow([{ startSec: 0, endSec: 51 }], 50.7)).toEqual({
      startDeltaSec: 0,
      endDeltaSec: 0,
    });
  });
});
