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
import { Router, RouterLink } from '@angular/router';
import type {
  Candidate,
  Channel,
  Clip,
  ClipPreview,
  CropAnchor,
  FitFill,
  FramingMode,
  Job,
  MusicCaptions,
  MusicMode,
  MusicOptions,
  ObscureOptions,
  ObscureRegion,
  PanKeyframe,
  Publication,
  PublishOptions,
  PublishPrivacy,
  RemakeOptions,
  SpeechMode,
  SubScores,
  VoiceCaptions,
} from '@clipforge/contracts';

import { ClipsRepository } from '../../core/data/clips';
import { JobsRepository } from '../../core/data/jobs';
import { PublishingRepository } from '../../core/data/publishing';
import { SourcesRepository } from '../../core/data/sources';
import type { Live } from '../../core/firestore/gateway';
import { youtubePlaylist } from '../../core/music-source';
import { PlaybackService, type PlaybackSource } from '../../core/playback';
import { publishStateOf, type PublishState } from '../../core/publish-state';
import { remakeMusicOutlook } from '../../core/remake-music';
import { CATEGORIES, PRIVACY_OPTIONS, categoryLabel } from '../../core/youtube';
import { SessionService } from '../../core/session';

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

/**
 * Are two rectangles the same mark, measured twice?
 *
 * Detection is a measurement, not a lookup: the same channel bug found on two
 * different cuts of the same match comes back a few tenths of a percent apart.
 * Comparing for equality would say a standing rule had not been applied when
 * it plainly had, so this compares within a tolerance wide enough to cover the
 * grid detection rounds to and far narrower than the gap between two marks.
 */
function upToSix(regions: readonly ObscureRegion[]): ObscureOptions['regions'] {
  /**
   * `maxItems` in the schema becomes a tuple union in TypeScript, so the field
   * is typed `[] | [R] | [R, R] | ...` and nothing produced by `.map()` is
   * assignable to it however long it is. Capped and cast once here rather than
   * at each call site, and the cap is the one the security rule enforces.
   */
  return regions.slice(0, 6) as ObscureOptions['regions'];
}

function sameRegion(a: ObscureRegion, b: ObscureRegion): boolean {
  const near = (x: number, y: number) => Math.abs(x - y) < 2.5;
  return (
    near(a.xPct, b.xPct) && near(a.yPct, b.yPct) && near(a.wPct, b.wPct) && near(a.hPct, b.hPct)
  );
}

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
  imports: [DatePipe, DecimalPipe, FormsModule, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './clip-page.html',
})
export class ClipPage implements OnDestroy {
  /**
   * Four repositories, because this screen is where four subjects meet: the
   * clip and its candidate, the jobs run against it, the source it was cut
   * from, and the channels and publications it goes out through. Deciding,
   * describing and publishing on one screen is the whole point of the page, and
   * it is what makes the reading list this long.
   */
  private readonly clips = inject(ClipsRepository);
  private readonly jobs = inject(JobsRepository);
  private readonly publishing = inject(PublishingRepository);
  private readonly sources = inject(SourcesRepository);
  private readonly router = inject(Router);
  private readonly session = inject(SessionService);
  private readonly playback = inject(PlaybackService);

  /** From the route: `review/:id`. */
  readonly id = input.required<string>();

  // ── The listeners this page holds ──────────────────────────────────────────
  //
  // Four live queries, each with its own held signal and its own release. They
  // are signals rather than plain fields because the delivering effects below
  // *read* them: a plain field is not tracked, so an effect would run once
  // against no listener and never again, and the page would sit empty for ever
  // with nothing anywhere saying why.
  //
  // Releasing is not unsubscribing. The gate keeps a listener warm for fifteen
  // minutes past its last reader, so Review → Clip → Review, or stepping back
  // to the version this one was made from, re-attaches to what is already in
  // hand and costs nothing.
  private readonly heldClip = signal<Live<Clip> | null>(null);
  private readonly heldChannels = signal<Live<Channel[]> | null>(null);
  private readonly heldPublications = signal<Live<Publication[]> | null>(null);
  private readonly heldPublishJobs = signal<Live<Job[]> | null>(null);

  protected readonly categories = CATEGORIES;
  protected readonly privacies = PRIVACY_OPTIONS;
  protected readonly categoryLabel = categoryLabel;

