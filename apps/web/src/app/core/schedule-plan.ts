import type { ResearchOptions, ResearchSchedule, ScheduleCadence } from '@clipforge/contracts';

/**
 * What a schedule means in words, and when its first run should be.
 *
 * Pure, like `trend-list.ts`, because each of these is quiet when wrong: a
 * "next run" computed in the wrong zone is a morning list that arrives at
 * midnight, and a cadence sentence that says "every 12 hours" about a daily
 * schedule is a setting somebody will not correct because it reads as right.
 */

/** What the Trends page collects before a schedule exists. */
export interface ScheduleDraft {
  readonly name: string;
  readonly cadence: ScheduleCadence;
  readonly everyHours: number;
  readonly at: string;
  readonly options: ResearchOptions;
}

/** The IANA zone this browser is in, which is the zone a daily time is meant in. */
export function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch {
    return 'UTC';
  }
}

/**
 * When a schedule created or edited now should first fire, as an ISO instant.
 *
 * INTERVAL fires on the worker's next tick — a person who just set up "every
 * 12 hours" wants a list now and another one at lunchtime, not a first list at
 * lunchtime. DAILY is the next occurrence of `at` in *this browser's* zone,
 * which is also the zone the schedule records; the worker recomputes every
 * later occurrence in that same zone.
 */
export function firstDue(cadence: ScheduleCadence, at: string, now: Date = new Date()): string {
  if (cadence !== 'DAILY') return now.toISOString();
  const [hours, minutes] = at.split(':').map(Number);
  const candidate = new Date(now);
  candidate.setHours(hours ?? 0, minutes ?? 0, 0, 0);
  if (candidate.getTime() <= now.getTime()) candidate.setDate(candidate.getDate() + 1);
  return candidate.toISOString();
}

/**
 * When an *edited* schedule should next fire.
 *
 * Not the same question as {@link firstDue}. A person who renames a schedule
 * has not asked for a run; one who changes the interval has asked for the
 * new interval to start now, not for a run now. So a daily time is always
 * recomputed (it is what changed, or it costs nothing), an unchanged interval
 * keeps its due time, and a changed one counts from now. A schedule that is
 * off stays off until it is switched on, which resets the clock anyway.
 */
export function nextDueAfterEdit(
  schedule: Pick<ResearchSchedule, 'enabled' | 'cadence' | 'everyHours' | 'nextDueAt'>,
  draft: Pick<ScheduleDraft, 'cadence' | 'everyHours' | 'at'>,
  now: Date = new Date(),
): string | null {
  if (!schedule.enabled) return schedule.nextDueAt ?? null;
  if (draft.cadence === 'DAILY') return firstDue('DAILY', draft.at, now);
  const unchanged = schedule.cadence === 'INTERVAL' && schedule.everyHours === draft.everyHours;
  if (unchanged) return schedule.nextDueAt ?? null;
  return new Date(now.getTime() + draft.everyHours * 3_600_000).toISOString();
}

/** "Every 12 hours" · "Daily at 07:30 (Europe/London)". */
export function describeCadence(
  schedule: Pick<ResearchSchedule, 'cadence' | 'everyHours' | 'at' | 'timezone'>,
): string {
  if (schedule.cadence === 'DAILY') {
    const zone = schedule.timezone ? ` (${schedule.timezone})` : '';
    return `Daily at ${schedule.at ?? '??:??'}${zone}`;
  }
  const hours = schedule.everyHours ?? 24;
  return hours === 24 ? 'Every day' : `Every ${hours} hours`;
}

/**
 * When the next run is, relative to now — or why there is not one.
 *
 * A schedule with no `nextDueAt` fires on the worker's next look, and says so
 * rather than showing a blank; one that is switched off says that instead.
 */
export function describeNext(
  schedule: Pick<ResearchSchedule, 'enabled' | 'nextDueAt'>,
  now: number = Date.now(),
): string {
  if (!schedule.enabled) return 'off';
  if (!schedule.nextDueAt) return 'on the worker’s next look';
  const due = Date.parse(schedule.nextDueAt);
  if (Number.isNaN(due)) return 'unknown';
  const minutes = Math.round((due - now) / 60_000);
  if (minutes <= 0) return 'due now, when a worker looks';
  if (minutes < 60) return `in ${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `in ${hours}h`;
  return `in ${Math.round(hours / 24)} days`;
}

/** What is wrong with a draft, in the order a person would fix it. Empty means nothing. */
export function scheduleProblems(draft: ScheduleDraft): string[] {
  const problems: string[] = [];
  if (!draft.name.trim()) problems.push('Give it a name.');
  if (draft.name.length > 80) problems.push('The name is longer than 80 characters.');
  if (draft.cadence === 'INTERVAL' && (draft.everyHours < 6 || draft.everyHours > 168)) {
    problems.push('The interval has to be between 6 hours and a week.');
  }
  if (draft.cadence === 'DAILY' && !/^([01]\d|2[0-3]):[0-5]\d$/.test(draft.at)) {
    problems.push('Give a time of day as HH:MM.');
  }
  return problems;
}

/** The hours a person is likely to mean. Six is the floor the contract sets. */
export const INTERVALS: readonly { hours: number; label: string }[] = [
  { hours: 6, label: 'Every 6 hours' },
  { hours: 12, label: 'Every 12 hours' },
  { hours: 24, label: 'Every day' },
  { hours: 48, label: 'Every two days' },
  { hours: 168, label: 'Every week' },
];
