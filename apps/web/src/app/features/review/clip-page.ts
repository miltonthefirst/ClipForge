import { DecimalPipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  computed,
  effect,
  inject,
  input,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import type {
  Candidate,
  Channel,
  Clip,
  ClipPreview,
  MusicCaptions,
  MusicMode,
  MusicOptions,
  Publication,
  PublishOptions,
  PublishPrivacy,
  RightsBasis,
  SubScores,
} from '@clipforge/contracts';

import { PlaybackService, type PlaybackSource } from '../../core/playback';
import { RIGHTS_BASES, draftIsComplete } from '../../core/rights';
import { CATEGORIES, PRIVACY_OPTIONS, categoryLabel } from '../../core/youtube';
import { SessionService } from '../../core/session';
import { ClipForgeStore } from '../../core/store';

/** The rubric's ceilings, mirrored from the contract's own bounds. */
const SUB_SCORE_MAXIMA: readonly (readonly [keyof SubScores, string, number])[] = [
  ['hook', 'Hook', 25],
  ['curiosity', 'Curiosity', 20],
  ['standalone', 'Standalone', 20],
  ['emotion', 'Emotion', 15],
  ['pacing', 'Pacing', 10],
  ['shareability', 'Shareability', 10],
];

/**
 * One clip, and everything you might want to do to it.
 *
 * The queue answers "which of these is worth my time"; this answers everything
 * after that. It exists because the queue was trying to be both — fifty cards
 * each carrying a video, a filmstrip, six score meters and a transcript is not
 * a list anyone can move through, and the information is not wrong, just in the
 * wrong place.
 *
 * Deciding, describing and publishing are on one screen on purpose. They used
 * to be two: approve here, then find the clip again on the Publish page and
 * configure it there. That is two screens for what is one thought — "this is
 * good, and this is how it should go out."
 */
@Component({
  selector: 'app-clip-page',
  imports: [DecimalPipe, FormsModule, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './clip-page.html',
})
export class ClipPage implements OnDestroy {
  private readonly store = inject(ClipForgeStore);
  private readonly session = inject(SessionService);
  private readonly playback = inject(PlaybackService);

  /** From the route: `review/:id`. */
  readonly id = input.required<string>();

  private stopClip: (() => void) | null = null;
  private stopChannels: (() => void) | null = null;

  protected readonly bases = RIGHTS_BASES;
  protected readonly categories = CATEGORIES;
  protected readonly privacies = PRIVACY_OPTIONS;
  protected readonly categoryLabel = categoryLabel;

  protected readonly clip = signal<Clip | null | undefined>(undefined);
  protected readonly preview = signal<ClipPreview | null>(null);
  protected readonly candidate = signal<Candidate | null>(null);
  protected readonly publications = signal<Publication[]>([]);
  protected readonly channels = signal<Channel[]>([]);
  protected readonly source = signal<PlaybackSource>({ kind: 'poster' });
  protected readonly localAvailable = signal(false);

  protected readonly error = signal<string | null>(null);
  protected readonly busy = signal(false);
  protected readonly saved = signal<string | null>(null);

  // ── Drafts ─────────────────────────────────────────────────────────────────
  //
  // Plain signals, not the `Record<string, …>` the queue-shaped Publish page
  // needs. One clip means one draft, and keying by id here would be ceremony.
  protected readonly draftTitle = signal<string | null>(null);
  protected readonly draftDescription = signal<string | null>(null);
  protected readonly draftNote = signal<string | null>(null);
  protected readonly draftTags = signal<string | null>(null);
  protected readonly draftChannel = signal<string | null>(null);
  protected readonly draftPrivacy = signal<PublishPrivacy | null>(null);
  protected readonly draftCategory = signal<string | null>(null);
  protected readonly draftSchedule = signal('');
  protected readonly draftBasis = signal<RightsBasis | null>(null);
  protected readonly draftRightsNote = signal('');
  protected readonly showPublishOptions = signal(false);

  // ── Music ──────────────────────────────────────────────────────────────────
  protected readonly showMusic = signal(false);
  protected readonly musicSource = signal('');
  protected readonly musicMode = signal<MusicMode>('BED');
  protected readonly musicCaptions = signal<MusicCaptions>('KEEP');
  protected readonly musicBasis = signal<RightsBasis | null>(null);
  protected readonly musicNote = signal('');

  /**
   * REMOVE re-cuts the segment from the original video, because captions are
   * burned into pixels and there is nothing to switch off in a finished file.
   * That needs the source still on the worker — worth saying before someone
   * chooses it, rather than after the job fails.
   */
  protected readonly captionsNeedSource = computed(() => this.musicCaptions() === 'REMOVE');

  protected readonly canAddMusic = computed(
    () =>
      this.musicSource().trim().length > 0 && draftIsComplete(this.musicBasis(), this.musicNote()),
  );

  constructor() {
    void this.playback.probeLocalServer().then((ok) => this.localAvailable.set(ok));

    effect((onCleanup) => {
      const id = this.id();
      const stop = this.store.watchClip(
        id,
        (clip) => {
          this.clip.set(clip);
          if (clip) void this.hydrate(clip);
        },
        (err) => this.error.set(err.message),
      );
      this.stopClip = stop;
      onCleanup(stop);
    });

    effect((onCleanup) => {
      if (!this.session.uid) return;
      const stop = this.store.watchChannels(
        (channels) => this.channels.set(channels),
        () => this.channels.set([]),
      );
      this.stopChannels = stop;
      onCleanup(stop);
    });
  }

  ngOnDestroy(): void {
    this.stopClip?.();
    this.stopChannels?.();
  }

  /**
   * Load the things that hang off a clip, once.
   *
   * Guarded on each so a live clip update — a review decision landing, say —
   * does not re-fetch a poster that cannot have changed.
   */
  private async hydrate(clip: Clip): Promise<void> {
    this.source.set(await this.playback.resolve(clip, this.localAvailable()));

    if (!this.preview()) {
      this.preview.set(await this.store.loadPreview(clip.id).catch(() => null));
    }
    if (!this.candidate()) {
      this.candidate.set(await this.store.loadCandidate(clip.candidateId).catch(() => null));
    }
    if (clip.review === 'APPROVED' && this.publications().length === 0) {
      this.publications.set(await this.store.loadPublications(clip.id).catch(() => []));
    }
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

  protected readonly subScores = computed(() => {
    const scores = this.candidate()?.subScores;
    if (!scores) return [];
    return SUB_SCORE_MAXIMA.map(([key, label, max]) => ({
      label,
      value: scores[key],
      max,
      pct: Math.round((Number(scores[key]) / max) * 100),
    }));
  });

  protected readonly attested = computed(() => !!this.clip()?.rights);
  protected readonly published = computed(
    () => this.publications().find((p) => p.state === 'PUBLISHED') ?? null,
  );

  /** A basis of FAIR_USE_ASSERTED has to say why; the rules require it too. */
  protected readonly needsRightsNote = computed(() => this.draftBasis() === 'FAIR_USE_ASSERTED');

  // `draftIsComplete` is the same rule the Publish page applies, imported
  // rather than restated — two copies of "when may this be attested" is one
  // copy too many for a gate.
  protected readonly canAttest = computed(() =>
    draftIsComplete(this.draftBasis(), this.draftRightsNote()),
  );

  protected readonly title = computed(() => this.draftTitle() ?? this.clip()?.title ?? '');
  protected readonly description = computed(
    () => this.draftDescription() ?? this.clip()?.description ?? '',
  );
  protected readonly note = computed(() => this.draftNote() ?? this.clip()?.reviewNote ?? '');

  protected readonly dirty = computed(() => {
    const clip = this.clip();
    if (!clip) return false;
    return (
      (this.draftTitle() !== null && this.draftTitle() !== (clip.title ?? '')) ||
      (this.draftDescription() !== null && this.draftDescription() !== (clip.description ?? '')) ||
      (this.draftNote() !== null && this.draftNote() !== (clip.reviewNote ?? ''))
    );
  });

  // ── Actions ────────────────────────────────────────────────────────────────

  private async run(what: string, action: () => Promise<void>): Promise<void> {
    this.busy.set(true);
    this.error.set(null);
    this.saved.set(null);
    try {
      await action();
      this.saved.set(what);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.busy.set(false);
    }
  }

  /** Save the copy and the note without deciding anything. */
  protected async save(): Promise<void> {
    const clip = this.clip();
    if (!clip) return;
    await this.run('Saved', () =>
      this.store.editClip(clip.id, {
        title: this.title().trim() || null,
        description: this.description().trim() || null,
        reviewNote: this.note().trim() || null,
      }),
    );
  }

  /**
   * Decide, saving any edits in the same write.
   *
   * One button rather than "save, then approve": an edited title that was never
   * saved because the reviewer went straight to Approve is a silent loss, and
   * the two are one intention anyway.
   */
  protected async decide(review: 'APPROVED' | 'REJECTED'): Promise<void> {
    const clip = this.clip();
    if (!clip) return;
    await this.run(review === 'APPROVED' ? 'Approved' : 'Rejected', () =>
      this.store.review(clip.id, review, clip.storagePath, {
        title: this.title().trim() || null,
        description: this.description().trim() || null,
        reviewNote: this.note().trim() || null,
      }),
    );
  }

  protected async attest(): Promise<void> {
    const clip = this.clip();
    const uid = this.session.uid;
    const basis = this.draftBasis();
    if (!clip || !uid || !basis) return;
    await this.run('Rights recorded', () =>
      this.store.attest(uid, clip.id, basis, this.draftRightsNote().trim() || null),
    );
  }

  protected async publish(): Promise<void> {
    const clip = this.clip();
    const uid = this.session.uid;
    if (!clip || !uid) return;

    const when = this.draftSchedule() ? new Date(this.draftSchedule()) : null;
    await this.run('Queued for publishing', async () => {
      await this.store.requestPublish(uid, clip.id, when, this.publishOptions());
    });
  }

  /**
   * The overrides for this one upload, or null when nothing was overridden.
   *
   * Null and empty are different answers and both are ones an operator can
   * mean: no options at all falls through to the channel's defaults, while an
   * empty tag list means "no tags on this one".
   */
  protected publishOptions(): PublishOptions | null {
    const options: PublishOptions = {};
    if (this.draftChannel()) options.channelId = this.draftChannel();
    if (this.draftPrivacy()) options.privacy = this.draftPrivacy();
    if (this.draftCategory()) options.categoryId = this.draftCategory();

    const tags = this.draftTags();
    if (tags !== null) {
      options.tags = tags
        .split(',')
        .map((tag) => tag.trim())
        .filter(Boolean)
        .slice(0, 20);
    }

    return Object.keys(options).length > 0 ? options : null;
  }

  protected tagsValue(): string {
    return this.draftTags() ?? '';
  }

  /**
   * Queue a MUSIC job for this clip.
   *
   * The result is a *new* clip, not an edit of this one — the version that was
   * reviewed stays exactly as it was, and the scored one arrives in the queue
   * to be watched on its own merits.
   */
  protected async addMusic(): Promise<void> {
    const clip = this.clip();
    const uid = this.session.uid;
    const basis = this.musicBasis();
    if (!clip || !uid || !basis) return;

    const options: MusicOptions = {
      source: this.musicSource().trim(),
      mode: this.musicMode(),
      captions: this.musicCaptions(),
      rights: {
        basis,
        attestedBy: uid,
        attestedAt: new Date().toISOString(),
        note: this.musicNote().trim() || null,
      },
    };

    await this.run(
      'Music queued — the scored version will appear in the review queue',
      async () => {
        await this.store.requestMusic(uid, clip.id, options);
        this.showMusic.set(false);
        this.musicSource.set('');
      },
    );
  }
}
