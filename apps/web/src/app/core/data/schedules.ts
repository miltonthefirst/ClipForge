import { Injectable, inject } from '@angular/core';
import type { ResearchSchedule } from '@clipforge/contracts';

import { FirestoreGateway, type Live } from '../firestore/gateway';
import type { QuerySpec } from '../firestore/spec';
import { browserTimezone, firstDue, type ScheduleDraft } from '../schedule-plan';

/**
 * Standing research requests: the part of Trends that runs without a press.
 *
 * A schedule is a document the client owns and the worker fires. What the
 * client writes is the request — name, cadence, what to look for, whether it
 * is on — and the worker writes back what it did with it. The rules pin the
 * client to its half, so nothing here can forge a run.
 */

const SCHEDULES = 'schedules';

/** More schedules than anyone should have. Bounded, like every listener. */
export const SCHEDULE_PAGE = 20;

/** Every schedule, unordered: a handful of rows, sorted in memory by {@link byName}. */
export function schedulesSpec(): QuerySpec {
  return { collection: SCHEDULES, limit: SCHEDULE_PAGE };
}

export function byName(schedules: readonly ResearchSchedule[]): ResearchSchedule[] {
  return [...schedules].sort((a, b) => a.name.localeCompare(b.name));
}

/**
 * The document a client is allowed to create.
 *
 * The worker's fields are written null on purpose: the rules refuse a schedule
 * that arrives claiming to have already run, and a blank `lastOutcome` is what
 * the page reads as "not yet".
 */
export function newSchedule(id: string, uid: string, draft: ScheduleDraft, now: string) {
  const daily = draft.cadence === 'DAILY';
  return {
    id,
    uid,
    name: draft.name.trim(),
    enabled: true,
    cadence: draft.cadence,
    everyHours: daily ? null : draft.everyHours,
    at: daily ? draft.at : null,
    timezone: daily ? browserTimezone() : null,
    options: draft.options,
    nextDueAt: firstDue(draft.cadence, draft.at, new Date(now)),
    lastRunAt: null,
    lastJobId: null,
    lastOutcome: null,
    createdAt: now,
    updatedAt: now,
  } satisfies ResearchSchedule;
}

@Injectable({ providedIn: 'root' })
export class SchedulesRepository {
  private readonly db = inject(FirestoreGateway);

  /** Live, because the worker writes back to these rows after every firing. */
  watchSchedules(): Live<ResearchSchedule[]> {
    return this.db.live<ResearchSchedule>(schedulesSpec());
  }

  async create(uid: string, draft: ScheduleDraft): Promise<string> {
    const id = this.db.newId(SCHEDULES);
    await this.db.create(
      SCHEDULES,
      { ...newSchedule(id, uid, draft, new Date().toISOString()) },
      id,
    );
    return id;
  }

  /**
   * Switch a schedule on or off.
   *
   * Switching on also resets when it is next due, so a schedule that was off
   * for a month fires on the worker's next look rather than at a time that
   * has long passed — which is the same instant, but the page can say why.
   */
  async setEnabled(schedule: ResearchSchedule, enabled: boolean): Promise<void> {
    await this.db.update(SCHEDULES, schedule.id, {
      enabled,
      nextDueAt: enabled ? firstDue(schedule.cadence, schedule.at ?? '00:00') : null,
      updatedAt: new Date().toISOString(),
    });
  }

  async remove(scheduleId: string): Promise<void> {
    await this.db.remove(SCHEDULES, scheduleId);
  }
}
