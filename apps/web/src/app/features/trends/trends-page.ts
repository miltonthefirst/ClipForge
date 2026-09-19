import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  computed,
  effect,
  inject,
  input,
  signal,
  untracked,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import type {
  Job,
  ResearchOptions,
  ResearchSchedule,
  ScheduleCadence,
  Trend,
  TrendVideo,
} from '@clipforge/contracts';

import { briefFromTrend } from '../../core/clip-brief';
import { CompileBasketService } from '../../core/compile-basket';
import { JobsRepository, newestFirst } from '../../core/data/jobs';
import { ResearchRepository, byRank } from '../../core/data/research';
import { SchedulesRepository, byName } from '../../core/data/schedules';
import type { Live } from '../../core/firestore/gateway';
import { jobNote, queueFailure } from '../../core/job-list';
import {
  INTERVALS,
  browserTimezone,
  describeCadence,
  describeNext,
  scheduleProblems,
  type ScheduleDraft,
} from '../../core/schedule-plan';
import { SessionService } from '../../core/session';
import {
  DEFAULT_RESEARCH,
  LOOKBACKS,
  REGIONS,
  asTopics,
  curationState,
  describeSignal,
  parseTopics,
  readableAge,
  readableDuration,
  readableViews,
  runInProgress,
  runLabel,
} from '../../core/trend-list';

/** One video on a trend card, with everything the template needs worked out. */
interface VideoRow {
  readonly video: TrendVideo;
  readonly views: string | null;
  readonly age: string | null;
  readonly length: string | null;
  readonly inBasket: boolean;
  readonly queued: boolean;
}

/** One standing schedule, with its sentences worked out. */
interface ScheduleRow {
  readonly schedule: ResearchSchedule;
  readonly cadence: string;
  readonly next: string;
  readonly topics: string;
}

/** One trend card. */
interface TrendRow {
  readonly trend: Trend;
  readonly signals: string[];
  readonly videos: VideoRow[];
  readonly dismissed: boolean;
  readonly promoted: boolean;
}

/**
 * What the web is talking about, and what to do about it.
 *
 * ## What this page is not
 *
 * It is not an autopilot. docs/PLAN.md Phase 10 is explicit: the system
 * proposes, a person decides, and nothing runs without a press. So the page
 * has a button that asks, a list that answers, and two ways to act on a row —
 * send one video to the pipeline as it is, or gather several into one
 * compilation — each of which is a job somebody chose to create.
 *
 * ## Why the runs are kept
 *
 * A list from yesterday is not stale the way a queue is. The point of asking
 * "what was trending on Monday" is that Monday's answer was different, and a
 * person who ran a search for one channel and then another wants both lists,
 * not the newer one over the top of the older.
 */
