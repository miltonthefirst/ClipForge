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
import { RouterLink } from '@angular/router';
import type { Clip, Job, Trend, TrendVideo } from '@clipforge/contracts';

import { CompileBasketService } from '../../core/compile-basket';
import { newestFirst } from '../../core/data/jobs';
import { IN_LIMIT, ResearchRepository } from '../../core/data/research';
import type { Live } from '../../core/firestore/gateway';
import { SessionService } from '../../core/session';
import { activityFor, describeActivity, type TrendActivity } from '../../core/trend-activity';
import {
  describeSignal,
  readableAge,
  readableDuration,
  readableViews,
} from '../../core/trend-list';
import { TrendActions } from './trend-actions';

/** One video, with everything the template needs worked out. */
interface VideoRow {
  readonly video: TrendVideo;
  readonly views: string | null;
  readonly age: string | null;
  readonly length: string | null;
  readonly inBasket: boolean;
  readonly queued: boolean;
}

/**
 * One trend, and what became of it.
 *
 * The card on the list page says what a trend is; this page says that and
 * then what happened next: every job made from it, live, the clips those
 * jobs produced with a way into Review, and — the case this page exists
 * for — a job that ran to the end and made nothing, said in words beside
 * the trend it was made from. See docs/adr/0026-what-became-of-a-trend.md.
 */
@Component({
  selector: 'app-trend-page',
  imports: [RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './trend-page.html',
})
export class TrendPage implements OnDestroy {
  private readonly research = inject(ResearchRepository);
  private readonly session = inject(SessionService);
  private readonly actions = inject(TrendActions);
  protected readonly basket = inject(CompileBasketService);

  /** From the route. */
  readonly id = input.required<string>();

  /** Undefined until the first snapshot; null for a trend that is not there. */
  protected readonly trend = signal<Trend | null | undefined>(undefined);
  protected readonly jobs = signal<Job[] | null>(null);
  protected readonly clips = signal<Clip[] | null>(null);
  protected readonly loadFailure = signal<string | null>(null);
  protected readonly error = signal<string | null>(null);
  protected readonly busy = signal<string | null>(null);

  private readonly heldTrend = signal<Live<Trend> | null>(null);
  private readonly heldJobs = signal<Live<Job[]> | null>(null);
  private readonly heldClips = signal<Live<Clip[]> | null>(null);

  protected readonly signals = computed(() => (this.trend()?.signals ?? []).map(describeSignal));

  protected readonly videos = computed<VideoRow[]>(() => {
    const trend = this.trend();
    if (!trend) return [];
    const queued = this.actions.queued();
    this.basket.items();
    const now = Date.now();
    return trend.videos.map((video) => ({
      video,
      views: readableViews(video.viewCount),
      age: readableAge(video.uploadedAt, now),
      length: readableDuration(video.durationSec),
      inBasket: this.basket.has(video.url),
      queued: queued.has(video.url),
    }));
  });

  /** Null until both the jobs and their clips have arrived, so a job is never shown clipless by accident. */
  protected readonly activity = computed<TrendActivity | null>(() => {
    const jobs = this.jobs();
    const clips = this.clips();
    if (jobs === null || clips === null) return null;
    return activityFor(jobs, clips);
  });

  protected readonly summary = computed(() => {
    const activity = this.activity();
    return activity ? describeActivity(activity) : null;
  });

  constructor() {
    effect(() => {
      const uid = this.session.uid;
      const id = this.id();
      untracked(() => this.heldTrend())?.release();
      this.heldTrend.set(null);
      this.trend.set(undefined);
      this.loadFailure.set(null);
      if (!uid || !id) return;
      this.heldTrend.set(this.research.watchTrend(id));
    });

    effect(() => {
      const held = this.heldTrend();
      if (!held) return;
      const failure = held.error();
      if (failure) {
        this.loadFailure.set(failure.message);
        return;
      }
      if (held.loading()) return;
      this.trend.set(held.data());
    });

    effect(() => {
      const uid = this.session.uid;
      const id = this.id();
      untracked(() => this.heldJobs())?.release();
      this.heldJobs.set(null);
      this.jobs.set(null);
      if (!uid || !id) return;
      this.heldJobs.set(this.research.watchTrendJobs(id));
    });

    effect(() => {
      const held = this.heldJobs();
      if (!held) return;
      // The activity list is a courtesy; the trend is the page. A failure
      // here leaves the list empty rather than taking the page down.
      if (held.error()) {
        this.jobs.set([]);
        return;
      }
      const jobs = held.data();
      if (jobs) this.jobs.set(newestFirst(jobs));
    });

    // Keyed on the job ids, not the jobs: a progress write on a job must not
    // tear down and re-open the clips listener.
    effect(() => {
      const ids = (this.jobs() ?? []).map((job) => job.id).slice(0, IN_LIMIT);
      untracked(() => this.heldClips())?.release();
      this.heldClips.set(null);
      if (this.jobs() === null) {
        this.clips.set(null);
        return;
      }
      if (!ids.length) {
        this.clips.set([]);
        return;
      }
      this.heldClips.set(this.research.watchClipsForJobs(ids));
    });

    effect(() => {
      const held = this.heldClips();
      if (!held) return;
      if (held.error()) {
        this.clips.set([]);
        return;
      }
      const clips = held.data();
      if (clips) this.clips.set(clips);
    });
  }

  ngOnDestroy(): void {
    this.heldTrend()?.release();
    this.heldJobs()?.release();
    this.heldClips()?.release();
  }

  // ── Actions ────────────────────────────────────────────────────────────────

  protected async clipIt(video: TrendVideo): Promise<void> {
    const trend = this.trend();
    if (!trend) return;
    this.busy.set(video.url);
    this.error.set(null);
    try {
      await this.actions.clipIt(trend, video);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.busy.set(null);
    }
  }

  protected toggleBasket(video: TrendVideo): void {
    const trend = this.trend();
    if (!trend) return;
    this.error.set(null);
    if (!this.actions.toggleBasket(trend, video)) {
      this.error.set(this.actions.basketFullMessage());
    }
  }

  protected async compileTrend(): Promise<void> {
    const trend = this.trend();
    if (!trend) return;
    this.error.set(null);
    try {
      await this.actions.compileTrend(trend);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    }
  }

  protected async decide(status: 'NEW' | 'DISMISSED'): Promise<void> {
    const trend = this.trend();
    if (!trend) return;
    this.busy.set(trend.id);
    this.error.set(null);
    try {
      await this.actions.decide(trend, status);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.busy.set(null);
    }
  }

  // ── Words ──────────────────────────────────────────────────────────────────

  protected statusTone(job: Job): string {
    switch (job.status) {
      case 'COMPLETED':
        return 'bg-ok-bg text-ok-ink';
      case 'FAILED':
        return 'bg-danger-bg text-danger-ink';
      case 'RUNNING':
        return 'bg-forge-600/10 text-ink';
      case 'CANCELLED':
        return 'bg-line text-ink-subtle';
      default:
        return 'bg-line text-ink-muted';
    }
  }

  protected reviewLabel(clip: Clip): string {
    switch (clip.review) {
      case 'APPROVED':
        return 'approved';
      case 'REJECTED':
        return 'rejected';
      default:
        return 'to review';
    }
  }

  protected reviewTone(clip: Clip): string {
    switch (clip.review) {
      case 'APPROVED':
        return 'bg-ok-bg text-ok-ink';
      case 'REJECTED':
        return 'bg-line text-ink-subtle';
      default:
        return 'bg-accent-soft text-accent-ink';
    }
  }
}
