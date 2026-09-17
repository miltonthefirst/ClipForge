import type { AppliedMusic } from '@clipforge/contracts';
import { describe, expect, it } from 'vitest';

import { remakeMusicOutlook, type RemakeMusicRequest } from './remake-music';

/**
 * What the remake form promises about the music.
 *
 * Worth testing because the promise is the whole point: the bug this came from
 * was a remade clip that had lost its soundtrack and still said it had one, and
 * a form that says "the track is carried over" and is wrong about it is the
 * same lie moved one screen earlier. Half of these cases are therefore about
 * what it must NOT say — the worker rewrites the request from the note before
 * it decides any of this, so anything stated flatly here is a guess.
 */

function music(overrides: Partial<AppliedMusic> = {}): AppliedMusic {
  return {
    mode: 'BED',
    captions: 'KEEP',
    source: 'https://www.youtube.com/watch?v=abcdefghijk',
    trackTitle: 'Slow Burn',
    tempoBpm: 92,
    musicStartSec: 41.5,
    gainDb: -6,
    alignToBeat: true,
    ...overrides,
  };
}

function request(overrides: Partial<RemakeMusicRequest> = {}): RemakeMusicRequest {
  return {
    keepMusic: true,
    startDeltaSec: 0,
    endDeltaSec: 0,
    addsNarration: false,
    notes: '',
    ...overrides,
  };
}

