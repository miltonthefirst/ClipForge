import { Injectable, computed, inject } from '@angular/core';
import type { CalibrationReport, MetricSnapshot } from '@clipforge/contracts';

import { FirestoreGateway, type Live } from '../firestore/gateway';
import type { QuerySpec } from '../firestore/spec';

/**
 * Did any of it work.
 *
 * Two questions, and both are about outcomes rather than work in progress: what
 * the clips that went out actually did, day by day, and what the last
 * calibration made of that. Everything else in the app reports on the pipeline;
 * this reports on whether running the pipeline was worth it.
 *
 * **Nothing here is scoped by uid**, which is a decision rather than an
 * omission. The first two clips this project published went out under two
 * different accounts, and a uid-filtered metrics query would have drawn half
 * the channel while looking complete — the same failure the review queue
 * already fixed. `uid` on a calibration report records who generated it, not
 * whose data it covers, so it is not filtered on either.
 */

/**
 * Every metric snapshot in the workspace, newest day first.
 *
 * **Descending, and reversed on the way out** — see {@link oldestFirst}. Every
 * query here is bounded, and a bound only makes sense with the newest end kept:
 * ordered ascending, the 2001st snapshot would push the dashboard into showing
 * the oldest 2000 for ever — freezing on the first clips ever published while
 * new ones silently never appeared, with nothing on screen to say so.
 *
 * `date` is the metrics day in the channel's reporting timezone, stored as a
 * `YYYY-MM-DD` string rather than a timestamp, because a reporting day has no
 * midnight and no timezone that would survive the conversion. Ordering on it is
 * a string comparison, which for that form is also chronological order.
 */
export const METRICS_SPEC: QuerySpec = {
  collection: 'metrics',
  orderBy: [['date', 'desc']],
  limit: 2000,
};

/**
 * The most recent calibration report, whoever ran it.
 *
 * One, not all of them. The history matters — a conclusion that changed is the
 * interesting case — but it belongs on a screen somebody asks for, not on the
 * one that answers "how are we doing".
 */
export const LATEST_CALIBRATION_SPEC: QuerySpec = {
  collection: 'calibrations',
  orderBy: [['generatedAt', 'desc']],
  limit: 1,
};

/**
 * The delivered page of snapshots, turned round so the oldest day is first.
 *
 * Reversing rather than sorting, because {@link METRICS_SPEC} has already
 * ordered by `date`; this is the inverse of that order and nothing more.
 *
 * A copy, though, and not `reverse()` in place. The listener behind that spec
 * is shared, so every reader of the metrics query holds the same array
 * instance — reversing it would flip the order under whoever else is reading,
 * with no error and nothing to notice until a chart ran backwards.
 */
export function oldestFirst(snapshots: readonly MetricSnapshot[]): MetricSnapshot[] {
  return [...snapshots].reverse();
}

@Injectable({ providedIn: 'root' })
export class AnalyticsRepository {
  private readonly db = inject(FirestoreGateway);

  /**
   * Every metric snapshot in the workspace, oldest day first.
   *
   * The reversal is a `computed` over the shared listener's own signal, so the
   * cost is paid once per snapshot delivered rather than once per read, however
   * many places on the page read it.
   */
  watchMetrics(): Live<MetricSnapshot[]> {
    const live = this.db.live<MetricSnapshot>(METRICS_SPEC);
    return {
      ...live,
      data: computed(() => {
        const rows = live.data();
        return rows === null ? null : oldestFirst(rows);
      }),
    };
  }

  /**
   * The newest calibration report, or nothing.
   *
   * `data` is null in two different situations here and `loading` is what tells
   * them apart: null while loading is "not back yet", null after it is "nobody
   * has run a calibration". Collapsing a one-row result to its first row is the
   * conflation the gate's three states exist to prevent, and it is only honest
   * because the other two states survive the collapse — a page that renders
   * this must read `loading` before it says there is no report.
   */
  watchLatestCalibration(): Live<CalibrationReport> {
    const live = this.db.live<CalibrationReport>(LATEST_CALIBRATION_SPEC);
    return { ...live, data: computed(() => live.data()?.[0] ?? null) };
  }
}
