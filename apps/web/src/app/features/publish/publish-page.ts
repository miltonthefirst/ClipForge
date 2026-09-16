import { DatePipe, DecimalPipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  computed,
  effect,
  inject,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import type { Channel, Clip, ClipPreview, Job, Publication } from '@clipforge/contracts';

import { publishStateOf, type PublishState } from '../../core/publish-state';
import { checkPublishable } from '../../core/publishable';
import { SessionService } from '../../core/session';
import { ClipForgeStore } from '../../core/store';

/**
 * The publish queue: approved clips, and where each one has got to.
 *
 * A **list**, in the same shape as the review queue, and for the same reason it
 * became one. Every row used to carry a full-width poster, a schedule picker
 * and a six-field options panel — a stack of forms rather than a queue, where
 * finding the clip published yesterday meant scrolling past everything that had
 * not been. Deciding what to do next needs the poster, the title and the state;
 * everything else is a click away on the clip's own publish page.
 *
 * The one thing that stayed on the row is Publish itself, because the common
 * case is publishing with the channel's settings and that must remain one tap.
 * The button names the privacy it will use, since `public` is the choice that
 * cannot be taken back.
 */
export interface PublishCard {
  readonly clip: Clip;
  readonly preview: ClipPreview | null;
  readonly publications: Publication[];
}

/** One row, with everything the template needs already worked out. */
export interface PublishRow {
  readonly clip: Clip;
  readonly poster: string | null;
  readonly state: PublishState;
  readonly channel: Channel | null;
  readonly privacy: string;
  /** Why this clip cannot go out, in words the operator can act on. */
  readonly refusal: string | null;
}

@Component({
  selector: 'app-publish-page',
  imports: [DatePipe, DecimalPipe, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './publish-page.html',
})
export class PublishPage implements OnDestroy {
  private readonly store = inject(ClipForgeStore);
  private readonly session = inject(SessionService);

  private stop: (() => void) | null = null;
  private stopChannels: (() => void) | null = null;
  private stopJobs: (() => void) | null = null;

  protected readonly cards = signal<PublishCard[] | null>(null);
  protected readonly channels = signal<Channel[]>([]);
  protected readonly jobs = signal<Job[]>([]);
  protected readonly error = signal<string | null>(null);
  protected readonly busy = signal<string | null>(null);

  constructor() {
    effect((onCleanup) => {
      const uid = this.session.uid;
      if (!uid) {
        this.cards.set(null);
        return;
      }
      const stop = this.store.watchApproved(
        (clips) => void this.buildCards(clips),
        (err) => this.error.set(err.message),
      );
      this.stop = stop;
      onCleanup(stop);
    });

    effect((onCleanup) => {
      if (!this.session.uid) return;
      // Failure is deliberately quiet. Channels supply *defaults*, and a
      // publish with none still works — an error banner here would report a
      // problem the operator does not have.
      const stop = this.store.watchChannels(
        (channels) => this.channels.set(channels),
        () => this.channels.set([]),
      );
      this.stopChannels = stop;
      onCleanup(stop);
    });

    effect((onCleanup) => {
      if (!this.session.uid) return;
      const stop = this.store.watchPublishJobs(
        (jobs) => void this.onJobs(jobs),
        () => this.jobs.set([]),
      );
      this.stopJobs = stop;
      onCleanup(stop);
    });
  }

  ngOnDestroy(): void {
    this.stop?.();
    this.stopChannels?.();
    this.stopJobs?.();
  }

  private async buildCards(clips: Clip[]): Promise<void> {
    this.cards.set(
      await Promise.all(
        clips.map(async (clip) => ({
          clip,
          preview: await this.store.loadPreview(clip.id).catch(() => null),
          publications: await this.store.loadPublications(clip.id).catch(() => []),
        })),
      ),
    );
  }

  /**
   * What the publish jobs looked like last time, as id:status pairs.
   *
   * A snapshot fires for every field a worker touches — a lease extended, an
   * attempt counted — and only a *status* change can have produced a new
   * publication. Comparing the signature is what stops a heartbeat costing a
   * read per clip in the queue.
   */
  private jobSignature = '';

  private async onJobs(jobs: Job[]): Promise<void> {
    this.jobs.set(jobs);

    const signature = jobs
      .map((job) => `${job.id}:${job.status}`)
      .sort()
      .join(',');
    if (signature === this.jobSignature) return;
    const first = this.jobSignature === '';
    this.jobSignature = signature;
    // Nothing to reconcile on the first delivery: `buildCards` has just read
    // every publication there is.
    if (first) return;

    // Only clips somebody actually tried to publish, so this stays bounded by
    // the number of publish jobs rather than by the length of the queue.
    const touched = new Set(jobs.map((job) => job.clipId).filter((id): id is string => !!id));
    const held = this.cards();
    if (!held) return;
    this.cards.set(
      await Promise.all(
        held.map(async (card) =>
          touched.has(card.clip.id)
            ? {
                ...card,
                publications: await this.store
                  .loadPublications(card.clip.id)
                  .catch(() => card.publications),
              }
            : card,
        ),
      ),
    );
  }

  /** The channel a publish would go to when nobody chooses one. */
  private defaultChannel(): Channel | null {
    const all = this.channels();
    if (all.length === 0) return null;
    return all.find((channel) => channel.isDefault) ?? all[0]!;
  }

  /**
   * The rows, in the order the queue listener delivered them.
   *
   * `new Date()` is read here rather than held in a ticking signal: the only
   * thing it decides is whether a scheduled publish is still in the future, and
   * this recomputes whenever a clip, a publication or a job changes — which is
   * every occasion on which that answer can change what to do.
   */
  protected readonly rows = computed<PublishRow[] | null>(() => {
    const cards = this.cards();
    if (cards === null) return null;
    const jobs = this.jobs();
    const channel = this.defaultChannel();
    const now = new Date();

    return cards.map((card) => ({
      clip: card.clip,
      poster: card.preview ? `data:image/jpeg;base64,${card.preview.posterBase64}` : null,
      state: publishStateOf(
        card.publications,
        jobs.filter((job) => job.clipId === card.clip.id),
        now,
      ),
      channel,
      privacy: channel?.defaults.privacy ?? 'unlisted',
      refusal: checkPublishable(card.clip)?.message ?? null,
    }));
  });

  /** Still to go out — the part of this screen that is actually a queue. */
  protected readonly waiting = computed(
    () => this.rows()?.filter((row) => row.state.kind !== 'PUBLISHED') ?? [],
  );

  /**
   * Already on a platform.
   *
   * Kept on this page rather than hidden away, because "what did we put out,
   * and when" is asked from here more often than from anywhere else — and
   * because a queue that silently drops a clip the moment it succeeds gives the
   * operator no confirmation that it ever did.
   */
  protected readonly done = computed(
    () => this.rows()?.filter((row) => row.state.kind === 'PUBLISHED') ?? [],
  );

  protected readonly empty = computed(() => this.rows()?.length === 0);

  /**
   * The privacy an untouched publish would use, for the line at the top.
   *
   * Read from the channel rather than hard-coded, because a channel whose
   * default is `public` makes that sentence a warning rather than reassurance —
   * and a page that said "unlisted" regardless would be actively misleading.
   */
  protected readonly defaultPrivacy = computed(
    () => this.defaultChannel()?.defaults.privacy ?? 'unlisted',
  );

  /**
   * Publish with the channel's settings and nothing else.
   *
   * No options block is sent, and that is not the same as sending one full of
   * nulls: nulls would pin this upload to today's defaults, so a correction
   * made to the channel before a scheduled job ran would be ignored.
   *
   * Nothing is shown to confirm it, because the row itself changes: the job
   * this creates is what the publish-job listener reads, and the state chip
   * says "waiting for the worker" a moment later. The old confirmation line
   * lived only in memory and vanished on reload, which is how a queued clip
   * came to look untouched.
   */
  protected async publish(row: PublishRow): Promise<void> {
    const uid = this.session.uid;
    if (!uid || row.refusal) return;

    this.busy.set(row.clip.id);
    this.error.set(null);
    try {
      await this.store.requestPublish(uid, row.clip.id, null, null);
    } catch (err) {
      // The rules refuse this if the clip is not approved. Saying so beats
      // showing a raw permission error.
      this.error.set(
        err instanceof Error && err.message.includes('permission')
          ? 'The publish gate refused this. Check the clip is still approved.'
          : err instanceof Error
            ? err.message
            : String(err),
      );
    } finally {
      this.busy.set(null);
    }
  }
}
