import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  computed,
  effect,
  inject,
  signal,
} from '@angular/core';
import type { UserProfile, UserRole, UserStatus } from '@clipforge/contracts';

import { SessionService } from '../../core/session';
import { ClipForgeStore } from '../../core/store';

/**
 * People: who has an account, and who is allowed to use it.
 *
 * Registration is open and access is not, so this is where the second half
 * happens. It is admin-only in the rules as well as in the router — a member who
 * navigates here directly gets a failed listener rather than a filtered list,
 * which is the correct outcome and the reason the error is shown rather than
 * swallowed.
 */
@Component({
  selector: 'app-users-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './users-page.html',
})
export class UsersPage implements OnDestroy {
  private readonly store = inject(ClipForgeStore);
  private readonly session = inject(SessionService);
  private stop: (() => void) | null = null;

  protected readonly users = signal<UserProfile[] | null>(null);
  protected readonly error = signal<string | null>(null);
  protected readonly busy = signal<string | null>(null);

  /** Pending first: it is the only group with an action waiting on someone. */
  protected readonly pending = computed(() =>
    (this.users() ?? []).filter((u) => u.status === 'PENDING'),
  );
  protected readonly decided = computed(() =>
    (this.users() ?? []).filter((u) => u.status !== 'PENDING'),
  );

  protected readonly myUid = computed(() => this.session.uid);

  constructor() {
    effect((onCleanup) => {
      if (!this.session.isAdmin()) return;
      const stop = this.store.watchUsers(
        (users) => this.users.set(users),
        (err) => this.error.set(err.message),
      );
      this.stop = stop;
      onCleanup(stop);
    });
  }

  ngOnDestroy(): void {
    this.stop?.();
  }

  protected async decide(user: UserProfile, changes: { status?: UserStatus; role?: UserRole }) {
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
      await this.store.decideUser(adminUid, user.uid, changes);
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
