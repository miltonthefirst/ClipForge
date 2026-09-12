import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';

import { SessionService } from '../../core/session';

type Mode = 'signIn' | 'register';

/**
 * Sign in, or ask for an account.
 *
 * Email and password lead because Google's flow cannot be relied on here: it
 * holds a sign-in on an unfamiliar device for verification — up to 48 hours —
 * which is survivable on a phone and useless on the desktop shell sitting next
 * to the worker. Google stays as a second option because on the web it is one
 * tap and no password to remember.
 *
 * Registering grants nothing. The account lands PENDING and every collection
 * stays closed until an admin approves it, which is what the waiting screen
 * then explains.
 */
@Component({
  selector: 'app-sign-in-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './sign-in-page.html',
})
export class SignInPage {
  private readonly session = inject(SessionService);

  protected readonly mode = signal<Mode>('signIn');
  protected readonly email = signal('');
  protected readonly password = signal('');
  protected readonly displayName = signal('');
  protected readonly busy = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly notice = signal<string | null>(null);

  protected readonly registering = computed(() => this.mode() === 'register');

  protected readonly canSubmit = computed(() => {
    const hasCredentials = this.email().trim().length > 3 && this.password().length >= 6;
    if (!hasCredentials || this.busy()) return false;
    return this.registering() ? this.displayName().trim().length > 0 : true;
  });

  protected setMode(mode: Mode): void {
    this.mode.set(mode);
    this.error.set(null);
    this.notice.set(null);
  }

  protected async submit(): Promise<void> {
    if (!this.canSubmit()) return;
    this.busy.set(true);
    this.error.set(null);
    this.notice.set(null);
    try {
      if (this.registering()) {
        await this.session.register(this.email().trim(), this.password(), this.displayName());
      } else {
        await this.session.signInWithPassword(this.email().trim(), this.password());
      }
    } catch (err) {
      this.error.set(describe(err));
    } finally {
      this.busy.set(false);
      this.password.set('');
    }
  }

  protected async withGoogle(): Promise<void> {
    this.busy.set(true);
    this.error.set(null);
    try {
      await this.session.signInWithGoogle();
    } catch (err) {
      this.error.set(describe(err));
    } finally {
      this.busy.set(false);
    }
  }

  protected async forgotPassword(): Promise<void> {
    const email = this.email().trim();
    if (!email) {
      this.error.set('Enter your email address first, then choose "Forgot password".');
      return;
    }
    this.busy.set(true);
    this.error.set(null);
    try {
      await this.session.resetPassword(email);
      this.notice.set(`If ${email} has an account, a reset link is on its way.`);
    } catch (err) {
      this.error.set(describe(err));
    } finally {
      this.busy.set(false);
    }
  }
}

/**
 * Firebase's error codes, in words.
 *
 * The raw ones read like `auth/invalid-credential`, which tells a person
 * nothing about what to do next. Anything unrecognised falls through to the
 * original message rather than a generic apology that hides a real fault.
 */
function describe(error: unknown): string {
  const code = (error as { code?: string } | null)?.code ?? '';
  switch (code) {
    case 'auth/invalid-credential':
    case 'auth/wrong-password':
    case 'auth/user-not-found':
      return 'That email and password do not match an account.';
    case 'auth/invalid-email':
      return 'That does not look like an email address.';
    case 'auth/email-already-in-use':
      return 'An account with that email already exists. Sign in instead.';
    case 'auth/weak-password':
      return 'Firebase needs at least six characters. Longer is better.';
    case 'auth/too-many-requests':
      return 'Too many attempts. Wait a minute and try again.';
    case 'auth/network-request-failed':
      return 'No network. The app needs a connection to sign in.';
    case 'auth/operation-not-allowed':
      return 'Email and password sign-in is disabled for this Firebase project.';
    default:
      return error instanceof Error ? error.message : String(error);
  }
}
