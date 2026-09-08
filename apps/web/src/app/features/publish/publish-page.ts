import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  effect,
  inject,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import type { Clip, ClipPreview, Publication, RightsBasis } from '@clipforge/contracts';

import { RIGHTS_BASES, checkPublishable, draftIsComplete } from '../../core/rights';
import { SessionService } from '../../core/session';
import { ClipForgeStore } from '../../core/store';

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

  protected readonly bases = RIGHTS_BASES;
  protected readonly cards = signal<PublishCard[] | null>(null);
  protected readonly error = signal<string | null>(null);
  protected readonly busy = signal<string | null>(null);
  protected readonly queued = signal<string | null>(null);

  /** The attestation being composed, per clip. Never sent until it is complete. */
  protected readonly draftBasis = signal<Record<string, RightsBasis | null>>({});
  protected readonly draftNote = signal<Record<string, string>>({});
  protected readonly draftSchedule = signal<Record<string, string>>({});

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
  }

  ngOnDestroy(): void {
    this.stop?.();
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
      await this.store.requestPublish(uid, card.clip.id, when ? new Date(when) : null);
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
