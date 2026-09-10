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
import type { Channel } from '@clipforge/contracts';

import { LocalApiService, type YouTubeStatus } from '../../core/local-api';
import { SessionService } from '../../core/session';
import { ClipForgeStore } from '../../core/store';
import { CATEGORIES } from '../../core/youtube';

/**
 * Connecting a YouTube channel, and deciding what a publish does by default.
 *
 * The page has two halves for a reason that is worth seeing in the layout: the
 * **credential** half only works in the desktop shell, because the secret is
 * handed to the worker on this machine and never travels
 * (docs/adr/0010, docs/adr/0011); the **defaults** half is ordinary Firestore
 * data and works from a phone.
 *
 * On the web the first half explains itself rather than offering a form that
 * could not possibly work.
 */
@Component({
  selector: 'app-youtube-page',
  imports: [RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './youtube-page.html',
})
export class YouTubePage implements OnDestroy {
  private readonly local = inject(LocalApiService);
  private readonly store = inject(ClipForgeStore);
  private readonly session = inject(SessionService);
  private stop: (() => void) | null = null;

  protected readonly categories = CATEGORIES;

  /** From the worker, over loopback. Null when this is a browser. */
  protected readonly status = signal<YouTubeStatus | null>(null);
  /** From Firestore. Readable anywhere. */
  protected readonly channel = signal<Channel | null | undefined>(undefined);

  protected readonly checkingLocal = signal(true);
  protected readonly localAvailable = signal(false);
  protected readonly localReason = this.local.unavailableReason;

  protected readonly clientId = signal('');
  protected readonly clientSecret = signal('');
  protected readonly busy = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly notice = signal<string | null>(null);
  /** True from pressing Authorise until the browser comes back, or gives up. */
  protected readonly authorising = signal(false);

  /** Draft defaults, so a half-typed tag list is not written on every keystroke. */
  protected readonly draftPrivacy = signal<string | null>(null);
  protected readonly draftCategory = signal<string | null>(null);
  protected readonly draftTags = signal<string | null>(null);
  protected readonly draftSuffix = signal<string | null>(null);
  protected readonly draftDescription = signal<string | null>(null);

  protected readonly privacy = computed(
    () => this.draftPrivacy() ?? this.channel()?.defaults.privacy ?? 'unlisted',
  );
  protected readonly category = computed(
    () => this.draftCategory() ?? this.channel()?.defaults.categoryId ?? '22',
  );
  protected readonly tags = computed(
    () => this.draftTags() ?? (this.channel()?.defaults.tags ?? []).join(', '),
  );
  protected readonly titleSuffix = computed(
    () => this.draftSuffix() ?? this.channel()?.defaults.titleSuffix ?? '',
  );
  protected readonly descriptionTemplate = computed(
    () => this.draftDescription() ?? this.channel()?.defaults.descriptionTemplate ?? '',
  );

  protected readonly canSaveClient = computed(
    () =>
      !this.busy() && this.clientId().trim().length > 10 && this.clientSecret().trim().length > 5,
  );

  /**
   * What the operator should do next, in one sentence.
   *
   * Derived from the worker's view rather than Firestore's, because the worker
   * is the thing that would actually fail — a channel document can say
   * CONNECTED while the token file underneath it has been deleted.
   */
  protected readonly nextStep = computed(() => {
    const status = this.status();
    if (!status) return null;
    if (!status.publishingEnabled) {
      return 'Publishing is off. Set CLIPFORGE_PUBLISHING_ENABLED=true in .env and restart the worker.';
    }
    switch (status.connection) {
      case 'NOT_CONFIGURED':
        return 'Add the OAuth client id and secret below.';
      case 'NEEDS_AUTH':
        return 'Client saved. Press Authorise to sign in to YouTube.';
      case 'CONNECTED':
        return null;
      default:
        return status.message;
    }
  });

  /**
   * Testing-mode refresh tokens expire after 7 days, so the token's age is the
   * number that predicts the next failure rather than a curiosity.
   */
  protected readonly tokenExpiringSoon = computed(() => {
    const age = this.status()?.tokenAgeDays;
    return age !== null && age !== undefined && age >= 6;
  });

  constructor() {
    void this.refresh();

    effect((onCleanup) => {
      if (!this.session.uid) return;
      const stop = this.store.watchChannel(
        'youtube-primary',
        (channel) => this.channel.set(channel),
        () => this.channel.set(null),
      );
      this.stop = stop;
      onCleanup(stop);
    });
  }

  ngOnDestroy(): void {
    this.stop?.();
  }

  protected async refresh(): Promise<void> {
    this.checkingLocal.set(true);
    try {
      const handshake = await this.local.connect();
      this.localAvailable.set(handshake.available);
      if (handshake.available) {
        this.status.set(await this.local.status());
      }
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.checkingLocal.set(false);
    }
  }

  protected async saveClient(): Promise<void> {
    if (!this.canSaveClient()) return;
    this.busy.set(true);
    this.error.set(null);
    this.notice.set(null);
    try {
      await this.local.setClient(this.clientId().trim(), this.clientSecret().trim());
      // Cleared immediately. It is on the worker now, and there is no reason for
      // it to sit in a form field afterwards.
      this.clientSecret.set('');
      this.notice.set('Saved to this machine. Now press Authorise.');
      this.status.set(await this.local.status());
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.busy.set(false);
    }
  }

  /**
   * Sign in to YouTube without leaving the app.
   *
   * Testing-mode refresh tokens expire every seven days, so this is a weekly
   * action rather than a one-off, and a weekly action should not require
   * remembering a command.
   *
   * The worker returns as soon as the browser is open, because the rest of the
   * dance waits on a person. So this polls `/status` until the token lands —
   * the same endpoint the page already uses, rather than a second mechanism
   * that could disagree with it.
   */
  protected async authorise(): Promise<void> {
    this.busy.set(true);
    this.error.set(null);
    this.notice.set(null);
    try {
      const started = await this.local.authorise(this.status()?.hasToken === true);
      if (!started) return;
      if (started.already) {
        this.notice.set(started.message ?? 'Already authorised.');
        return;
      }

      this.authorising.set(true);
      this.notice.set(
        started.openedBrowser === false
          ? 'Could not open a browser. Open this URL to continue: ' + (started.url ?? '')
          : 'Finish signing in with the browser window that just opened.',
      );

      const deadline = Date.now() + (started.timeoutSec ?? 180) * 1000 + 5_000;
      while (Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 1_500));
        const status = await this.local.status();
        if (!status) break;
        this.status.set(status);
        if (status.connection === 'CONNECTED') {
          this.notice.set('Connected. ClipForge can publish to this channel.');
          return;
        }
        if (status.authError) {
          this.error.set(status.authError);
          this.notice.set(null);
          return;
        }
        if (!status.authorising) break;
      }
      // Fell out of the loop without a verdict: the window was probably closed.
      this.error.set('Authorisation did not finish. Press Authorise to try again.');
      this.notice.set(null);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.authorising.set(false);
      this.busy.set(false);
    }
  }

  protected async disconnect(): Promise<void> {
    this.busy.set(true);
    this.error.set(null);
    this.notice.set(null);
    try {
      await this.local.disconnect();
      this.notice.set('Authorisation removed. The client id and secret are kept.');
      this.status.set(await this.local.status());
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.busy.set(false);
    }
  }

  protected async saveDefaults(): Promise<void> {
    this.busy.set(true);
    this.error.set(null);
    this.notice.set(null);
    try {
      await this.local.setDefaults({
        privacy: this.privacy(),
        categoryId: this.category(),
        tags: this.tags()
          .split(',')
          .map((tag) => tag.trim())
          .filter(Boolean),
        titleSuffix: this.titleSuffix().trim() || null,
        descriptionTemplate: this.descriptionTemplate().trim() || null,
      });
      this.notice.set('Defaults saved.');
      this.draftPrivacy.set(null);
      this.draftCategory.set(null);
      this.draftTags.set(null);
      this.draftSuffix.set(null);
      this.draftDescription.set(null);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.busy.set(false);
    }
  }
}
