import type { ClipOptions, Trend } from '@clipforge/contracts';

/**
 * The brief a CLIP job carries: what to look for, how many, how long.
 *
 * Pure, like the other `core/*-list` helpers, because the decisions in here
 * are quiet when wrong — a preset that writes 45 as the minimum and 20 as the
 * maximum is a job the rules refuse with a bare permission error.
 */

/** What the Jobs page collects. Numbers are seconds and counts, not strings. */
export interface ClipBrief {
  readonly instructions: string;
  readonly maxClips: number;
  readonly minDurationSec: number;
  readonly maxDurationSec: number;
}

/** The contract's defaults, so an untouched panel writes no options at all. */
export const DEFAULT_BRIEF: ClipBrief = {
  instructions: '',
  maxClips: 5,
  minDurationSec: 15,
  maxDurationSec: 75,
};

export interface DurationPreset {
  readonly key: string;
  readonly label: string;
  readonly min: number;
  readonly max: number;
}

/** The lengths people mean. Custom is the last option and sets nothing. */
export const DURATION_PRESETS: readonly DurationPreset[] = [
  { key: 'default', label: '15–75 s, the usual', min: 15, max: 75 },
  { key: 'short', label: 'Short, 10–30 s', min: 10, max: 30 },
  { key: 'medium', label: 'Medium, 20–45 s', min: 20, max: 45 },
  { key: 'long', label: 'Long, 45–90 s', min: 45, max: 90 },
  { key: 'longer', label: 'Longer, 60–180 s', min: 60, max: 180 },
];

/** Which preset a pair of lengths is, or 'custom' when it is none of them. */
export function presetFor(min: number, max: number): string {
  return DURATION_PRESETS.find((p) => p.min === min && p.max === max)?.key ?? 'custom';
}

/**
 * The document field, or null when the panel says nothing the defaults do not.
 *
 * Null rather than an object of defaults, so a job submitted without touching
 * the panel is indistinguishable from one submitted before the panel existed —
 * which keeps the Phase 9 comparison honest and the rules path short.
 */
export function toClipOptions(brief: ClipBrief): ClipOptions | null {
  const instructions = brief.instructions.trim();
  const untouched =
    !instructions &&
    brief.maxClips === DEFAULT_BRIEF.maxClips &&
    brief.minDurationSec === DEFAULT_BRIEF.minDurationSec &&
    brief.maxDurationSec === DEFAULT_BRIEF.maxDurationSec;
  if (untouched) return null;
  return {
    instructions: instructions || null,
    maxClips: Math.round(brief.maxClips),
    minDurationSec: Math.round(brief.minDurationSec),
    maxDurationSec: Math.round(brief.maxDurationSec),
  };
}

/** What is wrong with a brief, in the order a person would fix it. */
export function briefProblems(brief: ClipBrief): string[] {
  const problems: string[] = [];
  if (brief.instructions.length > 1000) problems.push('The brief is longer than 1000 characters.');
  if (!Number.isInteger(brief.maxClips) || brief.maxClips < 1 || brief.maxClips > 20) {
    problems.push('Ask for between 1 and 20 clips.');
  }
  const lengths = [brief.minDurationSec, brief.maxDurationSec];
  if (lengths.some((n) => !Number.isInteger(n) || n < 5 || n > 180)) {
    problems.push('Clip lengths have to be between 5 and 180 seconds.');
  } else if (brief.minDurationSec > brief.maxDurationSec) {
    problems.push('The shortest length is longer than the longest.');
  }
  return problems;
}

/** "up to 3 clips · 20–45 s · “the goals”", for a job card or page. */
export function describeBrief(options: ClipOptions | null | undefined): string | null {
  if (!options) return null;
  const parts: string[] = [];
  const clips = options.maxClips ?? DEFAULT_BRIEF.maxClips;
  parts.push(`up to ${clips} clip${clips === 1 ? '' : 's'}`);
  parts.push(
    `${options.minDurationSec ?? DEFAULT_BRIEF.minDurationSec}–${options.maxDurationSec ?? DEFAULT_BRIEF.maxDurationSec} s`,
  );
  if (options.instructions?.trim()) parts.push(`“${options.instructions.trim()}”`);
  return parts.join(' · ');
}

/**
 * A brief for a video found on the Trends page.
 *
 * The model's angle — one sentence on what a clip about this would show — is
 * the best instruction anyone has written for that video, so it becomes the
 * brief. Only when the model thought there was a clip in it: an angle that
 * says what is *missing* would filter everything out.
 */
export function briefFromTrend(trend: Pick<Trend, 'angle' | 'worthClipping'>): ClipOptions | null {
  const angle = trend.angle?.trim();
  if (!angle || trend.worthClipping === false) return null;
  return {
    instructions: angle.slice(0, 1000),
    maxClips: 3,
    minDurationSec: 15,
    maxDurationSec: 75,
  };
}
