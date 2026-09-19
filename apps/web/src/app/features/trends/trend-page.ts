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
import type { Clip, Job, Trend, TrendVideo } from '@clipforge/contracts';

import { CompileBasketService } from '../../core/compile-basket';
import {
  COMPOSE_LENGTHS,
  COMPOSE_VOICES,
  DEFAULT_COMPOSE,
  composeFromTrend,
  composeProblems,
  type ComposeDraft,
} from '../../core/compose-plan';
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
 *
 * It is also where a trend with nothing to clip becomes a video anyway:
 * *Make a video* asks the worker to write, speak and draw one
 * (docs/adr/0027-drawn-cartoons-as-the-first-visual-mode.md).
 */
@Component({
  selector: 'app-trend-page',
  imports: [RouterLink, FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './trend-page.html',
})
export class TrendPage implements OnDestroy {
  private readonly research = inject(ResearchRepository);
  private readonly session = inject(SessionService);
  private readonly actions = inject(TrendActions);
  private readonly router = inject(Router);
  protected readonly basket = inject(CompileBasketService);

  /** From the route. */
  readonly id = input.required<string>();
  /** `?make=1` opens the video panel, which is how the list card's button arrives here. */
  readonly make = input<string | undefined>(undefined);

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

  // ── Making a video ─────────────────────────────────────────────────────────

  protected readonly showMake = signal(false);
  protected readonly making = signal(false);
  protected readonly makeAngle = signal(DEFAULT_COMPOSE.angle);
  protected readonly makeScript = signal(DEFAULT_COMPOSE.script);
  protected readonly makeLength = signal(DEFAULT_COMPOSE.lengthSec);
  protected readonly makeVoice = signal<string | null>(DEFAULT_COMPOSE.voice);
  protected readonly makeCaptions = signal(DEFAULT_COMPOSE.captions);
  protected readonly makeTitleCard = signal(DEFAULT_COMPOSE.titleCard);
  protected readonly lengths = COMPOSE_LENGTHS;
  protected readonly voices = COMPOSE_VOICES;

  protected readonly makeDraft = computed<ComposeDraft>(() => ({
    angle: this.makeAngle(),
    script: this.makeScript(),
    lengthSec: this.makeLength(),
    voice: this.makeVoice(),
    captions: this.makeCaptions(),
    titleCard: this.makeTitleCard(),
  }));

  protected readonly makeTrouble = computed(() => composeProblems(this.makeDraft()));

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
      if (this.make()) this.showMake.set(true);
    });

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

  /**
   * Ask the worker to write, speak and draw a video about this trend.
   *
   * The trend's evidence goes along as facts the script may use; the steer
   * and the script, if given, are the person's own words. The job remembers
   * the trend, so it turns up under "what became of it" as soon as it exists.
   */
  protected async makeVideo(): Promise<void> {
    const trend = this.trend();
    const uid = this.session.uid;
    if (!trend || !uid || this.makeTrouble().length) return;
    this.making.set(true);
    this.error.set(null);
    try {
      const id = await this.research.startCompose(uid, composeFromTrend(trend, this.makeDraft()));
      if (trend.status !== 'PROMOTED') await this.actions.decide(trend, 'PROMOTED');
      await this.router.navigate(['/jobs', id]);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.making.set(false);
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
