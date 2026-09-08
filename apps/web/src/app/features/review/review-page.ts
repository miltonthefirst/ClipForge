import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  effect,
  inject,
  signal,
} from '@angular/core';
import { DecimalPipe } from '@angular/common';
import type { Candidate, Clip, ClipPreview, SubScores } from '@clipforge/contracts';

import { PlaybackService, type PlaybackSource } from '../../core/playback';
import { SessionService } from '../../core/session';
import { ClipForgeStore } from '../../core/store';

/** Everything one review card needs, assembled once. */
export interface ReviewCard {
  readonly clip: Clip;
  readonly source: PlaybackSource;
  readonly preview: ClipPreview | null;
  readonly candidate: Candidate | null;
}

/** The rubric's ceilings, mirrored from the contract's own bounds. */
const SUB_SCORE_MAXIMA: readonly (readonly [keyof SubScores, string, number])[] = [
  ['hook', 'Hook', 25],
  ['curiosity', 'Curiosity', 20],
  ['standalone', 'Standalone', 20],
  ['emotion', 'Emotion', 15],
  ['pacing', 'Pacing', 10],
  ['shareability', 'Shareability', 10],
];

@Component({
  selector: 'app-review-page',
  imports: [DecimalPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './review-page.html',
})
export class ReviewPage implements OnDestroy {
  private readonly store = inject(ClipForgeStore);
  private readonly session = inject(SessionService);
  private readonly playback = inject(PlaybackService);

  private stop: (() => void) | null = null;

  protected readonly cards = signal<ReviewCard[] | null>(null);
  protected readonly error = signal<string | null>(null);
  protected readonly busy = signal<string | null>(null);
  /**
   * Whether the worker's file server answered. Decides whether video plays at
   * all, and drives the "playable on the worker machine" affordance when it
   * does not.
   */
  protected readonly localAvailable = signal(false);

  constructor() {
    void this.playback.probeLocalServer().then((ok) => {
      this.localAvailable.set(ok);
    });

    effect((onCleanup) => {
      const uid = this.session.uid;
      if (!uid) {
        this.cards.set(null);
        return;
      }
      const stop = this.store.watchReviewQueue(
        uid,
        (clips) => void this.buildCards(clips),
        'PENDING',
        (err) => this.error.set(err.message),
      );
      this.stop = stop;
      onCleanup(stop);
    });
  }

  ngOnDestroy(): void {
    this.stop?.();
  }

  private async buildCards(clips: Clip[]): Promise<void> {
    const local = this.localAvailable();
    this.cards.set(
      await Promise.all(
        clips.map(async (clip) => ({
          clip,
          source: this.playback.resolve(clip, local),
          // Fetched per card rather than with the list: the poster is ~50 KB of
          // base64, and the queue listener re-delivers its whole result set on
          // every reconnect.
          preview: await this.store.loadPreview(clip.id).catch(() => null),
          candidate: await this.store.loadCandidate(clip.candidateId).catch(() => null),
        })),
      ),
    );
  }

  protected posterSrc(card: ReviewCard): string | null {
    return card.preview ? `data:image/jpeg;base64,${card.preview.posterBase64}` : null;
  }

  protected filmstripSrc(card: ReviewCard): string | null {
    return card.preview?.filmstripBase64
      ? `data:image/jpeg;base64,${card.preview.filmstripBase64}`
      : null;
  }

  protected subScores(card: ReviewCard): { label: string; value: number; pct: number }[] {
    const scores = card.candidate?.subScores;
    if (!scores) return [];
    return SUB_SCORE_MAXIMA.map(([key, label, max]) => ({
      label,
      value: scores[key],
      pct: Math.round((scores[key] / max) * 100),
    }));
  }

  protected async decide(clip: Clip, review: 'APPROVED' | 'REJECTED'): Promise<void> {
    this.busy.set(clip.id);
    try {
      await this.store.review(clip.id, review);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.busy.set(null);
    }
  }
}
