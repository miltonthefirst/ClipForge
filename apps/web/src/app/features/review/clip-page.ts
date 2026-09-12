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
  CropAnchor,
  FitFill,
  FramingMode,
  MusicCaptions,
  MusicMode,
  MusicOptions,
  PanKeyframe,
  Publication,
  PublishOptions,
  PublishPrivacy,
  RemakeOptions,
  RightsBasis,
  SpeechMode,
  SubScores,
  VoiceCaptions,
} from '@clipforge/contracts';

import { PlaybackService, type PlaybackSource } from '../../core/playback';
import { RIGHTS_BASES, draftIsComplete } from '../../core/rights';
import { CATEGORIES, PRIVACY_OPTIONS, categoryLabel } from '../../core/youtube';
import { SessionService } from '../../core/session';
import { ClipForgeStore } from '../../core/store';

/**
 * The framing choices, in the order a reviewer should consider them.
 *
 * "Leave it alone" first because most remakes are about the voice or the
 * timing. Then the three real answers, each with the trade it makes, because
 * there is no best one — a wide pitch view wants FIT and a penalty wants TRACK,
 * and a chooser that did not say so would just be four words.
 */
const FRAMING_CHOICES: readonly {
  value: FramingMode | 'UNCHANGED';
  label: string;
  hint: string;
}[] = [
  { value: 'UNCHANGED', label: 'Leave the framing', hint: 'Keep the window exactly as it is' },
  {
    value: 'FIT',
    label: 'Fit the whole frame',
    hint: 'Nothing is cropped, so nothing can be lost. A smaller picture',
  },
  {
    value: 'TRACK',
    label: 'Follow the action',
    hint: 'The window moves to wherever the motion is. Full-size picture',
  },
  {
    value: 'AS_RENDERED',
    label: 'Fix it to one side',
    hint: 'A still window, left, centre or right. For a subject that stays put',
  },
  {
    value: 'PAN',
    label: 'Move it by hand',
    hint: 'You set where the window sits, at points you choose',
  },
];

/**
 * The languages the worker can actually speak, with their default voices.
 *
 * Mirrored from `clipforge.media.speech`, and deliberately a short list rather
 * than a free-text field: offering a language with no voice behind it produces
 * a job that fails after the render, which is the worst possible moment to find
 * out.
 */
const REMAKE_LANGUAGES: readonly { tag: string; label: string; voice: string }[] = [
  { tag: 'en-us', label: 'English (US)', voice: 'af_heart' },
  { tag: 'en-gb', label: 'English (UK)', voice: 'bf_emma' },
  { tag: 'es', label: 'Spanish', voice: 'ef_dora' },
  { tag: 'fr-fr', label: 'French', voice: 'ff_siwis' },
  { tag: 'it', label: 'Italian', voice: 'if_sara' },
  { tag: 'pt-br', label: 'Portuguese (Brazil)', voice: 'pf_dora' },
  { tag: 'hi', label: 'Hindi', voice: 'hf_alpha' },
  { tag: 'ja', label: 'Japanese', voice: 'jf_alpha' },
  { tag: 'ko', label: 'Korean', voice: 'kf_yuna' },
  { tag: 'zh', label: 'Mandarin', voice: 'zf_xiaobei' },
];

function defaultVoiceFor(tag: string): string {
  return REMAKE_LANGUAGES.find((l) => l.tag === tag)?.voice ?? 'af_heart';
}

/**
 * What the contract and `remakeOptionsOk` in firebase/firestore.rules allow.
 * Mirrored rather than imported because the rules are not a TypeScript module;
 * a mismatch here is a job create denied with an unhelpful message.
 */