  /**
   * The clip: `undefined` until the listener has spoken, `null` once it has and
   * there is no such document.
   *
   * Three states rather than two, and the template renders all three. A
   * document listener says `null` twice over — "not here yet" and "there is no
   * clip with that id" — and the page has to be able to tell a deleted clip
   * from one still on its way.
   */
  protected readonly clip = signal<Clip | null | undefined>(undefined);
  protected readonly preview = signal<ClipPreview | null>(null);
  protected readonly candidate = signal<Candidate | null>(null);
  /**
   * This clip's publish attempts, or `null` while that is not yet known.
   *
   * Null rather than an empty array, because the two mean opposite things here
   * and {@link publishState} reads them: no publications and no outstanding
   * jobs is READY, which is what enables the Publish button. A list that had
   * not arrived yet, counted as empty, is how a clip already queued for Friday
   * gets offered — and taken up on — a second time.
   */
  protected readonly publications = signal<Publication[] | null>(null);
  /**
   * Outstanding publish jobs for this clip, or `null` while that is not yet
   * known.
   *
   * Watched because a `Publication` does not exist until the worker picks the
   * job up, so without these a publish that has been *asked for* — scheduled
   * for Friday, or queued behind a worker that is not running — is
   * indistinguishable from one nobody has requested. This screen offered
   * Publish again in exactly that state, which is how the same video goes out
   * twice. The publish queue reads the same two sources through the same
   * function (core/publish-state.ts), so the two screens cannot disagree.
   */
  protected readonly publishJobs = signal<Job[] | null>(null);
  /**
   * Every version of this clip, oldest first, or `null` before it is asked for.
   *
   * The queue shows one row per clip now, so this is where the rest of the
   * lineage lives: what was asked for at each step, what the machine made of
   * it, and what it refused. Loaded on demand rather than with the clip — most
   * clips have never been remade and would pay for a query that returns one
   * row.
   *
   * An empty array is a real answer: a clip written before lineages existed
   * matches no `lineageId` at all. So the not-yet-asked state is null, and the
   * history panel says "Loading…" for that and only that.
   */
  protected readonly lineage = signal<Clip[] | null>(null);
  protected readonly showHistory = signal(false);
  /** The destinations, or `null` while the channel list is still on its way. */
  protected readonly channels = signal<Channel[] | null>(null);
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

  /**
   * Why the clip is not here, when it is never going to arrive.
   *
   * A fourth state beside loading, missing and loaded, because the page could
   * previously only express three. A failed listener delivers nothing at all,
   * so `clip` stayed `undefined` and the screen went on saying "Loading…" for
   * as long as anyone left it open — and the error it did set went into a
   * banner that only renders *inside* the loaded-clip block, where nobody in
   * that state could ever see it.
   *
   * Separate from {@link error}, which is the banner for something the reviewer
   * just pressed. This one replaces the clip, because it is about the clip.
   */
  protected readonly loadFailure = signal<string | null>(null);

  /**
   * Why the publish state is unknown, when it is going to stay unknown.
   *
   * Without it the Publish panel would go on saying "Checking what has already
   * happened to this clip…" for ever behind a query that had already failed —
   * the same shape of lie the queue used to tell, one panel down. It
   * deliberately does not fall through to READY: an unknown publish state must
   * never turn into an offer to publish.
   */
  protected readonly deliveryFailure = signal<string | null>(null);

  /**
   * Why the destination list is empty, when it is not going to fill.
   *
   * The picker only appears when there is more than one channel, so a failed
   * channel query looks exactly like a workspace that has one — and the upload
   * silently goes to the default. Said out loud where the picker would be.
   */
  protected readonly channelsFailure = signal<string | null>(null);

  /** Why the version history is not here. See {@link openHistory}. */
  protected readonly lineageFailure = signal<string | null>(null);

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
  protected readonly showPublishOptions = signal(false);

  // ── Music ──────────────────────────────────────────────────────────────────
  protected readonly showMusic = signal(false);
  protected readonly musicSource = signal('');
  protected readonly musicMode = signal<MusicMode>('BED');
  protected readonly musicCaptions = signal<MusicCaptions>('KEEP');

