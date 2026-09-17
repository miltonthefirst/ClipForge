import { DatePipe, DecimalPipe } from '@angular/common';
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
import { RouterLink } from '@angular/router';
import type {
  Channel,
  Clip,
  ClipPreview,
  Job,
  MetricSnapshot,
  Publication,
  PublishOptions,
  PublishPrivacy,
} from '@clipforge/contracts';

import { ClipsRepository } from '../../core/data/clips';
import { JobsRepository } from '../../core/data/jobs';
import { PublishingRepository } from '../../core/data/publishing';
import type { Live } from '../../core/firestore/gateway';
import { PlaybackService, type PlaybackSource } from '../../core/playback';
import { publishStateOf, type PublishState } from '../../core/publish-state';
import { checkPublishable } from '../../core/publishable';
import { SessionService } from '../../core/session';
import {
  CATEGORIES,
  DEFAULT_CATEGORY_ID,
  PRIVACY_OPTIONS,
  categoryLabel,
} from '../../core/youtube';
import { rollUp, toCurves, type PublicationRollup } from '../insights/rollup';

/** The retention sparkline's box, in user units. */
const CURVE_WIDTH = 260;
const CURVE_HEIGHT = 64;

/**
 * One clip on its way to a platform, and everything that happened to it.
 *
 * The publish queue answers "which of these should go out next"; this answers
 * everything after that, and it is the screen the queue was previously trying
 * to be. A row that carried a schedule picker, a privacy radio group, a
 * category select and a tag field was a form pretending to be a list — and it
 * still could not show the two things most often wanted about a published clip:
 * what actually went out, and what happened to it afterwards.
 *
 * So: the clip plays here, large. Under it is what this upload will say, the
 * record of every attempt, the numbers the platform reported back, the versions
 * this clip was made from, and the jobs the worker ran. Deciding, correcting
 * and re-cutting stay on the review page — this one is about the delivery.
 */
