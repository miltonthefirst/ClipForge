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
import { DecimalPipe, LowerCasePipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import type { Candidate, Clip, ClipPreview, Preference } from '@clipforge/contracts';

import { ClipsRepository } from '../../core/data/clips';
import { PreferencesRepository } from '../../core/data/preferences';
import type { Live } from '../../core/firestore/gateway';
import { PlaybackService, type PlaybackSource } from '../../core/playback';
import { SessionService } from '../../core/session';

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
  private readonly clips = inject(ClipsRepository);
  private readonly preferences = inject(PreferencesRepository);
  private readonly session = inject(SessionService);
  private readonly playback = inject(PlaybackService);

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
  /**
   * Why the queue is not here, when it is never going to arrive.
   *
   * A third state, distinct from "still loading" and from "loaded and empty",
   * because the page could previously only express two. A failed query delivers
   * nothing at all, so `cards` stayed null and the page went on saying "Loading
   * the queue…" for as long as anyone left it open — under an error banner that
   * never explained why the list below it had not arrived.
   *
   * Separate from {@link error}, which is the banner for something the reviewer
   * just pressed. This one replaces the queue, because it is about the queue.
   */
  protected readonly loadFailure = signal<string | null>(null);
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

  /**
   * The two listeners this page holds, released when it goes.
   *
   * Signals rather than plain fields because the effects below *read* them: a
   * plain field is not tracked, so a delivering effect would run once against
   * no listener and never again — the queue would render nothing for ever, and
   * nothing on screen would say why.
   */
  private readonly heldQueue = signal<Live<Clip[]> | null>(null);
  private readonly heldProposals = signal<Live<Preference[]> | null>(null);

  constructor() {
    void this.playback.probeLocalServer().then((ok) => {
      this.localAvailable.set(ok);
    });

    effect(() => {
      const uid = this.session.uid;
      // Cleared first, and unconditionally: a failure belongs to the listener
      // that produced it, and this effect is about to replace that listener —
      // including with no listener at all, on the way out of the app.
      this.loadFailure.set(null);

      // Let go of the previous listener before taking the next. The gate keeps
      // it warm for fifteen minutes past its last reader, so Review → Clip →
      // Review re-attaches to the listener already open and pays for none of
      // those fifty documents again — which is why releasing is not the same as
      // unsubscribing.
      // `untracked`, or this effect depends on the signal it is about to write,
      // re-runs itself for ever, and releases and re-opens the queue on every
      // pass — a hang rather than a leak.
      untracked(() => this.heldQueue())?.release();
      this.heldQueue.set(null);
      this.cards.set(null);

      if (!uid) return;
      this.heldQueue.set(this.clips.watchReviewQueue('PENDING'));
    });

    // Separate from the subscription above so that a delivery is not a reason
    // to re-subscribe: this one reads the held signals and nothing else, and
    // reading `heldQueue` inside the effect that assigns it would make every
    // snapshot re-open the listener it had just been delivered from.
    effect(() => {
      const held = this.heldQueue();
      if (!held) return;
      const failure = held.error();
      if (failure) {
        // The end of the road for this listener: `onSnapshot` does not retry
        // after an error, so nothing further arrives until a navigation
        // re-subscribes. Said out loud, because the alternative is `cards`
        // staying null and the page going on claiming to be loading.
        this.loadFailure.set(
          failure.message || 'The review queue could not be loaded, and gave no reason.',
        );
        return;
      }
      const clips = held.data();
      // Still loading is not the same as delivered-and-empty, and only the
      // second one means "Nothing waiting for review".
      if (clips === null) return;
      void this.buildCards(clips);
    });

    effect(() => {
      const uid = this.session.uid;
      untracked(() => this.heldProposals())?.release();
      this.heldProposals.set(null);
      this.proposals.set([]);
      if (!uid) return;
      this.heldProposals.set(this.preferences.watch('PROPOSED'));
    });

    effect(() => {
      const held = this.heldProposals();
      if (!held) return;
      // A failure here empties the panel and says nothing else, on purpose.
      // These are suggestions nobody asked for; an error about them across the
      // top of the queue would push the work the reviewer did come here to do
      // down the page, over something they cannot act on anyway.
      if (held.error()) {
        this.proposals.set([]);
        return;
      }
      const proposed = held.data();
      // Not loaded yet, which is not the same as nothing proposed — both hide
      // the panel, but only the second one is an answer.
      if (proposed === null) return;
      this.proposals.set(proposed);
    });
  }

  ngOnDestroy(): void {
    this.heldQueue()?.release();
    this.heldProposals()?.release();
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
   * One call, although accepting one that carries rectangles is two writes —
   * the rule onto the source, and then the decision. Which order they go in,
   * and why the source has to be first, is a property of the writes rather than
   * of this button: see `PreferencesRepository.decide`.
   */
  protected async decidePreference(
    preference: Preference,
    status: 'ACCEPTED' | 'REJECTED',
  ): Promise<void> {
    const uid = this.session.uid;
    if (!uid) return;
    this.busy.set(preference.id);
    try {
      await this.preferences.decide(preference, uid, status);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.busy.set(null);
    }
  }

  /**
   * What each clip's card needed, kept between deliveries.
   *
   * A poster, a candidate and a playback URL are facts about one clip, and the
   * clip does not change — but the queue listener re-delivers its whole result
   * set whenever a worker touches any row in it, and a lease renewed every
   * thirty seconds is enough to do that. Rebuilding from scratch each time cost
   * three round trips per card, every time, on a phone that had them all
   * already.
   *
   * Keyed by clip id and pruned to what is on screen, so a queue worked through
   * over an hour does not accumulate the cards it has finished with.
   */
  private readonly built = new Map<string, ReviewCard>();

  private async buildCards(clips: Clip[]): Promise<void> {
    // Read untracked. This runs inside the delivering effect, and the probe in
    // the constructor answers a moment after the page opens: a tracked read
    // would make that answer re-run the effect and rebuild every card, paying
    // for a poster and a candidate per row a second time.
    const local = untracked(() => this.localAvailable());
    const wanted = this.latestOfEachLineage(clips);

    // Only what is new. The rest are already in hand and cost nothing.
    const missing = wanted.filter((clip) => !this.built.has(clip.id));
    const fresh = await Promise.all(missing.map((clip) => this.buildCard(clip, local)));
    for (const card of fresh) this.built.set(card.clip.id, card);

    // The clip object itself may have moved on — a title edited, a decision
    // recorded — so the row takes the newest one and keeps the fetched parts.
    const cards = wanted.map((clip) => ({ ...this.built.get(clip.id)!, clip }));

    const onScreen = new Set(wanted.map((clip) => clip.id));
    for (const id of [...this.built.keys()]) {
      if (!onScreen.has(id)) this.built.delete(id);
    }

    this.cards.set(cards);
  }

  private async buildCard(clip: Clip, local: boolean): Promise<ReviewCard> {
    // Together rather than in sequence: three independent answers, and on a
    // phone each one is a round trip. Awaiting them one after another made a
    // card take as long as the three of them added up.
    const [source, preview, candidate] = await Promise.all([
      this.playback.resolve(clip, local),
      // Fetched per card rather than with the list: the poster is ~50 KB of
      // base64, and the queue listener re-delivers its whole result set on
      // every reconnect.
      this.clips.loadPreview(clip.id).catch(() => null),
      this.clips.loadCandidate(clip.candidateId).catch(() => null),
    ]);
    return { clip, source, preview, candidate };
  }

  protected posterSrc(card: ReviewCard): string | null {
    return card.preview ? `data:image/jpeg;base64,${card.preview.posterBase64}` : null;
  }

  protected async decide(clip: Clip, review: 'APPROVED' | 'REJECTED'): Promise<void> {
    this.busy.set(clip.id);
    try {
      await this.clips.review(clip.id, review, clip.storagePath);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.busy.set(null);
    }
  }
}