describe('remakeMusicOutlook', () => {
  it('has nothing to say about a clip that never had a track', () => {
    expect(remakeMusicOutlook(null, request())).toBeNull();
    expect(remakeMusicOutlook(undefined, request())).toBeNull();
  });

  it('carries the track when the reviewer leaves the control alone', () => {
    const outlook = remakeMusicOutlook(music(), request());
    expect(outlook?.carried).toBe(true);
    expect(outlook?.warnings).toEqual([]);
  });

  it('promises the same section and the same level when nothing moves', () => {
    const outlook = remakeMusicOutlook(music(), request());
    expect(outlook?.summary).toContain('the same section');
    expect(outlook?.summary).toContain('same level');
  });

  it('asks for a version without the track when the reviewer opts out', () => {
    const outlook = remakeMusicOutlook(music(), request({ keepMusic: false }));
    expect(outlook?.carried).toBe(false);
    expect(outlook?.summary).toContain('without it');
  });

  it('says the clip being remade keeps its own track whichever way they choose', () => {
    const outlook = remakeMusicOutlook(music(), request({ keepMusic: false }));
    expect(outlook?.summary).toContain('This clip keeps its own track');
  });

  it('limits the opt-out to what it is: not fetched, not mixed in, not recorded', () => {
    const outlook = remakeMusicOutlook(music(), request({ keepMusic: false }));
    expect(outlook?.summary).toContain('not fetched, not mixed in again');
    expect(outlook?.summary).toContain('not recorded on the new clip');
  });

  it('does not promise the old mix leaves the file, which only rebuilt audio loses', () => {
    // The stage can hand the reviewed audio straight on, and a track baked into
    // it goes on playing under a clip whose record no longer names one. Which
    // of those two paths a request takes is decided after this screen is gone.
    const outlook = remakeMusicOutlook(music(), request({ keepMusic: false }));
    expect(outlook?.summary).toContain('how much of that audio this remake rebuilds');
  });

  it('warns that the music will start elsewhere when the clip gets longer', () => {
    const outlook = remakeMusicOutlook(music(), request({ endDeltaSec: 4 }));
    expect(outlook?.warnings.some((w) => w.includes('start somewhere else'))).toBe(true);
  });

  it('warns the same way when the clip gets shorter', () => {
    const outlook = remakeMusicOutlook(music(), request({ startDeltaSec: 3 }));
    expect(outlook?.warnings.some((w) => w.includes('start somewhere else'))).toBe(true);
  });

  it('stays quiet when the nudges slide the window without changing its length', () => {
    // Both ends two seconds later: a different two seconds of the match, and
    // exactly as long — so the stage reuses the start it recorded and the mix
    // is the one the reviewer approved.
    const outlook = remakeMusicOutlook(music(), request({ startDeltaSec: 2, endDeltaSec: 2 }));
    expect(outlook?.warnings).toEqual([]);
    expect(outlook?.summary).toContain('the same section');
  });

  it('does not claim the same section once the length has changed', () => {
    const outlook = remakeMusicOutlook(music(), request({ endDeltaSec: 4 }));
    expect(outlook?.summary).not.toContain('the same section');
    expect(outlook?.summary).toContain('at the level it has here');
  });

  it('ignores a nudge smaller than the tolerance the worker re-picks on', () => {
    const outlook = remakeMusicOutlook(music(), request({ endDeltaSec: 0.02 }));
    expect(outlook?.warnings).toEqual([]);
  });

  it('says a replacing track will play under a narration this remake adds', () => {
    const outlook = remakeMusicOutlook(
      music({ mode: 'REPLACE' }),
      request({ addsNarration: true }),
    );
    expect(outlook?.warnings.some((w) => w.includes('under the voice'))).toBe(true);
  });

  it('leaves a replacing track alone when the remake adds no narration', () => {
    const outlook = remakeMusicOutlook(music({ mode: 'REPLACE' }), request());
    expect(outlook?.warnings).toEqual([]);
  });

  it('says nothing about a bed giving way, because a bed never has to', () => {
    const outlook = remakeMusicOutlook(music({ mode: 'BED' }), request({ addsNarration: true }));
    expect(outlook?.warnings).toEqual([]);
  });

  it('gives both warnings when a longer clip is also getting a new voice', () => {
    const outlook = remakeMusicOutlook(
      music({ mode: 'REPLACE' }),
      request({ endDeltaSec: 6, addsNarration: true }),
    );
    expect(outlook?.warnings).toHaveLength(2);
  });

  it('warns about nothing once the track is being dropped, however the cut moves', () => {
    const outlook = remakeMusicOutlook(
      music({ mode: 'REPLACE' }),
      request({ keepMusic: false, endDeltaSec: 6, addsNarration: true }),
    );
    expect(outlook?.warnings).toEqual([]);
  });

  // ── What a note does to all of the above ─────────────────────────────────
  // The worker reads the note into the same options this screen is reasoning
  // from, so a note-only remake — the documented normal use — arrives here
  // looking like a request that changes nothing.

  it('stops claiming the same section when a note is going out with the request', () => {
    // The verified failure: "start it three seconds earlier" with both nudges
    // untouched. The page said the same section at the same level; the worker
    // filled the trim in from the note and moved the excerpt about three
    // seconds.
    const outlook = remakeMusicOutlook(
      music(),
      request({ notes: 'start it three seconds earlier' }),
    );
    expect(outlook?.summary).not.toContain('the same section');
    expect(outlook?.summary).toContain('what the note does to the length of the clip');
  });

  it('still says the section is the same when the note box holds only spaces', () => {
    const outlook = remakeMusicOutlook(music(), request({ notes: '   \n ' }));
    expect(outlook?.summary).toContain('the same section');
  });

  it('warns that a note asking for a voice would downgrade a replacing track', () => {
    // The other verified failure: "put it in Spanish" on a REPLACE clip with
    // the voice control off. A narration runs, so the stage drops the track to
    // a bed, and the page said nothing about it at all.
    const outlook = remakeMusicOutlook(
      music({ mode: 'REPLACE' }),
      request({ notes: 'put it in Spanish' }),
    );
    expect(outlook?.warnings.some((w) => w.includes('If the note asks for a voice'))).toBe(true);
  });

  it('says the downgrade once, not twice, when the form asks for the voice as well', () => {
    const outlook = remakeMusicOutlook(
      music({ mode: 'REPLACE' }),
      request({ addsNarration: true, notes: 'put it in Spanish' }),
    );
    expect(outlook?.warnings).toHaveLength(1);
    expect(outlook?.warnings[0]).toContain('would throw away the new narration');
  });

  it('adds no warning for a bed, which no note can put in contradiction', () => {
    const outlook = remakeMusicOutlook(music({ mode: 'BED' }), request({ notes: 'brighten it' }));
    expect(outlook?.warnings).toEqual([]);
  });

  it('keeps the definite excerpt warning when the nudges already change the length', () => {
    const outlook = remakeMusicOutlook(music(), request({ endDeltaSec: 4, notes: 'tighten it' }));
    expect(outlook?.warnings.some((w) => w.includes('start somewhere else'))).toBe(true);
    expect(outlook?.summary).toContain('at the level it has here');
    expect(outlook?.summary).not.toContain('the note');
  });

  it('says nothing new about a note once the track is being dropped', () => {
    const outlook = remakeMusicOutlook(
      music({ mode: 'REPLACE' }),
      request({ keepMusic: false, notes: 'put it in Spanish' }),
    );
    expect(outlook?.carried).toBe(false);
    expect(outlook?.warnings).toEqual([]);
  });
});