@Component({
  selector: 'app-publication-page',
  imports: [DatePipe, DecimalPipe, FormsModule, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './publication-page.html',
})
export class PublicationPage implements OnDestroy {
  private readonly clips = inject(ClipsRepository);
  private readonly publishing = inject(PublishingRepository);
  private readonly queue = inject(JobsRepository);
  private readonly session = inject(SessionService);
  private readonly playback = inject(PlaybackService);

  /** From the route: `publish/:id`. */
  readonly id = input.required<string>();

  /**
   * The four listeners this page holds, released when they are replaced.
   *
   * Signals rather than plain fields because the delivering effects below
   * *read* them: a plain field is not tracked, so each of those would run once
   * against no listener and never again, and the page would sit empty for ever
   * with nothing to show for it.
   */
  private readonly heldClip = signal<Live<Clip> | null>(null);
  private readonly heldPublications = signal<Live<Publication[]> | null>(null);
  private readonly heldChannels = signal<Live<Channel[]> | null>(null);
  private readonly heldJobs = signal<Live<Job[]> | null>(null);

  protected readonly categories = CATEGORIES;
  protected readonly privacies = PRIVACY_OPTIONS;
  protected readonly categoryLabel = categoryLabel;
  protected readonly curveWidth = CURVE_WIDTH;
  protected readonly curveHeight = CURVE_HEIGHT;

  /**
   * The clip: `undefined` while it loads, `null` when there is no such clip.
   *
   * Three states rather than two, and the listener now supplies all three. A
   * live document reads null both before its first snapshot and when the
   * document does not exist, so `loading()` is what separates "on its way"
   * from "gone" — see the delivering effect below.
   */
  protected readonly clip = signal<Clip | null | undefined>(undefined);
  protected readonly preview = signal<ClipPreview | null>(null);
  // ── Listened-for lists ────────────────────────────────────────────────────
  //
  // `null` means NOT DELIVERED YET on all three, and never "there are none".
  // Empty is `[]`. They started at `[]`, which read as "nothing has ever
  // happened to this clip" the moment a query was slow or had already failed —
  // and for {@link publications} and {@link jobs} that is the one answer that
  // offers Publish for a clip with an upload already on its way.
  protected readonly publications = signal<Publication[] | null>(null);
  protected readonly channels = signal<Channel[] | null>(null);
  /** Every publish job in the workspace. Which of them are this clip's: {@link jobs}. */
  private readonly publishJobs = signal<Job[] | null>(null);

  /**
   * This clip's publish jobs, filtered rather than queried.
   *
   * A derivation and not a signal an effect writes, so that changing clip
   * re-filters the list the page already holds instead of waiting for a
   * snapshot that is not coming — the listener is the same one either way, and
   * nothing about it changed.
   */
  protected readonly jobs = computed<Job[] | null>(() => {
    const all = this.publishJobs();
    if (all === null) return null;
    const id = this.id();
    return all.filter((job) => job.clipId === id);
  });
  /** Every job that ever touched this clip, not only the publishes. */
  protected readonly history = signal<Job[]>([]);
  protected readonly metrics = signal<MetricSnapshot[] | null>(null);
  protected readonly lineage = signal<Clip[]>([]);
  protected readonly source = signal<PlaybackSource>({ kind: 'poster' });
  protected readonly localAvailable = signal(false);
  protected readonly uploadRequested = signal(false);

  /** The banner for something the operator just pressed. */
  protected readonly error = signal<string | null>(null);
  protected readonly busy = signal(false);
  protected readonly saved = signal<string | null>(null);
  protected readonly showOptions = signal(false);

  // ── Why something is not here ─────────────────────────────────────────────
  //
  // A failed query delivers nothing at all, which is indistinguishable from a
  // slow one unless the page keeps the reason somewhere. Separate from
  // {@link error}, which is about an action: these are about a read, and each
  // belongs to the listener that produced it so that replacing that listener
  // clears its failure and nobody else's.

  /** Why the clip itself is not here. Replaces the whole page. */
  protected readonly clipFailure = signal<string | null>(null);
  private readonly publicationsFailure = signal<string | null>(null);
  private readonly jobsFailure = signal<string | null>(null);
  /**
   * Why this clip's publish state is unknown, from either half of it.
   *
   * One line on screen because it has one consequence: without both halves the
   * page cannot say what has already happened, so it withholds the button that
   * would happen again.
   */
  protected readonly statusFailure = computed(
    () => this.publicationsFailure() ?? this.jobsFailure(),
  );
  /** Why the destination picker is empty, and what is shown in its place. */
  protected readonly channelsFailure = signal<string | null>(null);

  // ── Drafts ─────────────────────────────────────────────────────────────────
  //
  // One clip means one draft, so these are plain signals. `undefined` means
  // "never touched" and is not the same as an empty string: an emptied tag
  // field says "no tags on this upload", which must not fall through to the
  // channel's.
  protected readonly draftChannel = signal<string | undefined>(undefined);
  protected readonly draftTitle = signal<string | undefined>(undefined);
  protected readonly draftDescription = signal<string | undefined>(undefined);
  protected readonly draftPrivacy = signal<PublishPrivacy | undefined>(undefined);
  protected readonly draftCategory = signal<string | undefined>(undefined);
  protected readonly draftTags = signal<string | undefined>(undefined);
  protected readonly draftSchedule = signal('');

  /**
   * Subscribing and delivering, kept apart.
   *
   * Three effects take listeners and four hand their snapshots to the page,
   * and the split is not tidiness: an effect that both opened a listener and
   * read what it delivered would treat every delivered snapshot as a reason to
   * open the listener again.
   *
   * No delivery can outrun the reset that belongs with it, and that holds
   * without relying on the order effects happen to run in: a delivering effect
   * reads nothing but the handle its subscriber writes, so it is only made
   * dirty by that write — which happens after {@link resetForClip} has cleared
   * what the previous clip left behind.
   */
  constructor() {
    void this.playback.probeLocalServer().then((ok) => this.localAvailable.set(ok));

    // ── Subscribing ─────────────────────────────────────────────────────────

    effect(() => {
      const id = this.id();
      // Clears the previous clip's failures as well as its data: a failure
      // belongs to the listener that produced it, and both are replaced here.
      this.resetForClip();

      // Let go before taking the next one. The gate keeps a listener warm for
      // fifteen minutes past its last reader, so stepping to the next clip and
      // back re-attaches to the same listener and pays nothing — which is why
      // releasing is not the same as unsubscribing.
      // `untracked`, or this effect depends on the signals it is about to
      // write and re-runs itself for ever, releasing and re-opening on every
      // pass — a hang rather than a leak.
      untracked(() => {
        this.heldClip()?.release();
        this.heldPublications()?.release();
      });
      this.heldClip.set(null);
      this.heldPublications.set(null);

      this.heldClip.set(this.clips.watchClip(id));
      // Live rather than loaded once. The worker writes a PENDING publication
      // before it calls YouTube and stamps it PUBLISHED after, so a page that
      // read once sits on "uploading" until somebody reloads it — which is
      // exactly when an operator concludes the worker is stuck.
      this.heldPublications.set(this.publishing.watchPublications(id));
    });

    effect(() => {
      const uid = this.session.uid;
      this.channelsFailure.set(null);
      untracked(() => this.heldChannels())?.release();
      this.heldChannels.set(null);
      if (!uid) {
        this.channels.set(null);
        return;
      }
      this.heldChannels.set(this.publishing.watchChannels());
    });

    effect(() => {
      const uid = this.session.uid;
      this.jobsFailure.set(null);
      untracked(() => this.heldJobs())?.release();
      this.heldJobs.set(null);
      if (!uid) {
        this.publishJobs.set(null);
        return;
      }
      // Not keyed on the clip: this is every publish job, and which of them
      // belong to this clip is {@link jobs}'s business. A `clipId` listener per
      // page would cost a second index for a query the queue already runs, and
      // the gate hands both pages the same listener.
      this.heldJobs.set(this.queue.watchPublishJobs());
    });

    // ── Delivering ──────────────────────────────────────────────────────────

    effect(() => {
      const held = this.heldClip();
      if (!held) return;
      const failure = held.error();
      if (failure) {
        this.clipFailure.set(`Could not load this clip: ${failure.message}`);
        return;
      }
      const clip = held.data();
      // A document listener reads null both before its first snapshot and when
      // there is no such document, so this is the one read that has to consult
      // `loading()`: without it the page says "No such clip" over a clip that
      // is still on its way.
      if (clip === null && held.loading()) return;
      this.clip.set(clip);
      if (clip?.storagePath) this.uploadRequested.set(false);
      // `untracked`: hydrate reads signals synchronously — the local-server
      // flag it hands the resolver — and this effect taking a dependency on
      // those would re-run it every time the five-second probe changed one.
      if (clip) untracked(() => void this.hydrate(clip));
    });

    effect(() => {
      const held = this.heldPublications();
      if (!held) return;
      const failure = held.error();
      if (failure) {
        this.publicationsFailure.set(
          `Could not read this clip's publish history: ${failure.message}`,
        );
        return;
      }
      const publications = held.data();
      // Not yet delivered is not the same as "nothing has ever been published",
      // and only the second is something to say out loud.
      if (publications === null) return;
      this.publications.set(publications);
      // A publication landing is the one event that can produce metrics.
      // `untracked` because loadMetrics both reads and writes `metrics`, and an
      // effect that depended on what it writes would run again to no purpose.
      if (publications.some((p) => p.state === 'PUBLISHED')) {
        untracked(() => void this.loadMetrics(this.id()));
      }
    });

    effect(() => {
      const held = this.heldChannels();
      if (!held) return;
      const failure = held.error();
      if (failure) {
        this.channelsFailure.set(`Could not read the channel list: ${failure.message}`);
        return;
      }
      const channels = held.data();
      if (channels === null) return;
      this.channels.set(channels);
    });

    effect(() => {
      const held = this.heldJobs();
      if (!held) return;
      const failure = held.error();
      if (failure) {
        this.jobsFailure.set(`Could not read the publish queue: ${failure.message}`);
        return;
      }
      const jobs = held.data();
      if (jobs === null) return;
      this.publishJobs.set(jobs);
    });
  }

  ngOnDestroy(): void {
    this.heldClip()?.release();
    this.heldPublications()?.release();
    this.heldChannels()?.release();
    this.heldJobs()?.release();
    if (this.retryPlayback) clearInterval(this.retryPlayback);
  }

  /**
   * Forget the clip we were just looking at.
   *
   * `publish/:id` is one route config, so the router reuses this component when
   * only the id changes — `withComponentInputBinding` updates the input signal
   * and nothing else. The drafts are the part that loses work: an unsaved title
   * typed against one clip would otherwise follow you to the next and be sent
   * with its upload.
   */
  private resetForClip(): void {
    this.clip.set(undefined);
    this.preview.set(null);
    // Back to "not delivered yet", which is what this means while the next
    // clip's listener is still opening. `[]` here would say the new clip had
    // been looked at and found to have no publish history. Its jobs need no
    // line: they are derived from a listener this does not disturb.
    this.publications.set(null);
    this.history.set([]);
    this.metrics.set(null);
    this.lineage.set([]);
    this.source.set({ kind: 'poster' });
    this.uploadRequested.set(false);
    this.error.set(null);
    this.clipFailure.set(null);
    this.publicationsFailure.set(null);
    this.saved.set(null);
    this.showOptions.set(false);

    this.draftChannel.set(undefined);
    this.draftTitle.set(undefined);
    this.draftDescription.set(undefined);
    this.draftPrivacy.set(undefined);
    this.draftCategory.set(undefined);
    this.draftTags.set(undefined);
    this.draftSchedule.set('');
  }

  /**
   * Load the things that hang off a clip, once.
   *
   * Guarded on each, so a live clip update — a publication landing, a review
   * note saved elsewhere — does not re-fetch a poster that cannot have changed.
   */
  private async hydrate(clip: Clip): Promise<void> {
    this.source.set(await this.playback.resolve(clip, this.localAvailable()));
    if (this.source().kind === 'poster') this.watchForPlayback();

    if (!this.preview()) {
      this.preview.set(await this.clips.loadPreview(clip.id).catch(() => null));
    }
    if (!this.lineage().length) {
      this.lineage.set(await this.clips.loadLineage(clip.lineageId ?? clip.id).catch(() => []));
    }
    if (!this.history().length) {
      this.history.set(await this.queue.loadJobsForClip(clip.id).catch(() => []));
    }
  }

  private async loadMetrics(clipId: string): Promise<void> {
    if (this.metrics() !== null) return;
    this.metrics.set(await this.clips.loadMetricsForClip(clipId).catch(() => []));
  }

  /**
   * Keep asking, but only while there is nothing to play.
   *
   * Two things can make an unplayable clip playable without its document
   * changing: the worker being started, which brings its file server up, and an
   * UPLOAD job landing. Stops itself the moment something plays, so the common
   * case costs nothing.
   */
  private retryPlayback: ReturnType<typeof setInterval> | null = null;

  private watchForPlayback(): void {
    if (this.retryPlayback) return;
    this.retryPlayback = setInterval(() => {
      const clip = this.clip();
      if (!clip || this.source().kind !== 'poster') {
        if (this.retryPlayback) clearInterval(this.retryPlayback);
        this.retryPlayback = null;
        return;
      }
      void this.playback.probeLocalServer().then(async (ok) => {
        this.localAvailable.set(ok);
        this.source.set(await this.playback.resolve(clip, ok));
      });
    }, 5_000);
  }

  // ── Derived ────────────────────────────────────────────────────────────────

  protected readonly posterSrc = computed(() => {
    const preview = this.preview();
    return preview ? `data:image/jpeg;base64,${preview.posterBase64}` : null;
  });

  protected readonly filmstripSrc = computed(() => {
    const preview = this.preview();
    return preview?.filmstripBase64 ? `data:image/jpeg;base64,${preview.filmstripBase64}` : null;
  });

  protected readonly playable = computed(() => {
    const kind = this.source().kind;
    return kind === 'remote' || kind === 'local' || kind === 'bucket';
  });

  /**
   * Whether this clip would be unplayable on a device that is not this one.
   *
   * Deliberately not "can I play it here". Playing locally is the case where
   * offering the upload matters most: it plays fine at the desk, and the phone
   * this will actually be checked on cannot reach the worker at all.
   */
  protected readonly needsCloudCopy = computed(() => {
    const clip = this.clip();
    if (!clip) return false;
    if (this.source().kind === 'remote') return false;
    return !this.playback.bucketCopyLive(clip);
  });

  protected readonly blockedReason = computed(() => {
    const source = this.source();
    return source.kind === 'blocked' ? source.reason : null;
  });

  /**
   * Where this clip has got to, or null while that is still unknown.
   *
   * Both halves are required, and neither may be stood in for by an empty
   * list. A publication is what the worker wrote; a PUBLISH job is the only
   * evidence of an upload that has been asked for and not yet started
   * (core/publish-state.ts). Read as empty, a list that has not arrived — or
   * that failed — says "nothing has ever happened to this clip", which is the
   * one answer that invites a second upload of a video already on its way.
   */
  protected readonly state = computed<PublishState | null>(() => {
    const publications = this.publications();
    const jobs = this.jobs();
    if (publications === null || jobs === null) return null;
    return publishStateOf(publications, jobs, new Date());
  });

  /**
   * Whether to show what this upload would say, and the button that sends it.
   *
   * Withheld while {@link state} is unknown, for the reason given there. The
   * two states it hides for besides are the ones where there is nothing left to
   * decide: the clip is on YouTube, or the worker has it.
   */
  protected readonly offerPublish = computed(() => {
    const state = this.state();
    return state !== null && state.kind !== 'PUBLISHED' && state.kind !== 'UPLOADING';
  });

  /** Why this clip may not be published, or null. */
  protected readonly refusal = computed(() => {
    const clip = this.clip();
    return clip ? (checkPublishable(clip)?.message ?? null) : null;
  });

  /**
   * Attempts newest first — the audit trail, read in the order it is asked
   * about.
   *
   * Nothing to show while the list has not arrived. The section this feeds is
   * hidden when it is empty either way; what an unread list must not do is
   * appear as a complete history with no attempts in it, and
   * {@link statusFailure} is what says so instead.
   */
  protected readonly attempts = computed(() =>
    [...(this.publications() ?? [])].sort((a, b) => (a.createdAt < b.createdAt ? 1 : -1)),
  );

  protected readonly rollup = computed<PublicationRollup | null>(() => {
    const snapshots = this.metrics();
    if (!snapshots?.length) return null;
    return rollUp(snapshots)[0] ?? null;
  });

  /** The retention curve as an SVG polyline, when the platform has released one. */
  protected readonly curve = computed(() => {
    const rollup = this.rollup();
    if (!rollup) return null;
    return toCurves([rollup], CURVE_WIDTH, CURVE_HEIGHT)[0] ?? null;
  });

  /** The most recent day the platform reported, for "as of". */
  protected readonly measuredTo = computed(() => {
    const snapshots = this.metrics();
    return snapshots?.length ? snapshots[snapshots.length - 1]!.date : null;
  });

  /**
   * The channels, once they are here.
   *
   * The one place "not delivered yet" is allowed to read as "none to offer",
   * because the picker has nothing to draw either way. The difference between
   * the two is carried by {@link channelsFailure}, which says why rather than
   * leaving a workspace with channels looking like one without.
   */
  protected readonly channelOptions = computed(() => this.channels() ?? []);

  /** A channel's name, or its id when it is one this workspace no longer has. */
  protected channelLabel(channelId: string): string {
    return this.channelOptions().find((channel) => channel.id === channelId)?.label ?? channelId;
  }

  /**
   * Where an attempt went, listing only what was actually recorded.
   *
   * A publication is the record of what went out, so a field the worker left
   * null has to read as absent rather than as the channel's current default.
   * Substituting one was how a failed attempt that never reached YouTube came
   * to display a category and a privacy it had never been given — an audit
   * trail with gaps is honest, one that fills them is not.
   */
  protected destination(publication: Publication | null): string {
    if (!publication) return '';
    const parts: string[] = [];
    if (publication.privacy) parts.push(publication.privacy);
    if (publication.channelId) parts.push(this.channelLabel(publication.channelId));
    return parts.join(' · ');
  }

  /** The same, prefixed with the platform, for a row in the history. */
  protected recorded(publication: Publication): string {
    const parts: string[] = [publication.platform];
    if (publication.channelId) parts.push(this.channelLabel(publication.channelId));
    if (publication.privacy) parts.push(publication.privacy);
    if (publication.categoryId) parts.push(categoryLabel(publication.categoryId));
    return parts.join(' · ');
  }

  // ── What this upload will say ──────────────────────────────────────────────
  //
  // Every accessor answers "what will actually go out", falling through the
  // same layers the worker's resolver uses
  // (apps/worker/clipforge/publish/metadata.py): this publish, then the
  // channel, then the floor. They therefore *show* the outcome rather than
  // sitting empty and leaving the operator to guess.
  //
  // Title and description are the exception: shown empty with the default as
  // placeholder text, because the channel's `titleSuffix` means the resolved
  // title is not something this side can compute without duplicating the part
  // of the resolver that is actually subtle.

  protected readonly channel = computed<Channel | null>(() => {
    const chosen = this.draftChannel();
    const all = this.channelOptions();
    if (chosen) return all.find((channel) => channel.id === chosen) ?? null;
    if (all.length === 0) return null;
    return all.find((channel) => channel.isDefault) ?? all[0]!;
  });

  protected readonly channelId = computed(() => this.channel()?.id ?? '');
  protected readonly title = computed(() => this.draftTitle() ?? '');
  protected readonly description = computed(() => this.draftDescription() ?? '');
  protected readonly privacy = computed<PublishPrivacy>(
    () => this.draftPrivacy() ?? this.channel()?.defaults.privacy ?? 'unlisted',
  );
  protected readonly category = computed(
    () => this.draftCategory() ?? this.channel()?.defaults.categoryId ?? DEFAULT_CATEGORY_ID,
  );

  /**
   * The tag list as text, prefilled from the channel.
   *
   * Prefilled rather than left blank, and that is what makes "no tags on this
   * one" expressible at all: the operator clears a field that had something in
   * it, which is an unmistakable instruction. A blank field that had always
   * been blank could not be told apart from one nobody opened.
   */
  protected readonly tags = computed(
    () => this.draftTags() ?? (this.channel()?.defaults.tags ?? []).join(', '),
  );

  protected readonly overridden = computed(
    () =>
      this.draftChannel() !== undefined ||
      this.title().trim() !== '' ||
      this.description().trim() !== '' ||
      this.draftPrivacy() !== undefined ||
      this.draftCategory() !== undefined ||
      this.draftTags() !== undefined,
  );

  /**
   * What to send, or null when there is nothing to say.
   *
   * Only touched fields are populated. Sending the resolved value of every
   * field instead would look equivalent and is not: it would pin this upload to
   * today's channel defaults, so a scheduled publish would ignore a correction
   * made to the channel before it ran.
   */
  protected options(): PublishOptions | null {
    if (!this.overridden()) return null;

    const tags = this.draftTags();
    return {
      channelId: this.draftChannel() ?? null,
      title: this.title().trim() || null,
      description: this.description().trim() || null,
      privacy: this.draftPrivacy() ?? null,
      categoryId: this.draftCategory() ?? null,
      // undefined and '' are different answers: never opened, versus opened and
      // emptied. The first falls through to the channel, the second does not.
      tags:
        tags === undefined
          ? null
          : tags
              .split(',')
              .map((tag) => tag.trim())
              .filter(Boolean)
              .slice(0, 20),
    };
  }

  // ── Actions ────────────────────────────────────────────────────────────────

  private async run(what: string, action: () => Promise<void>): Promise<void> {
    this.busy.set(true);
    this.error.set(null);
    this.saved.set(null);
    try {
      await action();
      this.saved.set(what);
    } catch (err) {
      this.error.set(
        err instanceof Error && err.message.includes('permission')
          ? 'The publish gate refused this. Check the clip is still approved.'
          : err instanceof Error
            ? err.message
            : String(err),
      );
    } finally {
      this.busy.set(false);
    }
  }

  protected async publish(): Promise<void> {
    const clip = this.clip();
    const uid = this.session.uid;
    if (!clip || !uid) return;
    // Checked here too, not only by the disabled attribute. A disabled button
    // is a hint to a person, not a constraint on a program.
    if (this.refusal()) return;

    const when = this.draftSchedule() ? new Date(this.draftSchedule()) : null;
    await this.run(when ? 'Scheduled' : 'Queued', () =>
      this.publishing.requestPublish(uid, clip.id, when, this.options()).then(() => undefined),
    );
  }

  /**
   * Call off a publish that has not started.
   *
   * The reason a scheduled upload is worth showing at all. A job queued for
   * Friday is claimable by any worker that comes up after Friday, so "I changed
   * my mind" has to be expressible before then — and cancelling the job is the
   * only place it can be said, because there is no publication yet to withdraw.
   */
  protected async cancelScheduled(): Promise<void> {
    const job = this.state()?.job;
    if (!job || job.status !== 'QUEUED') return;
    await this.run('Publish called off', () => this.queue.cancel(job.id));
  }

  /**
   * Ask the worker to put this clip in the bucket.
   *
   * For the case this page exists to serve: checking a publish from a phone,
   * finding nothing to play, and being nowhere near the machine that has it.
   */
  protected async askForUpload(): Promise<void> {
    const clip = this.clip();
    const uid = this.session.uid;
    if (!clip || !uid) return;

    await this.run('Upload queued — it will start playing here when it lands', async () => {
      await this.publishing.requestUpload(uid, clip.id);
      this.uploadRequested.set(true);
    });
  }
}
