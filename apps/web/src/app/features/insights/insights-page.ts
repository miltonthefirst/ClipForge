import { DecimalPipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  computed,
  effect,
  inject,
  signal,
  untracked,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import type { CalibrationReport, MetricSnapshot } from '@clipforge/contracts';

import { SessionService } from '../../core/session';
import { AnalyticsRepository } from '../../core/data/analytics';
import type { Live } from '../../core/firestore/gateway';
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
 *
 * Both queries arrive from {@link AnalyticsRepository} as live signals rather
 * than callbacks, and each has three answers rather than two: not here yet,
 * here, and never coming. This page renders all three, because on a dashboard
 * the difference between "no calibration has been run" and "the calibration
 * query was refused" is the difference between a fact and a lie.
 */
@Component({
  selector: 'app-insights-page',
  imports: [DecimalPipe, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './insights-page.html',
})
export class InsightsPage implements OnDestroy {
  private readonly data = inject(AnalyticsRepository);
  private readonly session = inject(SessionService);

  protected readonly chartWidth = CHART_WIDTH;
  protected readonly chartHeight = CHART_HEIGHT;

  /**
   * The daily snapshots, oldest day first, or null while the query is out.
   *
   * Null means not loaded and nothing else. A workspace with nothing published
   * delivers an empty array, and only that earns "Nothing has been measured
   * yet" — which is a statement about the channel, not about the network.
   */
  protected readonly snapshots = signal<MetricSnapshot[] | null>(null);

  /** The newest calibration report, or null for "there is not one". */
  protected readonly report = signal<CalibrationReport | null>(null);

  /**
   * Whether the calibration query has actually answered.
   *
   * Needed because the repository collapses a one-row query down to its row, so
   * `report` is null both before the answer arrives and when nobody has ever
   * run a calibration. Without this flag the page announced "No calibration has
   * been run yet" during the second or two before the first one loaded — and
   * would have gone on announcing it for ever behind a query that had failed.
   */
  protected readonly reportLoaded = signal(false);

  /**
   * Why each half of the page is missing, kept apart so that one recovering
   * does not clear the other's banner.
   *
   * Both queries run unfiltered over collections the rules gate on approval, so
   * in practice they fail together and say the same thing; the banner shows the
   * first. They are separate signals because the sections they explain are
   * separate, and because a snapshot that clears one is not evidence about the
   * other.
   */
  protected readonly metricsFailure = signal<string | null>(null);
  protected readonly reportFailure = signal<string | null>(null);

  protected readonly error = computed(() => this.metricsFailure() ?? this.reportFailure());

  /** Measurements in hand. False while loading, and false after a failure. */
  protected readonly loaded = computed(() => this.snapshots() !== null);

  /**
   * The listeners currently held, released on the way out and when the account
   * changes.
   *
   * Signals rather than plain fields because the effects below *read* them: a
   * plain field is not tracked, so a delivering effect would run once against
   * no listener and never again, and this page would sit blank for ever with
   * nothing on screen or in the console to say why.
   */
  private readonly metrics = signal<Live<MetricSnapshot[]> | null>(null);
  private readonly calibration = signal<Live<CalibrationReport> | null>(null);

  constructor() {
    effect(() => {
      const uid = this.session.uid;

      // Cleared first, and unconditionally: a failure belongs to the listeners
      // that produced it, and this effect is about to replace them — including
      // with no listeners at all, on the way out of the app.
      this.metricsFailure.set(null);
      this.reportFailure.set(null);

      // Let go before taking the next pair. The gate keeps a listener warm for
      // fifteen minutes past its last reader, so leaving this page and coming
      // back re-attaches to the same two and bills nothing — which is the whole
      // reason releasing is not the same as unsubscribing.
      // `untracked`, or this effect depends on the signals it is about to write
      // and re-runs itself for ever, releasing and re-opening both listeners on
      // every pass. That is a hang rather than a leak.
      untracked(() => {
        this.metrics()?.release();
        this.calibration()?.release();
      });
      this.metrics.set(null);
      this.calibration.set(null);
      this.snapshots.set(null);
      this.report.set(null);
      this.reportLoaded.set(false);

      // No uid in either query, but still gated on being signed in: the rules
      // refuse an unauthenticated read, and firing one would surface a
      // permission error on a page that is simply not ready yet.
      if (!uid) return;

      this.metrics.set(this.data.watchMetrics());
      this.calibration.set(this.data.watchLatestCalibration());
    });

    // Separate from the subscription above so that a delivery does not re-open
    // a listener: this one reads the held signal and nothing else, and reading
    // `metrics` inside the effect that assigns it would make every snapshot a
    // reason to re-subscribe.
    effect(() => {
      const held = this.metrics();
      if (!held) return;
      const failure = held.error();
      if (failure) {
        this.metricsFailure.set(failure.message);
        return;
      }
      const rows = held.data();
      // Still out is not the same as delivered-and-empty, and only the second
      // is news. Acting on the first is what leaves a loading line on screen
      // for ever behind a query that has already failed.
      if (rows === null) return;
      // A snapshot after a failure means the failure is over.
      this.metricsFailure.set(null);
      this.snapshots.set(rows);
    });

    effect(() => {
      const held = this.calibration();
      if (!held) return;
      const failure = held.error();
      if (failure) {
        this.reportFailure.set(failure.message);
        return;
      }
      // `loading` rather than a null check, and this is the one place on the
      // page where the difference matters: the repository turns a one-row query
      // into its single row, so null data here means "nobody has run a
      // calibration" as often as it means "not back yet". `loading` is the only
      // thing that tells those two apart.
      if (held.loading()) return;
      this.reportFailure.set(null);
      this.report.set(held.data());
      this.reportLoaded.set(true);
    });
  }

  ngOnDestroy(): void {
    this.metrics()?.release();
    this.calibration()?.release();
  }

  protected readonly rollups = computed(() => rollUp(this.snapshots() ?? []));

  protected readonly curves = computed(() => toCurves(this.rollups(), CHART_WIDTH, CHART_HEIGHT));

  protected readonly totalViews = computed(() =>
    this.rollups().reduce((total, row) => total + row.views, 0),
  );

  protected readonly publishedCount = computed(() => this.rollups().length);

  /** Days of data held, which is a bigger number than the clips it covers. */
  protected readonly snapshotCount = computed(() => this.snapshots()?.length ?? 0);

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
