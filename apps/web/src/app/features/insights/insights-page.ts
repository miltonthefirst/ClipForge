import { DecimalPipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import type { CalibrationReport, MetricSnapshot } from '@clipforge/contracts';

import { SessionService } from '../../core/session';
import { ClipForgeStore } from '../../core/store';
import { rollUp, toCurves } from './rollup';

/** A correlation with its optionals resolved to a single absent value. */
interface CorrelationRow {
  readonly outcome: string;
  readonly n: number;
  readonly coefficient: number;
  readonly ciLow: number | null;
  readonly ciHigh: number | null;
  readonly interpretation: string | null;
}

/** One bucket of one breakdown, likewise flattened. */
interface CohortRow {
  readonly bucket: string;
  readonly n: number;
  readonly meanViews: number | null;
  readonly meanRetentionAtHalf: number | null;
}

interface CohortGroup {
  readonly kind: string;
  readonly rows: readonly CohortRow[];
}

const CHART_WIDTH = 320;
const CHART_HEIGHT = 120;

/**
 * What actually happened to the clips that went out.
 *
 * The one screen in this app that can say the scoring was wrong, which is the
 * whole reason Phase 9 exists. Everything else here shows work in progress;
 * this shows whether the work was worth doing.
 *
 * **It is built to be readable when the answer is "we cannot tell yet".** That
 * is the honest state at the volumes this project publishes, and it will be for
 * a long time. A dashboard that only looks right once it has a finding is a
 * dashboard that gets read as broken in the months before it does — so the
 * underpowered case is the designed case, not the empty state.
 *
 * The folding lives in `./rollup`, not here: summing a field that is already an
 * average, or normalising each curve to its own maximum, produces a chart that
 * renders perfectly and means nothing, and neither mistake shows up in a
 * screenshot. Those rules are tested separately.
 *
 * Retention curves are inline SVG rather than a chart library. Three dozen
 * points on a polyline needs no dependency, and the CSP on this app would not
 * permit fetching one anyway.
 */
@Component({
  selector: 'app-insights-page',
  imports: [DecimalPipe, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './insights-page.html',
})
export class InsightsPage {
  private readonly store = inject(ClipForgeStore);
  private readonly session = inject(SessionService);

  protected readonly chartWidth = CHART_WIDTH;
  protected readonly chartHeight = CHART_HEIGHT;

  protected readonly snapshots = signal<MetricSnapshot[]>([]);
  protected readonly report = signal<CalibrationReport | null>(null);
  protected readonly error = signal<string | null>(null);
  protected readonly loaded = signal(false);

  constructor() {
    effect((onCleanup) => {
      // No uid in the query, but still gated on being signed in: the rules
      // refuse an unauthenticated read, and firing one would surface a
      // permission error on a page that is simply not ready yet.
      if (!this.session.uid) return;

      const stopMetrics = this.store.watchMetrics(
        (rows) => {
          this.snapshots.set(rows);
          this.loaded.set(true);
        },
        (err) => this.error.set(err.message),
      );
      const stopReport = this.store.watchLatestCalibration(
        (found) => this.report.set(found),
        (err) => this.error.set(err.message),
      );
      onCleanup(() => {
        stopMetrics();
        stopReport();
      });
    });
  }

  protected readonly rollups = computed(() => rollUp(this.snapshots()));

  protected readonly curves = computed(() =>
    toCurves(this.rollups(), CHART_WIDTH, CHART_HEIGHT),
  );

  protected readonly totalViews = computed(() =>
    this.rollups().reduce((total, row) => total + row.views, 0),
  );

  protected readonly publishedCount = computed(() => this.rollups().length);

  protected readonly hasCurves = computed(() => this.curves().length > 0);

  /**
   * Correlations, with every optional number resolved to `number | null`.
   *
   * The contract makes defaulted fields optional, so they arrive as
   * `number | null | undefined` — three states where the screen has two. They
   * are flattened here rather than guarded in the template, because a template
   * that has to distinguish "absent" from "null" is a template describing the
   * code generator rather than the data.
   */
  protected readonly correlationRows = computed<CorrelationRow[]>(
    () =>
      this.report()?.correlations?.map((c) => ({
        outcome: c.outcome,
        n: c.n,
        coefficient: c.coefficient,
        ciLow: c.ciLow ?? null,
        ciHigh: c.ciHigh ?? null,
        interpretation: c.interpretation ?? null,
      })) ?? [],
  );

  /** Cohort rows grouped by their dimension, flattened the same way. */
  protected readonly cohortGroups = computed<CohortGroup[]>(() => {
    const report = this.report();
    if (!report?.cohorts) return [];
    const grouped = new Map<string, CohortRow[]>();
    for (const stat of report.cohorts) {
      const row: CohortRow = {
        bucket: stat.bucket,
        n: stat.n,
        meanViews: stat.meanViews ?? null,
        meanRetentionAtHalf: stat.meanRetentionAtHalf ?? null,
      };
      const held = grouped.get(stat.kind);
      if (held) held.push(row);
      else grouped.set(stat.kind, [row]);
    }
    return [...grouped].map(([kind, rows]) => ({ kind: this.label(kind), rows }));
  });

  private label(kind: string): string {
    return kind.charAt(0) + kind.slice(1).toLowerCase().replace(/_/g, ' ');
  }
}
