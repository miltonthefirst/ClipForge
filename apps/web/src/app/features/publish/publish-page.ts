import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  effect,
  inject,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import type {
  Channel,
  Clip,
  ClipPreview,
  Publication,
  PublishOptions,
  PublishPrivacy,
  RightsBasis,
} from '@clipforge/contracts';

import { RIGHTS_BASES, checkPublishable, draftIsComplete } from '../../core/rights';
import { SessionService } from '../../core/session';
import { ClipForgeStore } from '../../core/store';
import {
  CATEGORIES,
  DEFAULT_CATEGORY_ID,
  PRIVACY_OPTIONS,
  categoryLabel,
} from '../../core/youtube';

/**
 * The publish queue: approved clips, and the one question that stands between
 * them and a channel.
 *
 * Publishing a derived clip of someone else's video is a different act from
 * making one privately, and the legal basis is the operator's to establish.
 * ClipForge does not answer that question and does not pretend it away — it asks
 * it, once, in plain language, and records the answer where an audit can find it.
 *
 * The gate is enforced in firestore.rules and in the worker. What this screen
 * adds is the *explanation*: a Publish button that failed opaquely would be
 * worse than one that is visibly disabled with the reason next to it.
 */
export interface PublishCard {
  readonly clip: Clip;
  readonly preview: ClipPreview | null;
  readonly publications: Publication[];
}

