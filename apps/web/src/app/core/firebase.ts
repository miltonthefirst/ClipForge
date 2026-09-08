import { Injectable, inject, InjectionToken } from '@angular/core';
import { initializeApp, type FirebaseApp } from 'firebase/app';
import {
  connectAuthEmulator,
  getAuth,
  GoogleAuthProvider,
  onAuthStateChanged,
  signInWithPopup,
  signOut,
  type Auth,
  type User,
} from 'firebase/auth';
import { connectFirestoreEmulator, getFirestore, type Firestore } from 'firebase/firestore';

import { loadConfig, type ClipForgeConfig } from '../../environments';

export const CLIPFORGE_CONFIG = new InjectionToken<ClipForgeConfig>('CLIPFORGE_CONFIG', {
  providedIn: 'root',
  factory: loadConfig,
});

/**
 * One Firebase app for the process, wired to the emulator when configured.
 *
 * The emulator connection is deliberately the default. Routine development must
 * not touch — or cost anything on — the real project, and on the Spark free tier
 * there is no billing safety net to fall back on.
 */
@Injectable({ providedIn: 'root' })
export class FirebaseService {
  private readonly config = inject(CLIPFORGE_CONFIG);
  readonly app: FirebaseApp;
  readonly auth: Auth;
  readonly db: Firestore;

  constructor() {
    this.app = initializeApp(this.config.firebase);
    this.auth = getAuth(this.app);
    this.db = getFirestore(this.app);

    if (this.config.useEmulators) {
      connectAuthEmulator(this.auth, this.config.authEmulator, { disableWarnings: true });
      connectFirestoreEmulator(
        this.db,
        this.config.firestoreEmulator.host,
        this.config.firestoreEmulator.port,
      );
    }
  }

  signIn(): Promise<unknown> {
    return signInWithPopup(this.auth, new GoogleAuthProvider());
  }

  signOut(): Promise<void> {
    return signOut(this.auth);
  }

  onUserChanged(handler: (user: User | null) => void): () => void {
    return onAuthStateChanged(this.auth, handler);
  }
}
