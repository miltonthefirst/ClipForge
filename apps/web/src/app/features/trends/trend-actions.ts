import { Injectable, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import type { Trend, TrendStatus, TrendVideo } from '@clipforge/contracts';

import { briefFromTrend } from '../../core/clip-brief';
import { CompileBasketService } from '../../core/compile-basket';
import { JobsRepository } from '../../core/data/jobs';
import { ResearchRepository } from '../../core/data/research';
import { SessionService } from '../../core/session';

/**
 * What can be done with a trend, from wherever it is shown.
 *
 * The list page and the trend's own page offer the same four actions, and a
 * person moving between them expects a video sent from one to read as Queued
 * on the other. So the actions live here, once, and so does the memory of
 * what this visit has sent. The pages keep their own busy and error state:
 * where a failure is shown is the page's business, what failed is not.
 */
@Injectable({ providedIn: 'root' })
export class TrendActions {
  private readonly jobs = inject(JobsRepository);
  private readonly research = inject(ResearchRepository);
  private readonly basket = inject(CompileBasketService);
  private readonly session = inject(SessionService);
  private readonly router = inject(Router);

  /** Video URLs this visit has already sent to the pipeline, so a button can say so. */
  readonly queued = signal<ReadonlySet<string>>(new Set());

  /**
   * One video, straight to the pipeline, as if its URL had been pasted.
   *
   * The model's angle on the trend becomes the brief when it thought there was
   * a clip in it. The job remembers the trend, so the trend's page can say what
   * became of it — including the one outcome that otherwise vanishes, a job
   * that finished with nothing to cut.
   */
  async clipIt(trend: Trend, video: TrendVideo): Promise<string> {
    const uid = this.session.uid;
    if (!uid) throw new Error('Not signed in.');
    const id = await this.jobs.submit(uid, video.url, briefFromTrend(trend), trend.id);
    this.queued.update((urls) => new Set([...urls, video.url]));
    if (trend.status !== 'PROMOTED') {
      await this.research.decide(trend.id, 'PROMOTED', uid);
    }
    return id;
  }

  /**
   * In or out of the basket. The first video in sets the theme from its trend.
   *
   * Returns false when the basket is full and the video could not go in.
   */
  toggleBasket(trend: Trend, video: TrendVideo): boolean {
    if (this.basket.count() === 0 && !this.basket.has(video.url)) {
      this.basket.setTheme(trend.compilationTitle ?? trend.topic);
      this.basket.trendId.set(trend.id);
    }
    const added = this.basket.toggle({
      url: video.url,
      title: video.title,
      channel: video.channel ?? null,
    });
    return added || this.basket.has(video.url) || !this.basket.full();
  }

  /** The whole trend, as a compilation: its best videos, its title, over to the Compile page. */
  async compileTrend(trend: Trend): Promise<void> {
    const best = [...trend.videos].sort((a, b) => b.score - a.score).slice(0, 6);
    if (best.length < 2) {
      throw new Error('A compilation needs at least two videos, and this trend has fewer.');
    }
    this.basket.startFrom(
      trend.id,
      trend.topic,
      trend.compilationTitle ?? null,
      best.map((video) => ({ url: video.url, title: video.title, channel: video.channel ?? null })),
    );
    await this.router.navigate(['/compile']);
  }

  async decide(trend: Trend, status: TrendStatus): Promise<void> {
    const uid = this.session.uid;
    if (!uid) throw new Error('Not signed in.');
    await this.research.decide(trend.id, status, uid);
  }

  /** The message for a basket that would not take one more. */
  basketFullMessage(): string {
    return `The compilation already has ${this.basket.count()} videos, which is the most it can take.`;
  }
}
