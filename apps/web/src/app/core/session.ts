import { Injectable, computed, inject, signal } from '@angular/core';
import type { UserProfile } from '@clipforge/contracts';
import type { User } from 'firebase/auth';
import { doc, onSnapshot, setDoc, type Unsubscribe } from 'firebase/firestore';

import { fromDocument } from './documents';
import { FirebaseService } from './firebase';

/**
 * Who is signed in, and — separately — whether they are allowed in.
 *
 * Those are two different questions and the app treats them as such.
 * Authentication says an account exists; the `users/{uid}` profile says an admin
 * has approved it. Security rules enforce the second, so the UI's job is to
 * explain it rather than to guard it: a pending user is shown why they are
 * waiting instead of an empty page and a console full of permission errors.
 */
@Injectable({ providedIn: 'root' })
export class SessionService {
  private readonly firebase = inject(FirebaseService);
  private stopProfile: Unsubscribe | null = null;

  /**
   * `undefined` means "not yet determined" and is distinct from `null`, which
   * means "signed out". Collapsing the two would flash the sign-in screen at
   * every signed-in user on every load.
   */
  readonly user = signal<User | null | undefined>(undefined);

  /** Same three-state convention, for the profile behind the account. */
  readonly profile = signal<UserProfile | null | undefined>(undefined);

  readonly signedIn = computed(() => !!this.user());
  readonly checking = computed(() => this.user() === undefined);

  /** Signed in, profile loaded, and an admin has said yes. */
  readonly approved = computed(() => this.profile()?.status === 'APPROVED');
  readonly isAdmin = computed(() => this.approved() && this.profile()?.role === 'ADMIN');

  /** Signed in but not yet let in — the state the waiting screen explains. */
  readonly awaitingApproval = computed(() => {
    const profile = this.profile();
    return this.signedIn() && !!profile && profile.status !== 'APPROVED';
  });

  constructor() {
    this.firebase.onUserChanged((user) => {
      this.user.set(user);
      this.stopProfile?.();
      this.stopProfile = null;

      if (!user) {
        this.profile.set(null);
        return;
      }
      this.profile.set(undefined);
      void this.watchProfile(user);
    });
  }

  get uid(): string | null {
    return this.user()?.uid ?? null;
  }

  /**
   * Follow the profile, creating it the first time.
   *
   * A live listener rather than a one-off read, so an approval takes effect
   * while the person is looking at the waiting screen — which is exactly when
   * they are looking at it.
   */
  private async watchProfile(user: User): Promise<void> {
    const reference = doc(this.firebase.db, 'users', user.uid);

    this.stopProfile = onSnapshot(
      reference,
      (snapshot) => {
        if (snapshot.exists()) {
          this.profile.set(fromDocument<UserProfile>(snapshot.data()));
          return;
        }
        // First sign-in. The client may write this row and only this shape:
        // rules pin role and status, so an account cannot admit itself.
        void setDoc(reference, {
          uid: user.uid,
          email: user.email ?? '',
          displayName: user.displayName ?? null,
          photoUrl: user.photoURL ?? null,
          role: 'MEMBER',
          status: 'PENDING',
          createdAt: new Date().toISOString(),
          decidedAt: null,
          decidedBy: null,
        }).catch(() => {
          // Losing this race is harmless — the listener will deliver whichever
          // write won. Anything else surfaces on the waiting screen.
          this.profile.set(null);
        });
      },
      () => this.profile.set(null),
    );
  }

  signInWithGoogle(): Promise<unknown> {
    return this.firebase.signIn();
  }

  signInWithPassword(email: string, password: string): Promise<unknown> {
    return this.firebase.signInWithPassword(email, password);
  }

  register(email: string, password: string, displayName: string): Promise<unknown> {
    return this.firebase.register(email, password, displayName);
  }

  resetPassword(email: string): Promise<void> {
    return this.firebase.resetPassword(email);
  }

  signOut(): Promise<void> {
    this.stopProfile?.();
    this.stopProfile = null;
    return this.firebase.signOut();
  }
}
