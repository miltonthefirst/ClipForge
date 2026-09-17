import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  computed,
  effect,
  inject,
  signal,
} from '@angular/core';
import { DecimalPipe, LowerCasePipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import type { Candidate, Clip, ClipPreview, Preference } from '@clipforge/contracts';

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
  imports: [DecimalPipe, LowerCasePipe, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './review-page.html',
})
export class ReviewPage implements OnDestroy {
  private readonly store = inject(ClipForgeStore);
  private readonly session = inject(SessionService);
  private readonly playback = inject(PlaybackService);

  private stop: (() => void) | null = null;

  protected readonly cards = signal<ReviewCard[] | null>(null);
  /**
   * What the system thinks it has learned, waiting to be kept or turned down.
   *
   * Shown here rather than on a settings page nobody opens: a proposal arrives
   * moments after the remake that taught it, while the reviewer is still
   * looking at the queue and still remembers why they asked.
   */
  protected readonly proposals = signal<Preference[]>([]);
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

    effect((onCleanup) => {
      if (!this.session.uid) return;
      const stop = this.store.watchPreferences(
        (preferences) => this.proposals.set(preferences),
        'PROPOSED',
        () => this.proposals.set([]),
      );
      onCleanup(stop);
    });
  }

  ngOnDestroy(): void {
    this.stop?.();
  }

  /**
   * One row per clip, not one per attempt.
   *
   * A remake is a new clip and is always PENDING, and its parent usually still
   * is too — so a football clip corrected three times filled four slots in this
   * queue and left the reviewer working out which was newest. They are one
   * thing to decide about, and the decision is about the latest version.
   *
   * Grouping here rather than in the query because the query is already
   * filtered to one review state: a rejected attempt is not in this result set
   * at all, so the highest version present is by construction the latest one
   * still awaiting a decision. Rejecting a remake therefore brings its parent
   * back on the next snapshot, with no extra bookkeeping and nothing to undo.
   */
  private latestOfEachLineage(clips: Clip[]): Clip[] {
    const best = new Map<string, Clip>();
    // A superseded version is one a decision has already been made about: a
    // later cut of it was approved. It stays PENDING because nothing rewrites
    // `review` on the versions that lost, and until the worker's tidy pass
    // collects it there is a window where it would otherwise reappear here as
    // the only survivor of its lineage — the decision undone by a deletion.
    clips = clips.filter((clip) => !clip.supersededAt);
    for (const clip of clips) {
      // A clip written before lineages existed is its own root.
      const key = clip.lineageId ?? clip.id;
      const held = best.get(key);
      if (!held || (clip.version ?? 1) > (held.version ?? 1)) best.set(key, clip);
    }
    // The listener ordered by createdAt; preserve that among the survivors.
    const keep = new Set([...best.values()].map((c) => c.id));
    return clips.filter((c) => keep.has(c.id));
  }

  /**
   * Keep this preference, or turn it down for good.
   *
   * Accepting one that carries rectangles does a second write, onto the source,
   * and that is where the difference between a note and a rule lives. The rest
   * of what gets learned here is a sentence, and a sentence earns its keep by
   * going into the next note-reading prompt. A rectangle cannot: it has to be
   * somewhere RENDER reads *before* a clip exists, or the reviewer keeps being
   * asked about a logo they have already decided about.
   *
   * Ordered so the rule lands first. If the source write is refused, the
   * preference stays PROPOSED and can be accepted again — which is recoverable.
   * Accepting first and failing second would leave a preference marked kept
   * that does nothing, with nothing on screen to say so.
   */
  protected async decidePreference(
    preference: Preference,
    status: 'ACCEPTED' | 'REJECTED',
  ): Promise<void> {
    const uid = this.session.uid;
    if (!uid) return;
    this.busy.set(preference.id);
    try {
      const regions = preference.defaults?.obscure?.regions ?? [];
      if (status === 'ACCEPTED' && preference.sourceId && regions.length) {
        await this.store.rememberObscure(preference.sourceId, {
          auto: false,
          regions,
          method: null,
          strength: null,
        });
      }
      await this.store.decidePreference(preference.id, uid, status);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.busy.set(null);
    }
  }

  private async buildCards(clips: Clip[]): Promise<void> {
    const local = this.localAvailable();
    this.cards.set(
      await Promise.all(
        this.latestOfEachLineage(clips).map(async (clip) => ({
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
