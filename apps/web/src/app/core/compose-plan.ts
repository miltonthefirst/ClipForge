import type { ComposeOptions, Trend } from '@clipforge/contracts';

import { describeSignal } from './trend-list';

/**
 * Turning a trend into the request for a drawn video, and checking it.
 *
 * Pure, like `clip-brief.ts`: what the trend page writes is what the worker
 * writes a script from, and a wrong line here — the curator's doubt passed
 * off as the angle, a video title dropped, a script over the limit sent
 * anyway — is quiet.
 */

/** What the panel holds before a job exists. */
export interface ComposeDraft {
  readonly angle: string;
  readonly script: string;
  readonly lengthSec: number;
  readonly voice: string | null;
  readonly captions: boolean;
  readonly titleCard: boolean;
}

export const DEFAULT_COMPOSE: ComposeDraft = {
  angle: '',
  script: '',
  lengthSec: 45,
  voice: null,
  captions: true,
  titleCard: true,
};

/** The lengths a person is likely to mean. The contract allows 15 to 90. */
export const COMPOSE_LENGTHS: readonly { seconds: number; label: string }[] = [
  { seconds: 30, label: 'Half a minute' },
  { seconds: 45, label: '45 seconds' },
  { seconds: 60, label: 'One minute' },
  { seconds: 90, label: 'A minute and a half' },
];

/**
 * The voices offered. Kokoro ships fifty-four; these six are the ones that
 * read news plainly in English. Null is the worker's own default.
 */
export const COMPOSE_VOICES: readonly { id: string | null; label: string }[] = [
  { id: null, label: 'The worker’s default' },
  { id: 'af_heart', label: 'Heart · US, female' },
  { id: 'af_bella', label: 'Bella · US, female' },
  { id: 'am_michael', label: 'Michael · US, male' },
  { id: 'am_adam', label: 'Adam · US, male' },
  { id: 'bf_emma', label: 'Emma · UK, female' },
  { id: 'bm_george', label: 'George · UK, male' },
];

const CONTEXT_LIMIT = 2000;

/**
 * What the trend knows, as lines the script may use.
 *
 * The signals, the videos that exist, and the curator's note. The note goes
 * here rather than into the angle on purpose: for the trends this is meant
 * for — the ones with nothing to clip — the curator's angle usually says the
 * evidence is thin, which is a fact the script should respect, not a steer it
 * should follow.
 */
export function contextFromTrend(trend: Trend): string {
  const lines: string[] = [];
  for (const signal of trend.signals) lines.push(`- ${describeSignal(signal)}`);
  for (const video of trend.videos.slice(0, 6)) {
    lines.push(`- Video: ${video.title}` + (video.channel ? ` — ${video.channel}` : ''));
  }
  if (trend.angle) lines.push(`- The curator's note: ${trend.angle}`);
  if (trend.matchedTopics?.length) {
    lines.push(`- This channel is about: ${trend.matchedTopics.join(', ')}`);
  }
  let text = lines.join('\n');
  if (text.length > CONTEXT_LIMIT) text = text.slice(0, CONTEXT_LIMIT - 1) + '…';
  return text;
}

/** The job's options: the trend's facts, the person's steer, the settings chosen. */
export function composeFromTrend(trend: Trend, draft: ComposeDraft): ComposeOptions {
  const seed = Array.from(trend.id).reduce(
    (sum, ch) => (sum * 31 + ch.charCodeAt(0)) % 2147483647,
    7,
  );
  return {
    topic: trend.topic.slice(0, 200),
    angle: draft.angle.trim().slice(0, 500) || null,
    context: contextFromTrend(trend) || null,
    script: draft.script.trim().slice(0, 2000) || null,
    targetDurationSec: Math.min(90, Math.max(15, Math.round(draft.lengthSec))),
    voice: draft.voice,
    language: 'en-us',
    style: 'STICK',
    captions: draft.captions,
    titleCard: draft.titleCard,
    seed,
    trendId: trend.id,
  };
}

/** What is wrong with a draft, in the order a person would fix it. Empty means nothing. */
export function composeProblems(draft: ComposeDraft): string[] {
  const problems: string[] = [];
  if (draft.script.trim().length > 2000)
    problems.push('The script is longer than 2000 characters.');
  if (draft.angle.trim().length > 500) problems.push('The steer is longer than 500 characters.');
  if (draft.lengthSec < 15 || draft.lengthSec > 90) {
    problems.push('A drawn video runs between 15 and 90 seconds.');
  }
  return problems;
}

/** One line for a clip's page: what a composed clip was made from. */
export function describeCompose(
  made: { scenes: readonly unknown[]; voice: string; scriptBy: string } | null | undefined,
): string | null {
  if (!made) return null;
  const who = made.scriptBy === 'OPERATOR' ? 'your words' : 'a script the model wrote';
  return `${made.scenes.length} drawn scene${made.scenes.length === 1 ? '' : 's'} · ${who} · voice ${made.voice}`;
}