  /**
   * REMOVE re-cuts the segment from the original video, because captions are
   * burned into pixels and there is nothing to switch off in a finished file.
   * That needs the source still on the worker — worth saying before someone
   * chooses it, rather than after the job fails.
   */
  protected readonly captionsNeedSource = computed(() => this.musicCaptions() === 'REMOVE');

  /**
   * The playlist in what they typed, when there is one.
   *
   * Not corrected on their behalf. A `list=` can mean "play me this mix" or
   * "here is the track I meant, with some rubbish on the end", and silently
   * picking the second is the same guess that let a MUSIC job fetch 190 tracks
   * before anyone noticed. The field keeps what they pasted; the message below
   * it says what to paste instead.
   */
  protected readonly musicPlaylist = computed(() => youtubePlaylist(this.musicSource()));

  /**
   * The worker refuses a playlist link too, and that guard is the authoritative
   * one. This is the same answer without the round trip: a job created here
   * would be queued, and a QUEUED job on this project means going to find out
   * whether a worker is running at all before it can even fail.
   */
  protected readonly canAddMusic = computed(
    () => this.musicSource().trim().length > 0 && !this.musicPlaylist(),
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

  /**
   * Hide what is burnt into the picture: a channel bug, a score bar, a
   * watermark.
   *
   * A single switch, and deliberately not a drawing tool. The regions are
   * percentages of the SOURCE frame and this app never sees a source frame —
   * media does not leave the worker (decision D3), and the poster it does see is
   * the finished 9:16 clip, which has already been cropped and scaled out of
   * those coordinates. So finding them is the worker's job, and what this page
   * offers instead is the record of what was found and one button to keep it.
   */
  protected readonly remakeHideMarks = signal(false);

  /**
   * Carry forward exactly what the last remake hid, rather than looking again.
   *
   * Worth its own control because detection is a measurement: run on a
   * different cut of the same match it can land a few tenths of a percent
   * apart, and a reviewer who was happy with the last one should be able to
   * have that one rather than a new opinion.
   */
  protected readonly remakeKeepMarks = signal(false);

  protected readonly remakeVoiceOn = signal(false);
  protected readonly remakeLanguage = signal('es');
  protected readonly remakeVoiceName = signal('');
  protected readonly remakeSpeechMode = signal<SpeechMode>('REPLACE');
  protected readonly remakeVoiceCaptions = signal<VoiceCaptions>('REBUILD');
  protected readonly remakeScript = signal('');

  /**
   * Whether the new version gets this clip's track. An opt-OUT, defaulting to
   * carrying it.
   *
   * Reset to true for every clip, deliberately: dropping the soundtrack is a
   * thing you decide about one remake, and a control that remembered the last
   * answer would silently strip the music from the next clip.
   */
  protected readonly remakeKeepMusic = signal(true);

  /**
   * Take the burned-in captions off, without touching the voice.
   *
   * Separate from `remakeVoiceCaptions` because that one only applies when a
   * new narration is being synthesised. A reviewer who writes "Remove caption"
   * is not asking for a different voice, and until this existed the request had
   * nowhere to go: the note was read correctly and the clip came back with its
   * captions and a summary about the language.
   */
  protected readonly remakeRemoveCaptions = signal(false);

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
        this.remakeHideMarks() ||
        this.remakeKeepMarks() ||
        this.remakeStartDelta() !== 0 ||
        this.remakeEndDelta() !== 0),
  );

  /**
   * What this remake will do to the clip's music, or null when it has none.
   *
   * Computed rather than written into the template because the answer depends
   * on the rest of what has been asked for — a trim that changes the length
   * moves the excerpt, and a track that replaces the audio has to give way to a
   * narration this remake adds. The note goes in too, not to be read but to be
   * counted: the worker interprets it into these same options after this screen
   * is gone, so its presence is what turns the definite sentences into
   * conditional ones. See core/remake-music.ts.
   */
  protected readonly remakeMusic = computed(() =>
    remakeMusicOutlook(this.clip()?.music, {
      keepMusic: this.remakeKeepMusic(),
      startDeltaSec: this.remakeStartDelta(),
      endDeltaSec: this.remakeEndDelta(),
      addsNarration: this.remakeVoiceOn(),
      notes: this.remakeNotes(),
    }),
  );

  /** Where the playhead is, so a pan point can be set against what is on screen. */
  protected readonly playhead = signal(0);

  /**
   * Subscribing and delivering are separate effects throughout, and that is not
   * a style choice. An effect that both opened a listener and read what it
   * delivered would make every snapshot a reason to re-subscribe: a heartbeat
   * on a publish job would close and re-open three listeners, and the page
   * would spend its life tearing itself down.
   *
   * The subscribing halves read {@link heldClip} and friends through
   * `untracked` when releasing, because they both read and write those signals.
   * Tracked, each would depend on the signal it is about to write and re-run
   * itself for ever — releasing and re-opening a listener on every pass, which
   * is a hang rather than a leak.
   */
  constructor() {
    void this.playback.probeLocalServer().then((ok) => this.localAvailable.set(ok));

    // ── The clip ─────────────────────────────────────────────────────────────
    effect(() => {
      const id = this.id();
      // Forgets the previous clip's everything, `loadFailure` included: a
      // failure belongs to the listener that produced it, and this is about to
      // replace that listener.
      this.resetForClip();
      untracked(() => this.heldClip())?.release();
      this.heldClip.set(this.clips.watchClip(id));
    });

    effect(() => {
      const held = this.heldClip();
      if (!held) return;
      const failure = held.error();
      if (failure) {
        this.loadFailure.set(failure.message);
        return;
      }
      const clip = held.data();
      // A document listener's `null` means two things, and `loading()` is the
      // only thing that separates them: null while loading is "not here yet",
      // null once loading is over is "there is no such clip". Only the second
      // is news, and treating the first as news is what puts "No such clip" in
      // front of somebody whose clip is on its way.
      if (clip === null && held.loading()) return;
      // Untracked because what follows is side effects, not dependencies:
      // `hydrate` reads `localAvailable` and the drafts, and a delivering
      // effect that took a dependency on those would re-run — and re-fetch the
      // poster — every time the playback probe answered.
      untracked(() => this.onClip(clip));
    });

    // ── The destinations ─────────────────────────────────────────────────────
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
      const held = this.heldChannels();
      if (!held) return;
      const failure = held.error();
      if (failure) {
        this.channelsFailure.set(failure.message);
        return;
      }
      const channels = held.data();
      if (channels === null) return;
      this.channels.set(channels);
    });

    // ── What is happening to it on the way out ───────────────────────────────
    //
    // Watched only once the clip is approved, and gated on a *computed* rather
    // than on `clip()` itself: a clip document changes whenever a review note is
    // saved, and keying this on the whole document would release both listeners
    // and take them again on every save. `approved()` flips once, which is the
    // only time this needs to change.
    //
    // Publications are watched rather than loaded once for the reason the
    // publish page watches them: the worker writes PENDING before it calls
    // YouTube and stamps PUBLISHED after, so a page that read once sits on
    // "uploading" until somebody reloads it.
    effect(() => {
      const id = this.id();
      const approved = this.approved();
      this.deliveryFailure.set(null);
      untracked(() => {
        this.heldPublications()?.release();
        this.heldPublishJobs()?.release();
      });
      this.heldPublications.set(null);
      this.heldPublishJobs.set(null);

      if (!approved) {
        // Empty rather than null, and that is the one place on this page where
        // empty is the honest answer to a question nobody asked: a clip that has
        // not been approved cannot have been published or queued, so this is a
        // result and not the absence of one.
        this.publications.set([]);
        this.publishJobs.set([]);
        return;
      }

      this.publications.set(null);
      this.publishJobs.set(null);
      this.heldPublications.set(this.publishing.watchPublications(id));
      this.heldPublishJobs.set(this.jobs.watchPublishJobs());
    });

    effect(() => {
      const held = this.heldPublications();
      if (!held) return;
      const failure = held.error();
      if (failure) {
        this.deliveryFailure.set(failure.message);
        return;
      }
      const publications = held.data();
      if (publications === null) return;
      this.publications.set(publications);
    });

    effect(() => {
      const held = this.heldPublishJobs();
      if (!held) return;
      const failure = held.error();
      if (failure) {
        this.deliveryFailure.set(failure.message);
        return;
      }
      const jobs = held.data();
      if (jobs === null) return;
      // One listener over every publish job, narrowed here. `watchPublishJobs`
      // asks for the type and nothing else so that every screen showing publish
      // state shares the one listener; a per-clip query would need a composite
      // index and would be a listener each.
      this.publishJobs.set(jobs.filter((job) => job.clipId === this.id()));
    });
  }

  ngOnDestroy(): void {
    this.heldClip()?.release();
    this.heldChannels()?.release();
    this.heldPublications()?.release();
    this.heldPublishJobs()?.release();
    if (this.retryPlayback) clearInterval(this.retryPlayback);
  }

  /**
   * A clip arrived, or the listener said there is none.
   *
   * A method rather than three lines in the effect, because it fans out into
   * two fetches that read signals of their own and so has to be called
   * untracked. See {@link hydrate} for what it declines to re-fetch and why.
   */
  private onClip(clip: Clip | null): void {
    this.clip.set(clip);
    if (clip?.storagePath) this.uploadRequested.set(false);
    if (clip) void this.hydrate(clip);
    void this.loadStandingMarks(clip?.sourceId);
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
      this.preview.set(await this.clips.loadPreview(clip.id).catch(() => null));
    }
    if (!this.candidate()) {
      this.candidate.set(await this.clips.loadCandidate(clip.candidateId).catch(() => null));
    }
    // Publications are not fetched here. They have a listener of their own,
    // because the interesting moments — PENDING becoming PUBLISHED — happen
    // while this page is open and would otherwise need a reload to be seen.
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

  /** Whether this clip has been said yes to, and so may be published at all. */
  protected readonly approved = computed(() => this.clip()?.review === 'APPROVED');

  /**
   * Where this clip has got to on its way to a platform, or `null` while that
   * is still unknown.
   *
   * Null until both halves have arrived, because half an answer here is a wrong
   * answer with a button attached: `publishStateOf([], [])` is READY, and READY
   * is what offers Publish. Counting a list that has not loaded yet as empty
   * would invite a second upload of a clip already queued for Friday, which is
   * the exact mistake the two listeners exist to prevent.
   *
   * `new Date()` is read here rather than held in a ticking signal: the only
   * thing it decides is whether a scheduled publish is still in the future, and
   * this recomputes whenever a publication or a job changes — every occasion on
   * which that answer can change what to offer.
   */
  protected readonly publishState = computed<PublishState | null>(() => {
    const publications = this.publications();
    const jobs = this.publishJobs();
    if (publications === null || jobs === null) return null;
    return publishStateOf(publications, jobs, new Date());
  });

  protected readonly published = computed(
    () => this.publications()?.find((p) => p.state === 'PUBLISHED') ?? null,
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
   *
   * Everything that can be unknown goes back to `null` rather than to empty.
   * Empty is a claim — there are none — and making it about the clip we have
   * not started loading yet is how the previous clip's answers get attributed
   * to the next one for a frame.
   */
  private resetForClip(): void {
    this.clip.set(undefined);
    this.preview.set(null);
    this.candidate.set(null);
    this.publications.set(null);
    this.publishJobs.set(null);
    this.lineage.set(null);
    this.lineageFailure.set(null);
    this.showHistory.set(false);
    this.source.set({ kind: 'poster' });
    this.uploadRequested.set(false);
    this.error.set(null);
    this.loadFailure.set(null);
    this.deliveryFailure.set(null);
    this.saved.set(null);

    this.draftTitle.set(null);
    this.draftDescription.set(null);
    this.draftNote.set(null);
    this.draftTags.set(null);
    this.draftChannel.set(null);
    this.draftPrivacy.set(null);
    this.draftCategory.set(null);
    this.draftSchedule.set('');
    this.showPublishOptions.set(false);

    this.showMusic.set(false);
    this.musicSource.set('');

    this.showRemake.set(false);
    this.remakeNotes.set('');
    this.remakeFraming.set('UNCHANGED');
    this.remakeCrop.set(null);
    this.remakeKeyframes.set([]);
    this.remakeVoiceOn.set(false);
    this.remakeHideMarks.set(false);
    this.remakeKeepMarks.set(false);
    this.remakeScript.set('');
    this.remakeKeepMusic.set(true);
    this.remakeStartDelta.set(0);
    this.remakeEndDelta.set(0);
    this.playhead.set(0);
  }

  /**
   * The other versions of this clip, fetched when the reviewer asks for them.
   *
   * A one-shot read, so there is nothing to release. The list stays null on
   * failure so that pressing Show history again retries it, and
   * {@link lineageFailure} is what stops the panel sitting on "Loading…" behind
   * a query that has already given up.
   */
  protected async openHistory(): Promise<void> {
    this.showHistory.set(true);
    if (this.lineage()?.length) return;
    const clip = this.clip();
    if (!clip) return;
    this.lineageFailure.set(null);
    try {
      this.lineage.set(await this.clips.loadLineage(clip.lineageId ?? clip.id));
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      this.lineageFailure.set(message);
      this.error.set(message);
    }
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
      this.clips.editClip(clip.id, {
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
      this.clips.review(clip.id, review, clip.storagePath, {
        title: this.title().trim() || null,
        description: this.description().trim() || null,
        reviewNote: this.note().trim() || null,
      }),
    );
  }

  protected async publish(): Promise<void> {
    const clip = this.clip();
    const uid = this.session.uid;
    if (!clip || !uid) return;
    // Refused here as well as by the disabled button, because a disabled
    // button is a hint to a person and not a constraint on a program. Queueing
    // a second publish behind one that has not run yet is how the same video
    // reaches the same channel twice.
    //
    // A null state is refused too, and that is the point of it being nullable:
    // "I do not know what has already happened to this clip" is the one answer
    // that must never be read as "nothing has".
    const state = this.publishState();
    if (!state || (state.kind !== 'READY' && state.kind !== 'FAILED')) return;

    const when = this.draftSchedule() ? new Date(this.draftSchedule()) : null;
    await this.run('Queued for publishing', async () => {
      await this.publishing.requestPublish(uid, clip.id, when, this.publishOptions());
    });
  }

  /**
   * Call off a publish that has not started.
   *
   * The counterpart to refusing a second one: a job queued for Friday is
   * claimable by any worker that comes up after Friday, so "I changed my mind"
   * has to be expressible before then — and cancelling the job is the only
   * place it can be said, because there is no publication yet to withdraw.
   */
  protected async cancelScheduled(): Promise<void> {
    const job = this.publishState()?.job;
    if (!job || job.status !== 'QUEUED') return;
    await this.run('Publish called off', () => this.jobs.cancel(job.id));
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
      await this.publishing.requestUpload(uid, clip.id);
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
    if (!clip || !uid || !this.canAddMusic()) return;

    const options: MusicOptions = {
      source: this.musicSource().trim(),
      mode: this.musicMode(),
      captions: this.musicCaptions(),
    };

    await this.run(
      'Music queued — the scored version will appear in the review queue',
      async () => {
        await this.publishing.requestMusic(uid, clip.id, options);
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

  /**
   * The rectangles this source hides on every clip, including ones not yet cut.
   *
   * Loaded rather than assumed, because a standing rule set weeks ago is
   * invisible otherwise: a clip arrives with a corner reconstructed, nobody
   * asked for it on this clip, and there is nothing anywhere that says why.
   */
  protected readonly alwaysHidden = signal<ObscureRegion[]>([]);

  /** Is what this remake hid already what the channel always hides? */
  protected readonly marksAreRemembered = computed(() => {
    const hidden = this.clip()?.remake?.obscured ?? [];
    const standing = this.alwaysHidden();
    if (!hidden.length || !standing.length) return false;
    return hidden.every((region) => standing.some((kept) => sameRegion(region, kept)));
  });

  private async loadStandingMarks(sourceId: string | null | undefined): Promise<void> {
    if (!sourceId) {
      this.alwaysHidden.set([]);
      return;
    }
    const source = await this.sources.loadSource(sourceId);
    this.alwaysHidden.set(source?.obscure?.regions ?? []);
  }

  /**
   * Promote what this remake hid into a property of the channel.
   *
   * The one action that turns this from a thing you ask for into a thing that
   * has already happened: after it, RENDER applies these as it cuts, so the
   * next clip from this source arrives clean rather than arriving wrong.
   */
  protected async rememberMarks(): Promise<void> {
    const clip = this.clip();
    const regions = clip?.remake?.obscured ?? [];
    if (!clip?.sourceId || !regions.length) return;
    await this.run('This channel will have these hidden from now on', async () => {
      await this.sources.rememberObscure(clip.sourceId!, {
        auto: false,
        regions: upToSix(regions.map((region) => ({ ...region, found: 'REMEMBERED' }))),
        method: null,
        strength: null,
      });
      await this.loadStandingMarks(clip.sourceId);
    });
  }

  /** Stop hiding them on future clips. Clips already cut keep what they have. */
  protected async forgetMarks(): Promise<void> {
    const sourceId = this.clip()?.sourceId;
    if (!sourceId) return;
    await this.run('Future clips from this channel will be left alone', async () => {
      await this.sources.rememberObscure(sourceId, null);
      this.alwaysHidden.set([]);
    });
  }

  /** "top right (14% x 9%)", the same phrase the worker records. */
  protected describeRegion(region: ObscureRegion): string {
    if (region.label) return region.label;
    const midX = region.xPct + region.wPct / 2;
    const midY = region.yPct + region.hPct / 2;
    const down = midY < 40 ? 'top' : midY > 60 ? 'bottom' : 'middle';
    const across = midX < 40 ? 'left' : midX > 60 ? 'right' : 'centre';
    return `${down} ${across} (${Math.round(region.wPct)}% x ${Math.round(region.hPct)}%)`;
  }

  /**
   * Forget this clip.
   *
   * The record only. Its file stays on whichever machine holds it, and its
   * publications stay too — those are the record of what was actually posted
   * and where, and an audit trail that vanishes when somebody tidies their
   * queue is not an audit trail.
   */
  protected async deleteClip(): Promise<void> {
    const clip = this.clip();
    if (!clip) return;
    const name = clip.title?.trim() || clip.id;
    if (
      !confirm(
        `Delete "${name}" from the review queue? Its video file stays on this machine until you ` +
          'remove it in Settings, Storage.',
      )
    ) {
      return;
    }
    await this.run('Clip deleted', async () => {
      await this.clips.deleteClip(clip.id);
      await this.router.navigate(['/review']);
    });
  }

  /**
   * Delete a compilation or a drawn video: the clip, and the job that made it.
   *
   * Offered beside the section that says what the clip is, because a clip
   * with no single source is deleted as a whole — there is no source page it
   * belongs to, and the job is the only other record of it. Records only,
   * as ever (docs/adr/0017-deleting-a-record-is-not-deleting-a-file.md).
   */
  protected async deleteMade(kind: 'compilation' | 'drawn video'): Promise<void> {
    const clip = this.clip();
    if (!clip) return;
    const name = clip.title?.trim() || clip.id;
    if (
      !confirm(
        `Delete the ${kind} "${name}" and the job that made it? The video file stays on this ` +
          'machine until you remove it in Settings, Storage.',
      )
    ) {
      return;
    }
    await this.run(`${kind[0]!.toUpperCase()}${kind.slice(1)} deleted`, async () => {
      await this.clips.deleteClip(clip.id);
      if (clip.jobId) {
        try {
          await this.jobs.deleteJob(clip.jobId);
        } catch {
          // The job may already be gone; the clip is what was asked about.
        }
      }
      await this.router.navigate(['/review']);
    });
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
      // Keeping the previous marks and looking for new ones compose: the worker
      // skips anything it finds that overlaps a region already listed, so
      // asking for both never puts two filters over the same pixels.
      obscure:
        this.remakeHideMarks() || this.remakeKeepMarks()
          ? {
              auto: this.remakeHideMarks(),
              regions: upToSix(this.remakeKeepMarks() ? (clip.remake?.obscured ?? []) : []),
              method: null,
              strength: null,
            }
          : null,
      profile: null,
      // An opt-OUT, so the boolean goes out on every remake rather than only
      // when it is false: the worker reads null and true alike as "carry it",
      // and saying so explicitly is what makes the request readable later
      // beside a clip that came back without its track.
      keepMusic: this.remakeKeepMusic(),
      // Omitted rather than sent as KEEP when the box is unticked: the rules
      // accept an absent field, and an absent one is what "the reviewer did not
      // raise this" has meant everywhere else in these options.
      ...(this.remakeRemoveCaptions() ? { captions: 'REMOVE' as const } : {}),
    };

    await this.run('Remake queued — the new version will appear in the review queue', async () => {
      await this.publishing.requestRemake(uid, clip.id, options);
      this.showRemake.set(false);
      this.remakeNotes.set('');
    });
  }
}