const TRIM_LIMIT_SEC = 30;
const NOTES_MAX = 2000;
const SCRIPT_MAX = 4000;

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
  /**
   * An UPLOAD job has been asked for and the clip is still unplayable.
   *
   * Cleared by the clip document itself rather than by a timer: the page is
   * already watching it, so the moment the worker stamps a storage path the
   * player appears and this has nothing left to say.
   */
  protected readonly uploadRequested = signal(false);

  /**
   * Whether this clip would be unplayable on a device that is not this one.
   *
   * Deliberately not "can I play it here". Playing locally is the case where
   * offering the upload matters most: you are at the desk, it plays fine, and
   * the phone you will actually review on cannot reach the worker at all. Tying
   * the offer to what *this* screen can show hid it from exactly the person
   * standing next to the machine that could fix it.
   */
  protected readonly needsCloudCopy = computed(() => {
    const clip = this.clip();
    if (!clip) return false;
    // A `playbackUrl` is a URL any browser can fetch, wherever it came from.
    if (this.source().kind === 'remote') return false;
    return !this.playback.bucketCopyLive(clip);
  });

  /** Whether there is something a `<video>` can actually be pointed at. */
  protected readonly playable = computed(() => {
    const kind = this.source().kind;
    return kind === 'remote' || kind === 'local' || kind === 'bucket';
  });

  /**
   * Why the cloud copy could not be fetched, when that is what happened.
   *
   * Distinct from every other "cannot play" state on this page, and shown
   * instead of the upload offer rather than beside it: the clip is already in
   * the bucket, so another UPLOAD job would be claimed, skipped and completed
   * without changing anything a reviewer can see. Offering it was how this
   * failure disguised itself as a worker that never uploaded.
   */
  protected readonly blockedReason = computed(() => {
    const source = this.source();
    return source.kind === 'blocked' ? source.reason : null;
  });
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

  // ── Remake ─────────────────────────────────────────────────────────────────
  //
  // The correction channel. Everything here is optional, because a remake is
  // almost always one complaint — "it loses the ball", "put it in Spanish" —
  // and a form demanding a complete specification for a single correction is
  // the interface failing to do its job.
  protected readonly showRemake = signal(false);
  protected readonly remakeNotes = signal('');
  protected readonly remakeFraming = signal<FramingMode | 'UNCHANGED'>('UNCHANGED');
  protected readonly remakeCrop = signal<CropAnchor | null>(null);
  protected readonly remakeFill = signal<FitFill>('BLUR');
  protected readonly remakeZoom = signal(1);
  protected readonly remakeStartDelta = signal(0);
  protected readonly remakeEndDelta = signal(0);

  /**
   * Clamp a typed nudge to what the rules will accept.
   *
   * `min`/`max` on a number input only mark the control invalid; the value
   * still flows through `ngModelChange`. Sending it anyway gets the whole job
   * create denied, and the only thing the reviewer sees is Firebase's own
   * "Missing or insufficient permissions" — which names no field and reads as
   * though they may not remake a clip they can plainly remake.
   */
  protected setTrim(which: 'start' | 'end', raw: string | number): void {
    const value = Math.max(-TRIM_LIMIT_SEC, Math.min(TRIM_LIMIT_SEC, Number(raw) || 0));
    (which === 'start' ? this.remakeStartDelta : this.remakeEndDelta).set(value);
  }
  protected readonly remakeKeyframes = signal<PanKeyframe[]>([]);

  protected readonly remakeVoiceOn = signal(false);
  protected readonly remakeLanguage = signal('es');
  protected readonly remakeVoiceName = signal('');
  protected readonly remakeSpeechMode = signal<SpeechMode>('REPLACE');
  protected readonly remakeVoiceCaptions = signal<VoiceCaptions>('REBUILD');
  protected readonly remakeScript = signal('');

  protected readonly notesMax = NOTES_MAX;
  protected readonly scriptMax = SCRIPT_MAX;
  protected readonly trimLimit = TRIM_LIMIT_SEC;
  protected readonly languages = REMAKE_LANGUAGES;
  protected readonly framingChoices = FRAMING_CHOICES;

  /**
   * Every mode except AS_RENDERED-without-an-anchor re-cuts from the source,
   * which the workspace collector reclaims. Worth saying *before* someone
   * chooses it rather than after the job fails — the same courtesy the music
   * panel pays for removing captions.
   */
  protected readonly framingNeedsSource = computed(() => this.remakeFraming() !== 'UNCHANGED');

  /** PAN is the one mode that cannot work without the reviewer setting points. */
  protected readonly panNeedsPoints = computed(
    () => this.remakeFraming() === 'PAN' && this.remakeKeyframes().length === 0,
  );

  /**
   * A remake with nothing in it would re-cut the clip identically, which is a
   * render nobody wanted. A note alone is enough, because the worker can read
   * it.
   */
  protected readonly canRemake = computed(
    () =>
      !this.panNeedsPoints() &&
      (this.remakeNotes().trim().length > 0 ||
        this.remakeFraming() !== 'UNCHANGED' ||
        this.remakeVoiceOn() ||
        this.remakeStartDelta() !== 0 ||
        this.remakeEndDelta() !== 0),
  );

  /** Where the playhead is, so a pan point can be set against what is on screen. */
  protected readonly playhead = signal(0);

  constructor() {
    void this.playback.probeLocalServer().then((ok) => this.localAvailable.set(ok));

    effect((onCleanup) => {
      const id = this.id();
      this.resetForClip();
      const stop = this.store.watchClip(
        id,
        (clip) => {
          this.clip.set(clip);
          if (clip?.storagePath) this.uploadRequested.set(false);
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
    if (this.retryPlayback) clearInterval(this.retryPlayback);
  }

  /**
   * Keep asking, but only while there is nothing to play.
   *
   * Two things can make an unplayable clip playable without its document
   * changing in any way this page would notice: the worker being started, which
   * brings its file server up, and an UPLOAD job landing. Without this the
   * poster stays a poster until the whole app is reloaded, and the person who
   * just pressed Start has no way to know it worked.
   *
   * Stops itself the moment something plays, so the common case costs nothing.
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

  /**
   * Load the things that hang off a clip, once.
   *
   * Guarded on each so a live clip update — a review decision landing, say —
   * does not re-fetch a poster that cannot have changed.
   */
  private async hydrate(clip: Clip): Promise<void> {
    this.source.set(await this.playback.resolve(clip, this.localAvailable()));
    if (this.source().kind === 'poster') this.watchForPlayback();

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

  /**
   * Forget everything that belonged to the clip we were just looking at.
   *
   * `review/:id` is one route config, so the router REUSES this component when
   * only the id changes — `withComponentInputBinding` updates the input signal
   * and nothing else. `hydrate()` then declines to refetch what is already
   * there (`if (!this.preview())`, `if (!this.candidate())`), so without this
   * the next clip renders with the previous one's poster, filmstrip, score
   * breakdown and publish history.
   *
   * The drafts are the part that actually loses work: `title()` falls back to
   * `draftTitle() ?? clip.title`, so an unsaved edit typed against one clip
   * would follow you to the next one and be written to it on Save.
   *
   * The clip→clip link this guards has existed since music remixes, but the
   * remake panel is what makes "← the version this was made from" an everyday
   * step rather than a rarity.
   */
  private resetForClip(): void {
    this.clip.set(undefined);
    this.preview.set(null);
    this.candidate.set(null);
    this.publications.set([]);
    this.source.set({ kind: 'poster' });
    this.uploadRequested.set(false);
    this.error.set(null);
    this.saved.set(null);

    this.draftTitle.set(null);
    this.draftDescription.set(null);
    this.draftNote.set(null);
    this.draftTags.set(null);
    this.draftChannel.set(null);
    this.draftPrivacy.set(null);
    this.draftCategory.set(null);
    this.draftSchedule.set('');
    this.draftBasis.set(null);
    this.draftRightsNote.set('');
    this.showPublishOptions.set(false);

    this.showMusic.set(false);
    this.musicSource.set('');
    this.musicBasis.set(null);
    this.musicNote.set('');

    this.showRemake.set(false);
    this.remakeNotes.set('');
    this.remakeFraming.set('UNCHANGED');
    this.remakeCrop.set(null);
    this.remakeKeyframes.set([]);
    this.remakeVoiceOn.set(false);
    this.remakeScript.set('');
    this.remakeStartDelta.set(0);
    this.remakeEndDelta.set(0);
    this.playhead.set(0);
  }

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
   * Ask the worker to put this clip in the bucket.
   *
   * For the case this page exists to serve: reviewing on a phone, finding a
   * clip with nothing to play, and being nowhere near the machine that has it.
   * The worker is unreachable from here — its file server answers on
   * 127.0.0.1 — so the request goes through the queue, and the clip document
   * this page is already watching is what reports the result.
   *
   * Nothing has to be polled and nothing has to be dismissed: when the storage
   * path arrives, the player replaces the poster on its own.
   */
  protected async askForUpload(): Promise<void> {
    const clip = this.clip();
    const uid = this.session.uid;
    if (!clip || !uid) return;

    await this.run('Upload queued — it will start playing here when it lands', async () => {
      await this.store.requestUpload(uid, clip.id);
      this.uploadRequested.set(true);
    });
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

  // ── Remake ─────────────────────────────────────────────────────────────────

  /** Note where the playhead is, so "pan to here" can mean something. */
  protected trackPlayhead(event: Event): void {
    const video = event.target as HTMLVideoElement | null;
    if (video) this.playhead.set(video.currentTime);
  }

  /**
   * Add a pan point at the playhead.
   *
   * `xPct` is the centre of the window as a percentage of the *source* width,
   * which is a thing the reviewer can only estimate — they are watching an
   * already-cropped clip. That is the honest limitation of setting a pan by
   * hand on a phone, and it is why TRACK exists: for footage where the subject
   * moves, having the worker find it beats describing where it went.
   */
  protected addPanPoint(xPct: number): void {
    const atSec = Math.round(this.playhead() * 10) / 10;
    const without = this.remakeKeyframes().filter((k) => Math.abs(k.atSec - atSec) > 0.05);
    this.remakeKeyframes.set(
      [...without, { atSec, xPct }].sort((a, b) => a.atSec - b.atSec).slice(0, 60),
    );
  }

  protected removePanPoint(atSec: number): void {
    this.remakeKeyframes.set(this.remakeKeyframes().filter((k) => k.atSec !== atSec));
  }

  /**
   * Seed the pan points from whatever the last remake actually did.
   *
   * The reason `AppliedRemake.keyframes` is stored. A tracked clip that
   * followed the wrong thing is corrected by editing the tracker's answer,
   * which is far less work than describing the whole path from nothing.
   */
  protected seedFromLastRemake(): void {
    const applied = this.clip()?.remake;
    if (!applied?.keyframes?.length) return;
    this.remakeFraming.set('PAN');
    this.remakeKeyframes.set(applied.keyframes.slice(0, 60));
  }

  protected async remake(): Promise<void> {
    const clip = this.clip();
    const uid = this.session.uid;
    if (!clip || !uid || !this.canRemake()) return;

    const mode = this.remakeFraming();
    const options: RemakeOptions = {
      notes: this.remakeNotes().trim() || null,
      // The worker reads the note to fill in what was left unset, and records
      // what it made of it on the clip. Always on: a note that is ignored is a
      // note the reviewer wasted.
      interpretNotes: true,
      framing:
        mode === 'UNCHANGED'
          ? null
          : {
              mode,
              crop: mode === 'AS_RENDERED' ? this.remakeCrop() : null,
              keyframes: mode === 'PAN' ? this.remakeKeyframes() : [],
              fill: mode === 'FIT' ? this.remakeFill() : null,
              zoom: this.remakeZoom(),
              offsetYPct: 0,
              smoothingSec: 2,
              maxPanPctPerSec: 12,
            },
      voice: this.remakeVoiceOn()
        ? {
            mode: this.remakeSpeechMode(),
            // Empty means "the default voice for that language", which the
            // worker resolves. Sending a name the synthesiser does not have
            // would fail after the render rather than before it.
            voice: this.remakeVoiceName().trim() || defaultVoiceFor(this.remakeLanguage()),
            language: this.remakeLanguage(),
            translate: true,
            script: this.remakeScript().trim() || null,
            speed: 1,
            gainDb: null,
            duckDb: null,
            captions: this.remakeVoiceCaptions(),
          }
        : null,
      startDeltaSec: this.remakeStartDelta(),
      endDeltaSec: this.remakeEndDelta(),
      profile: null,
    };

    await this.run('Remake queued — the new version will appear in the review queue', async () => {
      await this.store.requestRemake(uid, clip.id, options);
      this.showRemake.set(false);
      this.remakeNotes.set('');
    });
  }
}
