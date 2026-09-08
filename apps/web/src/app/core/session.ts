import { Injectable, inject, signal } from '@angular/core';
import type { User } from 'firebase/auth';

import { FirebaseService } from './firebase';

/** Who is signed in, as a signal, plus the two actions that change it. */
@Injectable({ providedIn: 'root' })
export class SessionService {
  private readonly firebase = inject(FirebaseService);

  /**
   * `undefined` means "not yet determined" and is distinct from `null`, which
   * means "signed out". Collapsing the two would flash the sign-in screen at
   * every signed-in user on every load.
   */
  readonly user = signal<User | null | undefined>(undefined);

  constructor() {
    this.firebase.onUserChanged((user) => this.user.set(user));
  }

  get uid(): string | null {
    return this.user()?.uid ?? null;
  }

  signIn(): Promise<unknown> {
    return this.firebase.signIn();
  }

  signOut(): Promise<void> {
    return this.firebase.signOut();
  }
}
