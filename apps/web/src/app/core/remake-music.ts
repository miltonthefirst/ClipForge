import type { AppliedMusic } from '@clipforge/contracts';

/**
 * What a remake is about to do to the track already on the clip.
 *
 * The music is mixed into the rendered file — there is no separate audio layer
 * kept anywhere — so a remake cannot simply carry it across: it has to fetch
 * the track and mix it in again. Until it did, a reframe came back with the
 * music gone and the clip record still naming a soundtrack, which is the
 * report this was written for: "I remake the video with music and it disregards
 * my music."
 *
 * The remake now carries the track by default, and this is the part of saying
 * so that has reasoning in it — which sentence to put under the control, given
 * what they have typed into the rest of the form.
 *
 * It cannot predict the job, and does not try to. RemakeStage decides on
 * options the note interpretation has already rewritten, and that runs on the
 * worker, through a model, after this screen is gone: a note saying "start it
 * three seconds earlier" fills in a trim the form left at 0 and moves the
 * excerpt, and a note saying "put it in Spanish" produces a narration the form
 * never asked for. So the two things stated flatly here — the same excerpt, and
 * a replacing track nobody is about to contradict — are hedged the moment a
 * note is going out with the request. A wrong guess is not a broken render; it
 * is a promise made on this screen that the job then does not keep, which is
 * the more expensive kind.
 */

/** The parts of the remake form that decide what happens to the soundtrack. */
export interface RemakeMusicRequest {
  /**
   * The keep-the-music control, which is an opt-OUT: true is what an untouched
   * form sends.
   */
  readonly keepMusic: boolean;
  /** The trim nudges, in seconds, exactly as the two number inputs hold them. */
  readonly startDeltaSec: number;
  readonly endDeltaSec: number;
  /** Whether the *form* asks for a narration. A note can ask for one too. */
  readonly addsNarration: boolean;
  /**
   * The note, as typed — read for its length only, never for its meaning.
   * Anything in it can overrule the fields above once the worker interprets it,
   * so a non-empty note is the signal to stop stating and start hedging.
   */
  readonly notes: string;
}

export interface RemakeMusicOutlook {
  /** Whether the new version is being asked for with the track or without it. */
  readonly carried: boolean;
  /** One sentence for under the control, saying what to expect. */
  readonly summary: string;
  /**
   * Consequences that are true, intended, and would otherwise be found out by
   * watching the result — the reviewer approved a particular mix, and these are
   * the ways the new one will differ from it.
   */
  readonly warnings: readonly string[];
}

/**
 * The 50 ms the worker allows itself when deciding whether the length moved.
 *
 * A re-cut can land a frame either side of what was asked for, so an exact
 * comparison would call an unchanged clip changed and re-pick the excerpt for
 * nothing.
 */
const SAME_LENGTH_SEC = 0.05;

/**
 * What to say about this clip's music, or `null` when it has none and there is
 * nothing to say.
 *
 * Takes the clip's applied music rather than the whole clip so the caller
 * cannot pass one clip's form state with another clip's track.
 */
export function remakeMusicOutlook(
  music: AppliedMusic | null | undefined,
  request: RemakeMusicRequest,
): RemakeMusicOutlook | null {
  if (!music) return null;

  if (!request.keepMusic) {
    return {
      carried: false,
      // Says what the request is, not what the file will sound like. A mix
      // lives inside the rendered audio, so taking it out means rebuilding
      // that audio, and how much of it a remake rebuilds is decided on the
      // worker from options this screen never sees. Said flatly, "asks for the
      // new version without it" promised silence from a remake that copies the
      // reviewed audio across and leaves the track playing inside it.
      summary:
        'Asks for the new version without it: not fetched, not mixed in again, and not ' +
        'recorded on the new clip. The old mix is part of the rendered audio rather than a ' +
        'layer over it, so how much of the track actually leaves the file depends on how much ' +
        'of that audio this remake rebuilds. This clip keeps its own track either way, and one ' +
        'can be added to the new version afterwards.',
      warnings: [],
    };
  }

  // A note is not read here, only counted: the worker turns it into the very
  // fields this function is reasoning from, so its presence means none of them
  // are the last word.
  const interpreted = request.notes.trim().length > 0;
  const relengthed = changesLength(request);
  const warnings: string[] = [];

  // Only a change of LENGTH moves the excerpt. The stage reuses the recorded
  // start and tempo when the finished clip comes back the same length, so a
  // window nudged two seconds later at both ends gets the identical mix.
  if (relengthed) {
    warnings.push(
      'A different length means the section of the track is chosen again, so the music will ' +
        'start somewhere else in it than it does here.',
    );
  }

  // The contradiction the stage resolves by downgrading: REPLACE drops
  // everything else in the mix, and everything else now includes the narration
  // this remake exists to produce.
  if (music.mode === 'REPLACE' && request.addsNarration) {
    warnings.push(
      'This track replaces the audio, which would throw away the new narration. It will play ' +
        'under the voice instead, ducking whenever it speaks.',
    );
  } else if (music.mode === 'REPLACE' && interpreted) {
    // The same downgrade, reached without the voice control ever being
    // touched: a note naming a language makes the worker build a narration of
    // its own, and the page showed nothing at all about it.
    warnings.push(
      'If the note asks for a voice — a language to put this in, or a line to say — the track ' +
        'cannot replace the audio and play the new narration too. It would play under the ' +
        'voice instead, ducking whenever it speaks.',
    );
  }

  return { carried: true, summary: carriedSummary(relengthed, interpreted), warnings };
}

/** The sentence under the control when the track is being carried across. */
function carriedSummary(relengthed: boolean, interpreted: boolean): string {
  const mixed = 'The track is fetched and mixed into the new version';
  if (relengthed) return `${mixed}, at the level it has here.`;
  if (interpreted) {
    // The nudges say the window is not changing length, and the note is read
    // before the cut is made: "start it three seconds earlier" fills in the
    // trim they left at 0 and moves the excerpt about that far.
    return (
      `${mixed}, at the level it has here. Which section of it plays depends on what the note ` +
      'does to the length of the clip.'
    );
  }
  return `${mixed} — the same section of it, at the same level.`;
}

/**
 * Whether the nudges make the clip a different length, rather than moving it.
 *
 * Negative `startDeltaSec` starts earlier and positive `endDeltaSec` ends
 * later, so the length changes by the difference between them: +2 on both ends
 * slides the window two seconds later and is exactly as long as it was.
 */
function changesLength(request: RemakeMusicRequest): boolean {
  const length = (request.endDeltaSec || 0) - (request.startDeltaSec || 0);
  return Math.abs(length) > SAME_LENGTH_SEC;
}
