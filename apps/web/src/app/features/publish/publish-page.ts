import { DatePipe, DecimalPipe } from '@angular/common';
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
import type { Channel, Clip, ClipPreview, Job, Publication } from '@clipforge/contracts';

import { ClipsRepository } from '../../core/data/clips';
import { JobsRepository } from '../../core/data/jobs';
import { PublishingRepository } from '../../core/data/publishing';
import type { Live } from '../../core/firestore/gateway';
import { publishStateOf, type PublishState } from '../../core/publish-state';
import { checkPublishable } from '../../core/publishable';
import { SessionService } from '../../core/session';

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
  private readonly clips = inject(ClipsRepository);
  private readonly publishing = inject(PublishingRepository);
  private readonly jobs = inject(JobsRepository);
  private readonly session = inject(SessionService);

  protected readonly cards = signal<PublishCard[] | null>(null);
  /**
   * The destinations, and what each one publishes as by default.
   *
   * `null` until the listener has answered, which is not the same as "there are
   * no channels": an empty array is a workspace with nothing connected, and the
   * page says `unlisted` for it on purpose. Guessing that while the answer was
   * still on its way is how the button came to name a privacy the upload would
   * not have used — see {@link rows}.
   */
  protected readonly channels = signal<Channel[] | null>(null);
  /** Publish jobs, on the same terms: `null` is unknown, `[]` is none. */
  protected readonly publishJobs = signal<Job[] | null>(null);
  protected readonly error = signal<string | null>(null);
  /**
   * Why the queue is not here, when it is never going to arrive.
   *
   * A third state, distinct from "still loading" and from "loaded and empty",
   * because the page could previously only express two. A failed query delivers
   * nothing at all, so `cards` stayed null and the page went on saying "Loading
   * approved clips…" underneath the banner for as long as anyone left it open.
   *
   * Separate from {@link error}, which is the banner for something the operator
   * just pressed. This one replaces the list, because it is about the list.
   */
  protected readonly loadFailure = signal<string | null>(null);
  protected readonly busy = signal<string | null>(null);

  /**
   * The three listeners this page holds, released when it goes.
   *
   * Signals rather than plain fields because the effects below *read* them: a
   * plain field is not tracked, so a delivering effect would run once against
   * no listener and never again, and the queue would sit empty for ever.
   */
  private readonly heldClips = signal<Live<Clip[]> | null>(null);
  private readonly heldChannels = signal<Live<Channel[]> | null>(null);
  private readonly heldJobs = signal<Live<Job[]> | null>(null);

  constructor() {
    // ── Approved clips ──────────────────────────────────────────────────────
    effect(() => {
      const uid = this.session.uid;
      // Cleared first, and unconditionally: a failure belongs to the listener
      // that produced it, and this effect is about to replace that listener —
      // including with no listener at all, on the way out of the app.
      this.loadFailure.set(null);

      // Let go of the previous listener before taking the next one. The gate
      // keeps it warm for fifteen minutes, so leaving this page and coming back
      // re-attaches to the same one and costs nothing — which is the whole
      // reason releasing is not the same as unsubscribing.
      // `untracked`, or this effect depends on the signal it is about to write
      // and re-runs itself for ever — releasing and re-opening a listener on
      // every pass, which is a hang rather than a leak.
      untracked(() => this.heldClips())?.release();
      this.heldClips.set(null);
      this.cards.set(null);

      if (!uid) return;
      this.heldClips.set(this.clips.watchApproved());
    });

    // Separate from the subscription above so that a delivery does not re-open
    // a listener: this one reads the held signals and nothing else, and reading
    // `heldClips` inside the effect that assigns it would make every snapshot a
    // reason to re-subscribe. The same division holds for the two pairs below.
    effect(() => {
      const held = this.heldClips();
      if (!held) return;
      const failure = held.error();
      if (failure) {
        this.loadFailure.set(approvedFailure(failure));
        return;
      }
      const clips = held.data();
      // Still loading is not the same as delivered-and-empty, and only the
      // second is news. Acting on the first is what put "Loading approved
      // clips…" on screen behind a query that had already failed.
      if (clips === null) return;
      void this.buildCards(clips);
    });

    // ── Channels ────────────────────────────────────────────────────────────
    effect(() => {
      const uid = this.session.uid;
      untracked(() => this.heldChannels())?.release();
      this.heldChannels.set(null);
      this.channels.set(null);

      if (!uid) return;
      this.heldChannels.set(this.publishing.watchChannels());
    });

    effect(() => {
      const held = this.heldChannels();
      if (!held) return;
      if (held.error()) {
        // Failure is deliberately quiet. Channels supply *defaults*, and a
        // publish with none still works — an error banner here would report a
        // problem the operator does not have.
        //
        // Quiet as "no channels" rather than as "not answered yet", though:
        // the rows wait for this listener, so leaving it null would hold the
        // whole queue at "loading" behind a question that will never be
        // answered.
        this.channels.set([]);
        return;
      }
      const channels = held.data();
      if (channels === null) return;
      this.channels.set(channels);
    });

    // ── Publish jobs ────────────────────────────────────────────────────────
    effect(() => {
      const uid = this.session.uid;
      untracked(() => this.heldJobs())?.release();
      this.heldJobs.set(null);
      this.publishJobs.set(null);
      this.jobSignature = null;

      if (!uid) return;
      this.heldJobs.set(this.jobs.watchPublishJobs());
    });

    effect(() => {
      const held = this.heldJobs();
      if (!held) return;
      if (held.error()) {
        // The same trade the channels listener makes, for the same reason:
        // silent, and empty rather than unknown, so a queue that cannot read
        // the publish jobs still renders from the publications alone — which is
        // all it had before this listener existed.
        this.publishJobs.set([]);
        return;
      }
      const jobs = held.data();
      if (jobs === null) return;
      void this.onJobs(jobs);
    });
  }

  ngOnDestroy(): void {
    this.heldClips()?.release();
    this.heldChannels()?.release();
    this.heldJobs()?.release();
  }

  private async buildCards(clips: Clip[]): Promise<void> {
    this.cards.set(
      await Promise.all(
        clips.map(async (clip) => ({
          clip,
          preview: await this.clips.loadPreview(clip.id).catch(() => null),
          publications: await this.publishing.loadPublications(clip.id).catch(() => []),
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
   *
   * `null` rather than `''` for "nothing delivered yet", because an empty queue
   * of publish jobs has the signature `''` too: the delivery after an empty
   * first one looked like a first delivery as well, and was skipped with it.
   */
  private jobSignature: string | null = null;

  private async onJobs(jobs: Job[]): Promise<void> {
    this.publishJobs.set(jobs);

    const signature = jobs
      .map((job) => `${job.id}:${job.status}`)
      .sort()
      .join(',');
    if (signature === this.jobSignature) return;
    const first = this.jobSignature === null;
    this.jobSignature = signature;
    // Nothing to reconcile on the first delivery: the clips listener re-opened
    // alongside this one, so `buildCards` has just read every publication there
    // is.
    if (first) return;

    // Only clips somebody actually tried to publish, so this stays bounded by
    // the number of publish jobs rather than by the length of the queue.
    const touched = new Set(jobs.map((job) => job.clipId).filter((id): id is string => !!id));
    // `untracked`, because this runs inside the delivering effect and both
    // reads `cards` and writes it back. Tracked, the effect would depend on the
    // signal it is about to write and re-run itself on its own reconcile.
    const held = untracked(() => this.cards());
    if (!held) return;
    this.cards.set(
      await Promise.all(
        held.map(async (card) =>
          touched.has(card.clip.id)
            ? {
                ...card,
                publications: await this.publishing
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
    if (!all || all.length === 0) return null;
    return all.find((channel) => channel.isDefault) ?? all[0]!;
  }

  /**
   * The rows, in the order the queue listener delivered them.
   *
   * All three listeners or nothing. `null` from any of them means it has not
   * answered yet, which is not the same as answering with nothing, and a row
   * built on the difference is wrong in a way the operator acts on: without the
   * publish jobs it offers Publish on a clip that is already queued — the
   * double upload `watchPublishJobs` exists to prevent — and without the
   * channels it names `unlisted` on the button that performs it while the
   * default is `public`. A listener that fails delivers `[]` instead of staying
   * null, so neither can hold this at null for ever.
   *
   * `new Date()` is read here rather than held in a ticking signal: the only
   * thing it decides is whether a scheduled publish is still in the future, and
   * this recomputes whenever a clip, a publication or a job changes — which is
   * every occasion on which that answer can change what to do.
   */
  protected readonly rows = computed<PublishRow[] | null>(() => {
    const cards = this.cards();
    const publishJobs = this.publishJobs();
    if (cards === null || publishJobs === null || this.channels() === null) return null;
    const channel = this.defaultChannel();
    const now = new Date();

    return cards.map((card) => ({
      clip: card.clip,
      poster: card.preview ? `data:image/jpeg;base64,${card.preview.posterBase64}` : null,
      state: publishStateOf(
        card.publications,
        publishJobs.filter((job) => job.clipId === card.clip.id),
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
      await this.publishing.requestPublish(uid, row.clip.id, null, null);
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

/**
 * What to say when the approved-clips listener fails outright.
 *
 * A listener does not retry after an error, so this is the end of the road for
 * the page until a navigation re-subscribes it — which is why it replaces the
 * queue rather than sitting above it as a banner.
 *
 * Permission gets its own sentence because Firestore's own words for it name
 * nothing the operator can do next. Everything else keeps the raw message: an
 * unrecognised failure rewritten into a friendly sentence is how a real cause
 * gets hidden.
 */
function approvedFailure(error: Error): string {
  if ((error as { code?: string }).code === 'permission-denied') {
    return 'This account is not allowed to read approved clips.';
  }
  return error.message || 'The publish queue could not be loaded, and gave no reason.';
}
