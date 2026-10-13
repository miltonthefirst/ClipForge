import type { SplitRange } from '@clipforge/contracts';

/**
 * Splitting one clip into several, as the remake form states it.
 *
 * Parts are start-to-end ranges in seconds on the clip as the reviewer watches
 * it — the only timeline this screen has. The worker applies every other
 * correction first and cuts the finished file, so each part keeps the framing,
 * captions, voice and music of the whole; see `clipforge.stages.splitting`,
 * whose checks {@link splitProblems} mirrors so a request that cannot be cut is
 * stopped here rather than failing after a render.
 *
 * Written for a request that had nowhere to go: "split this into two, beginning
 * to 35s then 35s to the end" was bent into a trim and came back as one longer
 * clip.
 */

/** firestore.rules and the stage cap the list at the same number. */
export const MAX_PARTS = 20;
/** The shortest part the stage will cut. */
export const MIN_PART_SEC = 1;
/** How far a part may reach outside the clip: the reach of a trim. */
export const REACH_SEC = 30;
/**
 * The worker's tolerances for an edge that was meant to be the clip's own:
 * wider outward, because a part ending 0.3s past the clip is a rounded length.
 */
const SNAP_OUT_SEC = 1;
const SNAP_IN_SEC = 0.5;

/** A part with its edges pulled onto the clip's, exactly as the worker does. */
function snapped(part: SplitRange, durationSec: number): SplitRange {
  const startSec =
    part.startSec >= -SNAP_OUT_SEC && part.startSec <= SNAP_IN_SEC ? 0 : part.startSec;
  const endSec =
    part.endSec >= durationSec - SNAP_IN_SEC && part.endSec <= durationSec + SNAP_OUT_SEC
      ? durationSec
      : part.endSec;
  return { startSec, endSec };
}

/** 0:35, 1:02.5, -0:05 — the same format the worker writes its messages in. */
export function clock(seconds: number): string {
  const sign = seconds < 0 ? '-' : '';
  const tenthsTotal = Math.round(Math.abs(seconds) * 10);
  const minutes = Math.floor(tenthsTotal / 600);
  const tenthsLeft = tenthsTotal % 600;
  const whole = Math.floor(tenthsLeft / 10);
  const tenths = tenthsLeft % 10;
  const text = `${minutes}:${String(whole).padStart(2, '0')}`;
  return sign + (tenths ? `${text}.${tenths}` : text);
}

/**
 * A typed time, in seconds: `35`, `35.5`, `0:35`, `1:02.5`, `-5`.
 *
 * Null for anything else, so the field can put back what it held rather than
 * sending a part that starts at NaN.
 */
export function parseTime(text: string): number | null {
  const trimmed = text.trim();
  const match = /^(-)?(?:(\d+):)?(\d+(?:\.\d+)?)$/.exec(trimmed);
  if (!match) return null;
  const [, minus, minutes, seconds] = match;
  if (minutes !== undefined && Number(seconds) >= 60) return null;
  const value = Number(minutes ?? 0) * 60 + Number(seconds);
  return Math.round((minus ? -value : value) * 10) / 10;
}

/** `count` equal parts of a clip `durationSec` long, to a tenth of a second. */
export function evenSplits(durationSec: number, count: number): SplitRange[] {
  const parts = Math.max(1, Math.min(MAX_PARTS, Math.floor(count)));
  const edges = Array.from({ length: parts + 1 }, (_, i) =>
    i === parts ? durationSec : Math.round(((durationSec * i) / parts) * 10) / 10,
  );
  return edges.slice(0, -1).map((startSec, i) => ({ startSec, endSec: edges[i + 1] }));
}

/**
 * The part the Add button appends: from where the last one ends to the end of
 * the clip, which is what "and then the rest" means. From the start, when
 * there is nothing yet.
 */
export function nextPart(parts: readonly SplitRange[], durationSec: number): SplitRange {
  const last = parts.at(-1);
  if (!last) return { startSec: 0, endSec: durationSec };
  const startSec = Math.min(last.endSec, Math.max(0, durationSec - MIN_PART_SEC));
  return { startSec, endSec: Math.max(durationSec, startSec + MIN_PART_SEC) };
}

/** Whether any part reaches outside the clip, which needs the original video. */
export function reachesOutside(parts: readonly SplitRange[], durationSec: number): boolean {
  const window = splitWindow(parts, durationSec);
  return window.startDeltaSec < 0 || window.endDeltaSec > 0;
}

/** Everything wrong with the parts, one sentence each. Empty means cuttable. */
export function splitProblems(parts: readonly SplitRange[], durationSec: number): string[] {
  const problems: string[] = [];
  if (parts.length > MAX_PARTS) {
    problems.push(`At most ${MAX_PARTS} parts; there are ${parts.length}.`);
  }
  parts.forEach((asked, i) => {
    const part = snapped(asked, durationSec);
    const label = `Part ${i + 1}`;
    if (part.endSec - part.startSec < MIN_PART_SEC) {
      problems.push(`${label} needs to end at least a second after it starts.`);
    }
    if (part.startSec < -REACH_SEC) {
      problems.push(`${label} starts more than ${REACH_SEC}s before the clip does.`);
    }
    if (part.endSec > durationSec + REACH_SEC) {
      problems.push(`${label} ends more than ${REACH_SEC}s after the clip does.`);
    }
  });
  return problems;
}

/** "2 clips: 0:00–0:35 and 0:35–0:50.7" — what pressing Remake will make. */
export function describeParts(parts: readonly SplitRange[]): string {
  const ordered = [...parts].sort((a, b) => a.startSec - b.startSec || a.endSec - b.endSec);
  const ranges = ordered.map((part) => `${clock(part.startSec)}–${clock(part.endSec)}`);
  if (ordered.length === 1) return `One clip, cut to ${ranges[0]}`;
  const listed =
    ranges.length > 2
      ? `${ranges.slice(0, -1).join(', ')} and ${ranges.at(-1)}`
      : ranges.join(' and ');
  return `${ordered.length} clips: ${listed}`;
}

/**
 * The trim the worker will derive from the parts, for anything on this screen
 * that reasons about the window — the music outlook, chiefly. Never shrinks it:
 * parts inside the clip are cut from the rendered file as it is.
 */
export function splitWindow(
  parts: readonly SplitRange[],
  durationSec: number,
): { startDeltaSec: number; endDeltaSec: number } {
  if (!parts.length) return { startDeltaSec: 0, endDeltaSec: 0 };
  const edges = parts.map((part) => snapped(part, durationSec));
  return {
    startDeltaSec: Math.min(0, ...edges.map((part) => part.startSec)),
    endDeltaSec: Math.max(0, Math.max(...edges.map((part) => part.endSec)) - durationSec),
  };
}
