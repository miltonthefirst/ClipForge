import { Injectable, Injector, computed, effect, inject, signal } from '@angular/core';
import type { UserProfile } from '@clipforge/contracts';
import type { User } from 'firebase/auth';

import { FirebaseService } from './firebase';
import { FirestoreGateway, type Live } from './firestore/gateway';

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
  private readonly db = inject(FirestoreGateway);
  private readonly injector = inject(Injector);

  /** The shared profile listener, and the effect mirroring it into `profile`. */
  private held: Live<UserProfile> | null = null;
  private mirror: { destroy: () => void } | null = null;
  /** Guards the first-sign-in write: the effect re-runs, the row is made once. */
  private seeded = false;

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
      this.forgetProfile();

      if (!user) {
        this.profile.set(null);
        return;
      }
      this.profile.set(undefined);
      this.watchProfile(user);
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
  private watchProfile(user: User): void {
    this.held?.release();
    this.seeded = false;
    const held = this.db.liveDoc<UserProfile>('users', user.uid);
    this.held = held;

    this.mirror?.destroy();
    this.mirror = effect(
      () => {
        const profile = held.data();
        if (profile) {
          this.profile.set(profile);
          return;
        }

        // Absent is not the same as not-yet-arrived, and acting on the wrong
        // one would write a profile row over the top of a read still in
        // flight. The gate answers that question separately, which is the
        // whole reason it does.
        if (held.loading()) return;

        if (held.error()) {
          this.profile.set(null);
          return;
        }

        // Loaded, and there is nothing there: first sign-in. The client may
        // write this row and only this shape — rules pin role and status, so
        // an account cannot admit itself.
        this.profile.set(null);
        if (this.seeded) return;
        this.seeded = true;
        void this.db
          .create(
            'users',
            {
              uid: user.uid,
              email: user.email ?? '',
              displayName: user.displayName ?? null,
              photoUrl: user.photoURL ?? null,
              role: 'MEMBER',
              status: 'PENDING',
              createdAt: new Date().toISOString(),
              decidedAt: null,
              decidedBy: null,
            },
            user.uid,
          )
          // Losing this race is harmless — the listener delivers whichever
          // write won. Anything else surfaces on the waiting screen.
          .catch(() => this.profile.set(null));
      },
      { injector: this.injector },
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
    this.forgetProfile();
    return this.firebase.signOut();
  }

  /**
   * Let go of the profile listener.
   *
   * `release` rather than an unsubscribe: the gate may be holding this document
   * for someone else, and it keeps it warm for a while either way — which is
   * what makes signing back in immediate rather than another round trip.
   */
  private forgetProfile(): void {
    this.mirror?.destroy();
    this.mirror = null;
    this.held?.release();
    this.held = null;
    this.seeded = false;
  }
}
