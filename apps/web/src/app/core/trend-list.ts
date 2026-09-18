import type { Job, ResearchOptions, TrendSignal, TrendSource } from '@clipforge/contracts';

/**
 * What the Trends page says about a row, worked out here so it can be tested.
 *
 * The same reasoning as `job-list.ts`: the only parts of that screen with a
 * decision in them are the ones that turn a document into a sentence, and a
 * wrong sentence there is quiet — a view count read as a velocity, a topic
 * list that lost its twelfth entry, an age that says "2 hours" about last week.
 */

/**
 * The generated contract types a bounded array as a union of tuples, which is
 * what `maxItems: 12` means to json-schema-to-typescript and not what anyone
 * writes code against. {@link parseTopics} already enforces the bound; this
 * is the one place the assertion lives.
 */
export function asTopics(topics: string[]): ResearchOptions['topics'] {
  return topics.slice(0, 12) as ResearchOptions['topics'];
}

/** What a run asks for when the form is left alone. Mirrors the contract's defaults. */
export const DEFAULT_RESEARCH: Required<
  Pick<ResearchOptions, 'region' | 'lookbackHours' | 'videosPerTopic' | 'maxTrends' | 'curate'>
> = {
  region: 'US',
  lookbackHours: 48,
  videosPerTopic: 5,
  maxTrends: 12,
  curate: true,
};

/** The regions the form offers. Trends are local; the same day differs by country. */
export const REGIONS: readonly { code: string; label: string }[] = [
  { code: 'US', label: 'United States' },
  { code: 'GB', label: 'United Kingdom' },
  { code: 'IE', label: 'Ireland' },
  { code: 'CA', label: 'Canada' },
  { code: 'AU', label: 'Australia' },
  { code: 'NZ', label: 'New Zealand' },
  { code: 'IN', label: 'India' },
  { code: 'DE', label: 'Germany' },
  { code: 'FR', label: 'France' },
  { code: 'ES', label: 'Spain' },
  { code: 'IT', label: 'Italy' },
  { code: 'NL', label: 'Netherlands' },
  { code: 'BR', label: 'Brazil' },
  { code: 'MX', label: 'Mexico' },
  { code: 'JP', label: 'Japan' },
];

/** How far back "now" reaches. The feeds only come in day, week and month. */
export const LOOKBACKS: readonly { hours: number; label: string }[] = [
  { hours: 24, label: 'Today' },
  { hours: 48, label: 'Last two days' },
  { hours: 72, label: 'Last three days' },
  { hours: 168, label: 'This week' },
];

/**
 * Topics out of a text box: one per line or comma, trimmed, deduplicated,
 * capped at the contract's twelve and eighty characters each.
 *
 * Capped here rather than refused by the rules, because a thirteenth topic is
 * not an error anyone meant to make — it is a list that grew.
 */
export function parseTopics(text: string): string[] {
  const seen = new Set<string>();
  const topics: string[] = [];
  for (const raw of text.split(/[\n,]+/)) {
    const topic = raw.trim().slice(0, 80);
    const key = topic.toLowerCase();
    if (!topic || seen.has(key)) continue;
    seen.add(key);
    topics.push(topic);
    if (topics.length === 12) break;
  }
  return topics;
}

export function sourceLabel(source: TrendSource): string {
  switch (source) {
    case 'GOOGLE_TRENDS':
      return 'Google Trends';
    case 'REDDIT':
      return 'Reddit';
    case 'YOUTUBE':
      return 'YouTube';
  }
}

/** One chip per signal: where it was seen, and the provider's own number. */
export function describeSignal(signal: TrendSignal): string {
  return signal.detail
    ? `${sourceLabel(signal.source)} · ${signal.detail}`
    : sourceLabel(signal.source);
}

/** 1234567 → "1.2M", 45000 → "45K". Null → null, so the template can leave it out. */
export function readableViews(count: number | null | undefined): string | null {
  if (count === null || count === undefined) return null;
  if (count >= 1_000_000) return `${(count / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`;
  if (count >= 1_000) return `${Math.round(count / 1_000)}K`;
  return String(count);
}

/** How long ago, in the coarsest unit that is still honest. */
export function readableAge(
  iso: string | null | undefined,
  now: number = Date.now(),
): string | null {
  if (!iso) return null;
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return null;
  const hours = Math.max(0, (now - then) / 3_600_000);
  if (hours < 1) return 'just now';
  if (hours < 24) return `${Math.round(hours)}h ago`;
  const days = hours / 24;
  if (days < 14) return `${Math.round(days)}d ago`;
  return `${Math.round(days / 7)}w ago`;
}

/** 485 → "8:05"; 3725 → "1:02:05". */
export function readableDuration(seconds: number | null | undefined): string | null {
  if (seconds === null || seconds === undefined) return null;
  const whole = Math.max(0, Math.round(seconds));
  const h = Math.floor(whole / 3600);
  const m = Math.floor((whole % 3600) / 60);
  const s = whole % 60;
  return h
    ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`;
}

/**
 * What a run was about, for the run picker and the Jobs page.
 *
 * The topics if there were any, else the plain fact that there were none —
 * "whatever is trending" is a real request and the label should say so
 * rather than showing an empty string.
 */
export function runLabel(job: Job): string {
  const topics = job.researchOptions?.topics ?? [];
  return topics.length ? topics.join(', ') : 'whatever is trending';
}

/** Whether the list is still being written, so the page can say "still looking". */
export function runInProgress(job: Job): boolean {
  return job.status === 'QUEUED' || job.status === 'RUNNING';
}

/**
 * Whether the model has had its say, or is not going to.
 *
 * CURATE is the second stage; SKIPPED means it ran and had nothing to add —
 * no model reachable, or curation turned off — which is a finished state,
 * not a pending one.
 */
export function curationState(job: Job): 'pending' | 'done' | 'skipped' {
  const curate = job.stages.find((stage) => stage.name === 'CURATE');
  if (!curate) return 'skipped';
  if (curate.status === 'DONE') return 'done';
  if (curate.status === 'SKIPPED' || job.status === 'FAILED' || job.status === 'CANCELLED') {
    return 'skipped';
  }
  return 'pending';
}