@Component({
  selector: 'app-publish-page',
  imports: [FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './publish-page.html',
})
export class PublishPage implements OnDestroy {
  private readonly store = inject(ClipForgeStore);
  private readonly session = inject(SessionService);

  private stop: (() => void) | null = null;
  private stopChannels: (() => void) | null = null;

  protected readonly bases = RIGHTS_BASES;
  protected readonly categories = CATEGORIES;
  protected readonly privacies = PRIVACY_OPTIONS;
  protected readonly categoryLabel = categoryLabel;

  protected readonly cards = signal<PublishCard[] | null>(null);
  protected readonly channels = signal<Channel[]>([]);
  protected readonly error = signal<string | null>(null);
  protected readonly busy = signal<string | null>(null);
  protected readonly queued = signal<string | null>(null);

  /** The attestation being composed, per clip. Never sent until it is complete. */
  protected readonly draftBasis = signal<Record<string, RightsBasis | null>>({});
  protected readonly draftNote = signal<Record<string, string>>({});
  protected readonly draftSchedule = signal<Record<string, string>>({});

  /**
   * The overrides being composed, per clip.
   *
   * An **absent** key means the operator never touched that field, and that is
   * not the same as an empty one: a cleared tag list says "no tags on this
   * upload" and must not fall through to the channel's. `Record` with optional
   * values keeps the two states apart all the way to the wire, where
   * `PublishOptions` keeps them apart too — see the note on `tagsFor`.
   */
  protected readonly panelOpen = signal<Record<string, boolean>>({});
  protected readonly draftChannel = signal<Record<string, string>>({});
  protected readonly draftTitle = signal<Record<string, string>>({});
  protected readonly draftDescription = signal<Record<string, string>>({});
  protected readonly draftPrivacy = signal<Record<string, PublishPrivacy>>({});
  protected readonly draftCategory = signal<Record<string, string>>({});
  protected readonly draftTags = signal<Record<string, string>>({});

  constructor() {
    effect((onCleanup) => {
      const uid = this.session.uid;
      if (!uid) {
        this.cards.set(null);
        return;
      }
      const stop = this.store.watchApproved(
        uid,
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
  }

  ngOnDestroy(): void {
    this.stop?.();
    this.stopChannels?.();
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

  protected posterSrc(card: PublishCard): string | null {
    return card.preview ? `data:image/jpeg;base64,${card.preview.posterBase64}` : null;
  }

  /** Why this clip cannot be published yet, in words the user can act on. */
  protected refusal(card: PublishCard): string | null {
    return checkPublishable(card.clip)?.message ?? null;
  }

  protected attested(card: PublishCard): boolean {
    return checkPublishable(card.clip) === null;
  }

  /** A clip already sent to a platform, if any. */
  protected published(card: PublishCard): Publication | null {
    return card.publications.find((p) => p.state === 'PUBLISHED') ?? null;
  }

  protected pending(card: PublishCard): Publication | null {
    return card.publications.find((p) => p.state === 'PENDING' || p.state === 'UPLOADING') ?? null;
  }

  protected failed(card: PublishCard): Publication | null {
    return card.publications.find((p) => p.state === 'FAILED') ?? null;
  }

  protected basisFor(clipId: string): RightsBasis | null {
    return this.draftBasis()[clipId] ?? null;
  }

  protected noteFor(clipId: string): string {
    return this.draftNote()[clipId] ?? '';
  }

  protected scheduleFor(clipId: string): string {
    return this.draftSchedule()[clipId] ?? '';
  }

  protected chooseBasis(clipId: string, basis: RightsBasis): void {
    this.draftBasis.update((all) => ({ ...all, [clipId]: basis }));
  }

  protected setNote(clipId: string, note: string): void {
    this.draftNote.update((all) => ({ ...all, [clipId]: note }));
  }

  protected setSchedule(clipId: string, when: string): void {
    this.draftSchedule.update((all) => ({ ...all, [clipId]: when }));
  }

  // ── Per-publish overrides ──────────────────────────────────────────────
  //
  // Every accessor below answers "what will actually go out", falling back
  // through the same layers the worker's resolver uses
  // (apps/worker/clipforge/publish/metadata.py): this publish, then the
  // channel, then the floor. The fields therefore *show* the outcome rather
  // than sitting empty and leaving the operator to guess — and only what the
  // operator changed is sent, so editing the channel's defaults later still
  // affects every publish they did not override.
  //
  // Title and description are the exception: they are shown empty with the
  // default as placeholder text, because the channel's `titleSuffix` means the
  // resolved title is not a value this side can compute without duplicating
  // the part of the resolver that is actually subtle.

  protected isOpen(clipId: string): boolean {
    return this.panelOpen()[clipId] === true;
  }

  protected toggle(clipId: string): void {
    this.panelOpen.update((all) => ({ ...all, [clipId]: !all[clipId] }));
  }

  /** The channel a publish will go to, chosen or defaulted. */
  protected channelFor(clipId: string): Channel | null {
    const chosen = this.draftChannel()[clipId];
    const all = this.channels();
    if (chosen) return all.find((channel) => channel.id === chosen) ?? null;
    if (all.length === 0) return null;
    return all.find((channel) => channel.isDefault) ?? all[0];
  }

  protected channelIdFor(clipId: string): string {
    return this.channelFor(clipId)?.id ?? '';
  }

  protected titleFor(clipId: string): string {
    return this.draftTitle()[clipId] ?? '';
  }

  protected descriptionFor(clipId: string): string {
    return this.draftDescription()[clipId] ?? '';
  }

  protected privacyFor(clipId: string): PublishPrivacy {
    return this.draftPrivacy()[clipId] ?? this.channelFor(clipId)?.defaults.privacy ?? 'unlisted';
  }

  protected categoryFor(clipId: string): string {
    return (
      this.draftCategory()[clipId] ??
      this.channelFor(clipId)?.defaults.categoryId ??
      DEFAULT_CATEGORY_ID
    );
  }

  /**
   * The tag list as text, prefilled from the channel.
   *
   * Prefilled rather than left blank, and that is what makes "no tags on this
   * one" expressible at all: the operator clears a field that had something in
   * it, which is an unmistakable instruction. A blank field that had always
   * been blank could not be told apart from one nobody opened.
   */
  protected tagsFor(clipId: string): string {
    return this.draftTags()[clipId] ?? (this.channelFor(clipId)?.defaults.tags ?? []).join(', ');
  }

  protected setChannel(clipId: string, channelId: string): void {
    this.draftChannel.update((all) => ({ ...all, [clipId]: channelId }));
  }

  protected setTitle(clipId: string, title: string): void {
    this.draftTitle.update((all) => ({ ...all, [clipId]: title }));
  }

  protected setDescription(clipId: string, description: string): void {
    this.draftDescription.update((all) => ({ ...all, [clipId]: description }));
  }

  protected setPrivacy(clipId: string, privacy: PublishPrivacy): void {
    this.draftPrivacy.update((all) => ({ ...all, [clipId]: privacy }));
  }

  protected setCategory(clipId: string, categoryId: string): void {
    this.draftCategory.update((all) => ({ ...all, [clipId]: categoryId }));
  }

  protected setTags(clipId: string, tags: string): void {
    this.draftTags.update((all) => ({ ...all, [clipId]: tags }));
  }

  /** Whether anything was overridden, for the summary line on a closed panel. */
  protected overridden(clipId: string): boolean {
    return (
      this.draftChannel()[clipId] !== undefined ||
      this.titleFor(clipId).trim() !== '' ||
      this.descriptionFor(clipId).trim() !== '' ||
      this.draftPrivacy()[clipId] !== undefined ||
      this.draftCategory()[clipId] !== undefined ||
      this.draftTags()[clipId] !== undefined
    );
  }

  /**
   * What to send, or null when there is nothing to say.
   *
   * Only touched fields are populated. Sending the resolved value of every
   * field instead would look equivalent and is not: it would pin this upload to
   * today's channel defaults, so a scheduled publish would ignore a correction
   * made to the channel before it ran.
   */
  protected optionsFor(clipId: string): PublishOptions | null {
    if (!this.overridden(clipId)) return null;

    const tags = this.draftTags()[clipId];
    return {
      channelId: this.draftChannel()[clipId] ?? null,
      title: this.titleFor(clipId).trim() || null,
      description: this.descriptionFor(clipId).trim() || null,
      privacy: this.draftPrivacy()[clipId] ?? null,
      categoryId: this.draftCategory()[clipId] ?? null,
      // undefined and '' are different answers: never opened, versus opened and
      // emptied. The first falls through to the channel, the second does not.
      tags:
        tags === undefined
          ? null
          : tags
              .split(',')
              .map((tag) => tag.trim())
              .filter(Boolean),
    };
  }

  /** Fair use is the one basis whose reasoning is required, not optional. */
  protected needsNote(clipId: string): boolean {
    return this.basisFor(clipId) === 'FAIR_USE_ASSERTED';
  }

  protected canAttest(clipId: string): boolean {
    return draftIsComplete(this.basisFor(clipId), this.noteFor(clipId));
  }

  protected async attest(card: PublishCard): Promise<void> {
    const uid = this.session.uid;
    const basis = this.basisFor(card.clip.id);
    if (!uid || !basis) return;

    this.busy.set(card.clip.id);
    this.error.set(null);
    try {
      await this.store.attest(uid, card.clip.id, basis, this.noteFor(card.clip.id) || null);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.busy.set(null);
    }
  }

  protected async publish(card: PublishCard): Promise<void> {
    const uid = this.session.uid;
    if (!uid) return;

    // Checked here too, not only by the disabled attribute. A disabled button is
    // a hint to a person, not a constraint on a program.
    if (!this.attested(card)) return;

    const when = this.scheduleFor(card.clip.id);
    this.busy.set(card.clip.id);
    this.error.set(null);
    try {
      await this.store.requestPublish(
        uid,
        card.clip.id,
        when ? new Date(when) : null,
        this.optionsFor(card.clip.id),
      );
      this.queued.set(card.clip.id);
    } catch (err) {
      // The rules refuse this if the attestation is incomplete. Saying so beats
      // showing a raw permission error.
      this.error.set(
        err instanceof Error && err.message.includes('permission')
          ? 'The rights gate refused this publish. Check the attestation is complete and the clip is approved.'
          : err instanceof Error
            ? err.message
            : String(err),
      );
    } finally {
      this.busy.set(null);
    }
  }
}
