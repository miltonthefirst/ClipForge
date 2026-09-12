import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  computed,
  effect,
  inject,
  signal,
} from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import type { Candidate, Clip, ClipPreview } from '@clipforge/contracts';

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

@Component({
  selector: 'app-review-page',
  imports: [DecimalPipe, RouterLink],
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

  /**
   * Whether any clip in the queue can only be played on the worker.
   *
   * Gates the "open this on that machine" notice. Once clips are uploaded most
   * of the queue plays anywhere, and a banner describing a constraint that no
   * longer applies is worse than no banner — it teaches people to ignore it.
   */
  protected readonly anyLocalOnly = computed(() =>
    (this.cards() ?? []).some((card) => card.source.kind === 'poster'),
  );

  /**
   * Why the queue cannot fetch clips that are demonstrably uploaded.
   *
   * Surfaced on the queue and not only on the clip page because this is a
   * project-wide condition — if one clip is refused, every clip is — and the
   * queue is where someone notices that nothing plays. Reported once for the
   * whole list rather than per card, since it is one fact about access rather
   * than a property of any clip.
   */
  protected readonly blockedReason = computed(() => {
    for (const card of this.cards() ?? []) {
      if (card.source.kind === 'blocked') return card.source.reason;
    }
    return null;
  });

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
          source: await this.playback.resolve(clip, local),
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

  protected async decide(clip: Clip, review: 'APPROVED' | 'REJECTED'): Promise<void> {
    this.busy.set(clip.id);
    try {
      await this.store.review(clip.id, review, clip.storagePath);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.busy.set(null);
    }
  }
}
