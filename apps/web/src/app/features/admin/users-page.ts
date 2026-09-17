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
import type { UserProfile } from '@clipforge/contracts';

import { AdminRepository, type AccountDecision } from '../../core/data/admin';
import type { Live } from '../../core/firestore/gateway';
import { SessionService } from '../../core/session';

/**
 * People: who has an account, and who is allowed to use it.
 *
 * Registration is open and access is not, so this is where the second half
 * happens. It is admin-only in the rules as well as in the router — a member who
 * navigates here directly gets a failed listener rather than a filtered list,
 * which is the correct outcome and the reason the failure is shown rather than
 * swallowed.
 */
@Component({
  selector: 'app-users-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './users-page.html',
})
export class UsersPage implements OnDestroy {
  private readonly data = inject(AdminRepository);
  private readonly session = inject(SessionService);

  protected readonly users = signal<UserProfile[] | null>(null);
  /** The banner for something the operator just pressed. */
  protected readonly error = signal<string | null>(null);
  /**
   * Why the list is not here, when it is never going to arrive.
   *
   * A third state, distinct from "still loading" and from "loaded and nobody
   * has signed up", because the page could previously only express two. A
   * refused listener delivers nothing at all, so `users` stayed null and the
   * page went on saying "Loading people…" underneath the error for as long as
   * anyone left it open — which is what a member saw here.
   *
   * Separate from {@link error}: this one replaces the list, because it is
   * about the list.
   */
  protected readonly loadFailure = signal<string | null>(null);
  /** The account a decision is in flight for, so only its own buttons go quiet. */
  protected readonly busy = signal<string | null>(null);

  /**
   * The listener currently held, released when the account changes or the page
   * goes.
   *
   * A signal rather than a plain field because the delivering effect below
   * *reads* it: a field is not tracked, so that effect would run once against
   * no listener and never again, and the page would sit on "Loading people…"
   * for ever with nothing to explain it.
   */
  private readonly held = signal<Live<UserProfile[]> | null>(null);

  /**
   * Pending first: it is the only group with an action waiting on someone.
   *
   * Empty stands in for not-yet-delivered in both of these, which is safe only
   * because the template chooses between loading, failed and loaded before it
   * reads either — a null `users()` is answered there, once, rather than in
   * every group derived from it.
   */
  protected readonly pending = computed(() =>
    (this.users() ?? []).filter((u) => u.status === 'PENDING'),
  );
  protected readonly decided = computed(() =>
    (this.users() ?? []).filter((u) => u.status !== 'PENDING'),
  );

  protected readonly myUid = computed(() => this.session.uid);

  constructor() {
    effect(() => {
      const uid = this.session.uid;
      // Cleared first, and unconditionally: a failure belongs to the listener
      // that produced it, and this effect is about to replace that listener —
      // including with no listener at all, on the way out of the app.
      this.loadFailure.set(null);

      // Let go of the previous listener before taking the next one. The gate
      // keeps it warm for fifteen minutes, so leaving this page and coming back
      // re-attaches to the same listener and costs nothing — which is the whole
      // reason releasing is not the same as unsubscribing.
      // `untracked`, or this effect depends on the signal it is about to write
      // and re-runs itself for ever, releasing and re-opening a listener on
      // every pass — a hang rather than a leak.
      untracked(() => this.held())?.release();
      this.held.set(null);
      this.users.set(null);

      if (!uid) return;

      // Asked for on behalf of whoever is signed in, rather than only when this
      // session believes itself an admin. The rules are the gate, and their
      // refusal is a better answer than a page that quietly shows nothing: it
      // arrives as an error beside empty data, and the page says so.
      this.held.set(this.data.users());
    });

    // Separate from the subscription above so that a delivery does not re-open
    // a listener: this one reads the held signals and nothing else, and reading
    // `held` inside the effect that assigns it would make every snapshot a
    // reason to re-subscribe.
    effect(() => {
      const held = this.held();
      if (!held) return;
      const failure = held.error();
      if (failure) {
        this.loadFailure.set(accountsFailure(failure));
        return;
      }
      const users = held.data();
      // Still loading is not the same as delivered-and-empty, and only the
      // second is news. Acting on the first is what leaves "Loading people…" on
      // screen behind a query that has already failed.
      if (users === null) return;
      this.users.set(users);
    });
  }

  ngOnDestroy(): void {
    this.held()?.release();
  }

  protected async decide(user: UserProfile, changes: AccountDecision) {
    const adminUid = this.session.uid;
    if (!adminUid) return;

    // The rules refuse this too. Checked here so the UI never offers an action
    // that is guaranteed to fail: an admin cannot change their own access, which
    // is what stops the last one locking everybody out with a single tap.
    if (user.uid === adminUid) {
      this.error.set('You cannot change your own role or status.');
      return;
    }

    this.busy.set(user.uid);
    this.error.set(null);
    try {
      await this.data.decideUser(adminUid, user.uid, changes);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.busy.set(null);
    }
  }

  protected label(user: UserProfile): string {
    return user.displayName || user.email;
  }
}

/**
 * What to say when the accounts listener fails outright.
 *
 * A refusal gets its own sentence because it is the expected failure here — the
 * rules allow `users` to be listed by an admin and nobody else — and Firestore's
 * own wording for it says nothing about accounts. Everything else keeps the raw
 * message: an unrecognised failure rewritten into a friendly sentence is how a
 * real cause gets hidden.
 */
function accountsFailure(error: unknown): string {
  const code = (error as { code?: string } | null)?.code;
  const message = error instanceof Error ? error.message : String(error ?? '');

  if (code === 'permission-denied') {
    return 'Only an admin can see the list of accounts.';
  }
  return message || 'The list of accounts could not be loaded, and gave no reason.';
}