@Component({
  selector: 'app-trends-page',
  imports: [FormsModule, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './trends-page.html',
})
export class TrendsPage implements OnDestroy {
  private readonly research = inject(ResearchRepository);
  private readonly schedules = inject(SchedulesRepository);
  private readonly jobs = inject(JobsRepository);
  private readonly session = inject(SessionService);
  private readonly router = inject(Router);
  protected readonly basket = inject(CompileBasketService);

  /** From `?run=`, so a list can be linked to and survives a reload. */
  readonly run = input<string | undefined>(undefined);

  // ── The question ───────────────────────────────────────────────────────────

  protected readonly topicsText = signal('');
  protected readonly region = signal<string>(DEFAULT_RESEARCH.region);
  protected readonly lookback = signal<number>(DEFAULT_RESEARCH.lookbackHours);
  protected readonly curate = signal<boolean>(DEFAULT_RESEARCH.curate);
  protected readonly starting = signal(false);
  protected readonly regions = REGIONS;
  protected readonly lookbacks = LOOKBACKS;

  protected readonly topics = computed(() => parseTopics(this.topicsText()));

  // ── The standing question ──────────────────────────────────────────────────
  //
  // The same specifics as the form above, plus a cadence. Saving turns what is
  // typed into a schedule the worker fires on its own; the form is left as it
  // is, so a manual run with the same settings is still one press away.

  protected readonly showSchedule = signal(false);
  protected readonly scheduleName = signal('');
  protected readonly scheduleCadence = signal<ScheduleCadence>('DAILY');
  protected readonly scheduleEvery = signal(24);
  protected readonly scheduleAt = signal('07:30');
  protected readonly savingSchedule = signal(false);
  protected readonly scheduleBusy = signal<string | null>(null);
  protected readonly intervals = INTERVALS;
  protected readonly zone = browserTimezone();
  protected readonly standing = signal<ResearchSchedule[] | null>(null);
  private readonly heldSchedules = signal<Live<ResearchSchedule[]> | null>(null);

  protected readonly scheduleDraft = computed<ScheduleDraft>(() => ({
    name: this.scheduleName(),
    cadence: this.scheduleCadence(),
    everyHours: this.scheduleEvery(),
    at: this.scheduleAt(),
    options: this.currentOptions(),
  }));

  protected readonly scheduleTrouble = computed(() => scheduleProblems(this.scheduleDraft()));

  // ── Editing one in place ───────────────────────────────────────────────────
  //
  // Its own set of fields rather than the form's above: editing a schedule
  // must not disturb what somebody has typed for a manual run, and the two
  // are open at the same time more often than not.

  protected readonly editing = signal<string | null>(null);
  protected readonly editName = signal('');
  protected readonly editCadence = signal<ScheduleCadence>('DAILY');
  protected readonly editEvery = signal(24);
  protected readonly editAt = signal('07:30');
  protected readonly editTopicsText = signal('');
  protected readonly editRegion = signal<string>(DEFAULT_RESEARCH.region);
  protected readonly editLookback = signal<number>(DEFAULT_RESEARCH.lookbackHours);
  protected readonly editCurate = signal<boolean>(DEFAULT_RESEARCH.curate);
  protected readonly savingEdit = signal(false);

  protected readonly editDraft = computed<ScheduleDraft>(() => ({
    name: this.editName(),
    cadence: this.editCadence(),
    everyHours: this.editEvery(),
    at: this.editAt(),
    options: {
      topics: asTopics(parseTopics(this.editTopicsText())),
      region: this.editRegion(),
      lookbackHours: this.editLookback(),
      videosPerTopic: DEFAULT_RESEARCH.videosPerTopic,
      maxTrends: DEFAULT_RESEARCH.maxTrends,
      sources: null,
      subreddits: [],
      curate: this.editCurate(),
    },
  }));

  protected readonly editTrouble = computed(() => scheduleProblems(this.editDraft()));

  protected readonly scheduleRows = computed<ScheduleRow[] | null>(() => {
    const rows = this.standing();
    if (rows === null) return null;
    const now = Date.now();
    return byName(rows).map((schedule) => ({
      schedule,
      cadence: describeCadence(schedule),
      next: describeNext(schedule, now),
      topics: schedule.options.topics?.length
        ? schedule.options.topics.join(', ')
        : 'whatever is trending',
    }));
  });

  // ── The answers ────────────────────────────────────────────────────────────

  protected readonly runs = signal<Job[] | null>(null);
  protected readonly trends = signal<Trend[] | null>(null);
  protected readonly loadFailure = signal<string | null>(null);
  protected readonly error = signal<string | null>(null);
  protected readonly busy = signal<string | null>(null);
  /** Videos this visit has already sent to the pipeline, so the button says so. */
  private readonly queued = signal<ReadonlySet<string>>(new Set());
  protected readonly showDismissed = signal(false);

  private readonly heldRuns = signal<Live<Job[]> | null>(null);
  private readonly heldTrends = signal<Live<Trend[]> | null>(null);
  /** The form is seeded from the latest run once, and never over what was typed. */
  private seeded = false;

  /** The run being shown: the one in the URL, else the newest. */
  protected readonly selectedRun = computed<Job | null>(() => {
    const runs = this.runs();
    if (!runs) return null;
    const wanted = this.run();
    return runs.find((job) => job.id === wanted) ?? runs[0] ?? null;
  });

  protected readonly selectedLabel = computed(() => {
    const run = this.selectedRun();
    return run ? runLabel(run) : '';
  });

  protected readonly inProgress = computed(() => {
    const run = this.selectedRun();
    return !!run && runInProgress(run);
  });

  /** What the worker says it is doing, while it is doing it. */
  protected readonly progressNote = computed(() => {
    const run = this.selectedRun();
    return run ? jobNote(run) : null;
  });

  protected readonly curation = computed(() => {
    const run = this.selectedRun();
    return run ? curationState(run) : 'skipped';
  });

  constructor() {
    effect(() => {
      const uid = this.session.uid;
      this.loadFailure.set(null);
      untracked(() => this.heldRuns())?.release();
      this.heldRuns.set(null);
      this.runs.set(null);
      if (!uid) return;
      this.heldRuns.set(this.research.watchRuns());
    });

    effect(() => {
      const uid = this.session.uid;
      untracked(() => this.heldSchedules())?.release();
      this.heldSchedules.set(null);
      this.standing.set(null);
      if (!uid) return;
      this.heldSchedules.set(this.schedules.watchSchedules());
    });

    effect(() => {
      const held = this.heldSchedules();
      if (!held) return;
      // A failure here is not the page's failure: the list of runs still
      // works, and the section simply does not appear.
      if (held.error()) {
        this.standing.set([]);
        return;
      }
      const rows = held.data();
      if (rows === null) return;
      this.standing.set(rows);
    });

    effect(() => {
      const held = this.heldRuns();
      if (!held) return;
      const failure = held.error();
      if (failure) {
        this.loadFailure.set(queueFailure(failure));
        return;
      }
      const jobs = held.data();
      if (jobs === null) return;
      const ordered = newestFirst(jobs);
      this.runs.set(ordered);
      untracked(() => this.seedFrom(ordered[0] ?? null));
    });

    // The list follows the selected run. Reading `selectedRun` here is what
    // re-subscribes when the URL changes or a newer run arrives.
    effect(() => {
      const id = this.selectedRun()?.id ?? null;
      untracked(() => this.heldTrends())?.release();
      this.heldTrends.set(null);
      this.trends.set(null);
      if (!id) return;
      this.heldTrends.set(this.research.watchTrends(id));
    });

    effect(() => {
      const held = this.heldTrends();
      if (!held) return;
      const failure = held.error();
      if (failure) {
        this.loadFailure.set(failure.message);
        return;
      }
      const rows = held.data();
      if (rows === null) return;
      this.trends.set(byRank(rows));
    });
  }

  ngOnDestroy(): void {
    this.heldRuns()?.release();
    this.heldTrends()?.release();
    this.heldSchedules()?.release();
  }

  /**
   * Start the form where the last run left it.
   *
   * Once. A person who has typed a new topic list and is waiting for the
   * previous run to finish must not have it replaced when that run's document
   * ticks over.
   */
  private seedFrom(run: Job | null): void {
    if (this.seeded || !run) return;
    this.seeded = true;
    const options = run.researchOptions;
    if (!options) return;
    if (options.topics?.length) this.topicsText.set(options.topics.join(', '));
    if (options.region) this.region.set(options.region);
    if (options.lookbackHours) this.lookback.set(options.lookbackHours);
    if (options.curate !== undefined && options.curate !== null) this.curate.set(options.curate);
  }

  // ── Rows ───────────────────────────────────────────────────────────────────

  protected readonly rows = computed<TrendRow[] | null>(() => {
    const trends = this.trends();
    if (trends === null) return null;
    const queued = this.queued();
    // Read so a basket change re-renders the toggles.
    this.basket.items();
    const now = Date.now();
    return trends.map((trend) => ({
      trend,
      signals: trend.signals.map(describeSignal),
      videos: trend.videos.map((video) => ({
        video,
        views: readableViews(video.viewCount),
        age: readableAge(video.uploadedAt, now),
        length: readableDuration(video.durationSec),
        inBasket: this.basket.has(video.url),
        queued: queued.has(video.url),
      })),
      dismissed: trend.status === 'DISMISSED',
      promoted: trend.status === 'PROMOTED',
    }));
  });

  protected readonly visibleRows = computed(() => {
    const rows = this.rows();
    if (!rows) return null;
    return this.showDismissed() ? rows : rows.filter((row) => !row.dismissed);
  });

  protected readonly dismissedCount = computed(
    () => this.rows()?.filter((row) => row.dismissed).length ?? 0,
  );

  protected readonly empty = computed(() => this.rows()?.length === 0);

  // ── Actions ────────────────────────────────────────────────────────────────

  /** What the form asks for, whether now or on a schedule. */
  private currentOptions(): ResearchOptions {
    return {
      topics: asTopics(this.topics()),
      region: this.region(),
      lookbackHours: this.lookback(),
      videosPerTopic: DEFAULT_RESEARCH.videosPerTopic,
      maxTrends: DEFAULT_RESEARCH.maxTrends,
      sources: null,
      subreddits: [],
      curate: this.curate(),
    };
  }

  protected async start(): Promise<void> {
    const uid = this.session.uid;
    if (!uid) return;
    const options = this.currentOptions();
    this.starting.set(true);
    this.error.set(null);
    try {
      const id = await this.research.startResearch(uid, options);
      await this.router.navigate(['/trends'], { queryParams: { run: id } });
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.starting.set(false);
    }
  }

  /** One video, straight to the pipeline, as if its URL had been pasted. */
  protected async clipIt(row: TrendRow, video: TrendVideo): Promise<void> {
    const uid = this.session.uid;
    if (!uid) return;
    this.busy.set(video.url);
    this.error.set(null);
    try {
      // The model's angle on the trend becomes the brief, when it thought
      // there was a clip in it: the best instruction anyone has written for
      // this video, and until now read once and lost.
      await this.jobs.submit(uid, video.url, briefFromTrend(row.trend));
      this.queued.update((urls) => new Set([...urls, video.url]));
      if (row.trend.status !== 'PROMOTED') {
        await this.research.decide(row.trend.id, 'PROMOTED', uid);
      }
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.busy.set(null);
    }
  }

  /** In or out of the basket. The first video in sets the theme from its trend. */
  protected toggleBasket(row: TrendRow, video: TrendVideo): void {
    this.error.set(null);
    if (this.basket.count() === 0 && !this.basket.has(video.url)) {
      this.basket.setTheme(row.trend.compilationTitle ?? row.trend.topic);
      this.basket.trendId.set(row.trend.id);
    }
    const added = this.basket.toggle({
      url: video.url,
      title: video.title,
      channel: video.channel ?? null,
    });
    if (!added && !this.basket.has(video.url)) {
      this.error.set(
        `The compilation already has ${this.basket.count()} videos, which is the most it can take.`,
      );
    }
  }

  /** The whole trend, as a compilation: its best videos, its title, over to the Compile page. */
  protected async compileTrend(row: TrendRow): Promise<void> {
    const best = [...row.trend.videos].sort((a, b) => b.score - a.score).slice(0, 6);
    if (best.length < 2) {
      this.error.set('A compilation needs at least two videos, and this trend has fewer.');
      return;
    }
    this.basket.startFrom(
      row.trend.id,
      row.trend.topic,
      row.trend.compilationTitle ?? null,
      best.map((video) => ({ url: video.url, title: video.title, channel: video.channel ?? null })),
    );
    await this.router.navigate(['/compile']);
  }

  protected async decide(row: TrendRow, status: 'NEW' | 'DISMISSED'): Promise<void> {
    const uid = this.session.uid;
    if (!uid) return;
    this.busy.set(row.trend.id);
    this.error.set(null);
    try {
      await this.research.decide(row.trend.id, status, uid);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.busy.set(null);
    }
  }

  // ── Schedules ──────────────────────────────────────────────────────────────

  protected async saveSchedule(): Promise<void> {
    const uid = this.session.uid;
    if (!uid || this.scheduleTrouble().length) return;
    this.savingSchedule.set(true);
    this.error.set(null);
    try {
      await this.schedules.create(uid, this.scheduleDraft());
      this.showSchedule.set(false);
      this.scheduleName.set('');
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.savingSchedule.set(false);
    }
  }

  protected async toggleSchedule(row: ScheduleRow): Promise<void> {
    this.scheduleBusy.set(row.schedule.id);
    this.error.set(null);
    try {
      await this.schedules.setEnabled(row.schedule, !row.schedule.enabled);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.scheduleBusy.set(null);
    }
  }

  /** The schedule's own settings, once, now: a manual run wearing its clothes. */
  protected async runScheduleNow(row: ScheduleRow): Promise<void> {
    const uid = this.session.uid;
    if (!uid) return;
    this.scheduleBusy.set(row.schedule.id);
    this.error.set(null);
    try {
      const id = await this.research.startResearch(uid, row.schedule.options);
      await this.router.navigate(['/trends'], { queryParams: { run: id } });
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.scheduleBusy.set(null);
    }
  }

  /** Open the editor on one row, seeded from what the schedule says now. */
  protected beginEdit(row: ScheduleRow): void {
    const schedule = row.schedule;
    this.editName.set(schedule.name);
    this.editCadence.set(schedule.cadence);
    this.editEvery.set(schedule.everyHours ?? 24);
    this.editAt.set(schedule.at ?? '07:30');
    this.editTopicsText.set((schedule.options.topics ?? []).join(', '));
    this.editRegion.set(schedule.options.region ?? DEFAULT_RESEARCH.region);
    this.editLookback.set(schedule.options.lookbackHours ?? DEFAULT_RESEARCH.lookbackHours);
    this.editCurate.set(schedule.options.curate ?? DEFAULT_RESEARCH.curate);
    this.error.set(null);
    this.editing.set(schedule.id);
  }

  protected cancelEdit(): void {
    this.editing.set(null);
  }

  protected async saveEdit(row: ScheduleRow): Promise<void> {
    if (this.editing() !== row.schedule.id || this.editTrouble().length) return;
    this.savingEdit.set(true);
    this.error.set(null);
    try {
      await this.schedules.update(row.schedule, this.editDraft());
      this.editing.set(null);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.savingEdit.set(false);
    }
  }

  protected async deleteSchedule(row: ScheduleRow): Promise<void> {
    if (!confirm(`Delete the schedule "${row.schedule.name}"? The lists it already made stay.`)) {
      return;
    }
    this.scheduleBusy.set(row.schedule.id);
    this.error.set(null);
    try {
      await this.schedules.remove(row.schedule.id);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.scheduleBusy.set(null);
    }
  }

  protected label(job: Job): string {
    return runLabel(job);
  }

  protected when(job: Job): string {
    const at = new Date(job.createdAt);
    return at.toLocaleString(undefined, {
      weekday: 'short',
      hour: '2-digit',
      minute: '2-digit',
      day: 'numeric',
      month: 'short',
    });
  }
}
