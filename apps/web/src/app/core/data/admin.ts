import { Injectable, inject } from '@angular/core';
import type { UserProfile, UserRole, UserStatus } from '@clipforge/contracts';

import { FirestoreGateway, type Live } from '../firestore/gateway';
import type { QuerySpec } from '../firestore/spec';

/**
 * Accounts: who has one, and whether they are allowed to use it.
 *
 * Registration is open and access is not, so this is the second half of the
 * gate. The two writes here are deliberately not the same write, because the
 * rules grant them to different people: {@link AdminRepository.decideUser} is
 * an admin deciding somebody else's access, and
 * {@link AdminRepository.updateOwnProfile} is anyone changing the cosmetics of
 * their own row. Keeping them apart in the code keeps the difference visible —
 * a member may do the second and may never do the first.
 */

/** One document per person with an account. */
const USERS = 'users';

/**
 * How many accounts the People page lists.
 *
 * `users` would be safe to read unbounded — one document per person with an
 * account, and a deployment where that is a costly read is a deployment with
 * problems this query is not one of. Bounded anyway, so "safe today" does not
 * quietly become an unbounded listen later.
 */
const ACCOUNT_LIMIT = 200;

/** What an admin may change about somebody else's account. */
export type AccountDecision = {
  readonly status?: UserStatus;
  readonly role?: UserRole;
};

/** What anyone may change about their own. */
export type OwnProfileEdit = {
  readonly displayName?: string;
};

/** The shape of a decision as it goes to Firestore. */
type DecisionWrite = {
  status?: UserStatus;
  role?: UserRole;
  decidedAt: string;
  decidedBy: string;
};

/**
 * Every account, newest first.
 *
 * Newest first because the accounts with someone waiting on them are the new
 * ones, and the page shows the pending group above the decided one.
 */
export function accountsSpec(): QuerySpec {
  return {
    collection: USERS,
    orderBy: [['createdAt', 'desc']],
    limit: ACCOUNT_LIMIT,
  };
}

/**
 * The write behind a decision, or `null` when there is nothing to decide.
 *
 * `decidedBy` is stamped from the caller's own uid rather than accepted as an
 * argument, and the rules require the two to match — an audit trail whose
 * author could be supplied by whoever wrote it answers nothing. That is why the
 * fields are picked one at a time rather than spread: a spread carries whatever
 * key the caller attached, including the one that says who did this.
 *
 * A field the caller did not decide is left out rather than sent as
 * `undefined`. Firestore rejects an explicit `undefined` outright, and the
 * rules read the *set of changed keys*, so "absent" and "present but empty" are
 * not the same write.
 *
 * `null` when neither status nor role is changing: stamping `decidedAt` for
 * that would put a decision in the audit trail that decided nothing.
 */
export function decisionPatch(
  adminUid: string,
  decision: AccountDecision,
  at: string,
): DecisionWrite | null {
  const patch: DecisionWrite = { decidedAt: at, decidedBy: adminUid };
  let decided = false;

  if (decision.status !== undefined) {
    patch.status = decision.status;
    decided = true;
  }
  if (decision.role !== undefined) {
    patch.role = decision.role;
    decided = true;
  }

  return decided ? patch : null;
}

/**
 * The cosmetic fields of your own row, or `null` when nothing is being saved.
 *
 * Picked rather than passed through for the same reason as {@link
 * decisionPatch}: the rules allow a member to change `displayName` and
 * `photoUrl` on their own row and nothing else, so a patch that could carry
 * `role` or `status` is one rejected write away from a confusing error message.
 */
export function ownProfilePatch(changes: OwnProfileEdit): { displayName: string } | null {
  return changes.displayName === undefined ? null : { displayName: changes.displayName };
}

@Injectable({ providedIn: 'root' })
export class AdminRepository {
  private readonly db = inject(FirestoreGateway);

  /**
   * Every account, for the admin People page.
   *
   * `list` is admin-only in the rules, so a member's call fails rather than
   * returning a filtered set — and the failure arrives as `error` beside empty
   * `data`, which is the distinction the page needs to say "you may not see
   * this" rather than render an empty list as though nobody had ever signed up.
   *
   * An unapproved account reads almost nothing anywhere in the app. The rules
   * enforce that, and the shell renders a deliberate dead end explaining the
   * wait instead of routing anyone here to discover it as a permission error.
   *
   * Bounded at {@link ACCOUNT_LIMIT} — see the note there for why a collection
   * this small is still asked for with a limit.
   */
  users(): Live<UserProfile[]> {
    return this.db.live<UserProfile>(accountsSpec());
  }

  /**
   * Approve, reject or disable an account, or change its role.
   *
   * The rules refuse this when `adminUid` and `uid` are the same person, which
   * is what stops the last admin locking everybody out with one tap. The People
   * page checks it too, so the UI never offers an action guaranteed to fail.
   */
  async decideUser(adminUid: string, uid: string, decision: AccountDecision): Promise<void> {
    const patch = decisionPatch(adminUid, decision, new Date().toISOString());
    if (!patch) return;
    await this.db.update(USERS, uid, patch);
  }

  /**
   * The parts of your own profile you own.
   *
   * The one write a non-admin may make to a `users` row, and the reason it is a
   * separate method from {@link decideUser}: it cannot reach status or role,
   * and the rules would refuse it if it tried.
   */
  async updateOwnProfile(uid: string, changes: OwnProfileEdit): Promise<void> {
    const patch = ownProfilePatch(changes);
    if (!patch) return;
    await this.db.update(USERS, uid, patch);
  }
}
