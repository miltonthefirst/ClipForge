import { Injectable, inject, InjectionToken, signal } from '@angular/core';
import { initializeApp, type FirebaseApp } from 'firebase/app';
import {
  connectAuthEmulator,
  createUserWithEmailAndPassword,
  getAuth,
  getRedirectResult,
  GoogleAuthProvider,
  onAuthStateChanged,
  sendPasswordResetEmail,
  signInWithEmailAndPassword,
  signInWithPopup,
  signInWithRedirect,
  signOut,
  updateProfile,
  type Auth,
  type User,
} from 'firebase/auth';
import { connectFirestoreEmulator, getFirestore, type Firestore } from 'firebase/firestore';
import { connectStorageEmulator, getStorage, type FirebaseStorage } from 'firebase/storage';

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
  /**
   * Cloud Storage, for playing a clip on a device that is not the worker.
   *
   * Constructed unconditionally even where no bucket is configured: it makes no
   * network call until something asks for an object, and a clip with no
   * `storagePath` never does. See `playback.ts` for the precedence.
   */
  readonly storage: FirebaseStorage;

  /** Why a redirect sign-in failed, for the shell to show. */
  readonly redirectError = signal<string | null>(null);

  constructor() {
    this.app = initializeApp(this.config.firebase);
    this.auth = getAuth(this.app);
    this.db = getFirestore(this.app);
    this.storage = getStorage(this.app);

    if (this.config.useEmulators) {
      connectAuthEmulator(this.auth, this.config.authEmulator, { disableWarnings: true });
      connectFirestoreEmulator(
        this.db,
        this.config.firestoreEmulator.host,
        this.config.firestoreEmulator.port,
      );
      connectStorageEmulator(
        this.storage,
        this.config.storageEmulator.host,
        this.config.storageEmulator.port,
      );
      this.exposeEmulatorSignIn();
    }

    // A redirect sign-in finishes on the *next* page load, so the result has to
    // be collected here. `onAuthStateChanged` would report the success on its
    // own; what this adds is the failure, which otherwise vanishes and leaves a
    // sign-in button that looks like it did nothing.
    void getRedirectResult(this.auth).catch((error: unknown) => {
      this.redirectError.set(error instanceof Error ? error.message : String(error));
    });
  }

  /**
   * An emulator-only sign-in hook, for end-to-end tests.
   *
   * Driving the real popup would test Google's OAuth flow, which is not ours and
   * does not run against the emulator anyway; injecting a session into storage
   * does not work either, because Firebase persists to IndexedDB. So the tests
   * sign in with a password against the emulator, which needs an entry point.
   *
   * Guarded by `useEmulators`, so it is absent whenever the app points at a real
   * project — the property that makes this acceptable rather than a back door.
   */
  private exposeEmulatorSignIn(): void {
    if (typeof window === 'undefined') return;
    (
      window as unknown as {
        __clipforgeSignIn?: (email: string, password: string) => Promise<unknown>;
      }
    ).__clipforgeSignIn = (email, password) =>
      signInWithEmailAndPassword(this.auth, email, password);
  }

  /**
   * Sign in with Google, by popup where that works and by redirect where it
   * does not.
   *
   * The desktop shell is the case that forced this. Tauri's WebView2 blocks
   * `window.open` outright — it returns null rather than a window — so Firebase
   * raises `auth/popup-blocked` and the button appears to do nothing at all.
   * Ordinary browser popup blockers produce exactly the same error, so the
   * fallback earns its place in both.
   *
   * Redirect is not simply used everywhere because it costs a full page load
   * and, on the web, the popup keeps the queue on screen behind it.
   */
  async signIn(): Promise<unknown> {
    const provider = new GoogleAuthProvider();

    if (this.config.authFlow === 'redirect') {
      return signInWithRedirect(this.auth, provider);
    }

    try {
      return await signInWithPopup(this.auth, provider);
    } catch (error) {
      if (!isPopupUnavailable(error)) throw error;
      return signInWithRedirect(this.auth, provider);
    }
  }

  /**
   * Sign in with an email and password.
   *
   * The path that matters on this machine. Google holds a sign-in on a fresh
   * device for verification — up to 48 hours — which is tolerable on a phone
   * and useless on the desktop shell sitting next to the worker.
   */
  signInWithPassword(email: string, password: string): Promise<unknown> {
    return signInWithEmailAndPassword(this.auth, email, password);
  }

  /**
   * Register a new account.
   *
   * Creating the Auth user grants nothing by itself: the profile written
   * alongside it is PENDING, and security rules refuse every other collection
   * until an admin approves it. Registration being open is deliberate; access
   * being open is not.
   */
  async register(email: string, password: string, displayName: string): Promise<User> {
    const created = await createUserWithEmailAndPassword(this.auth, email, password);
    if (displayName.trim()) {
      await updateProfile(created.user, { displayName: displayName.trim() });
    }
    return created.user;
  }

  resetPassword(email: string): Promise<void> {
    return sendPasswordResetEmail(this.auth, email);
  }

  signOut(): Promise<void> {
    return signOut(this.auth);
  }

  onUserChanged(handler: (user: User | null) => void): () => void {
    return onAuthStateChanged(this.auth, handler);
  }
}

/**
 * Whether the failure means "no popup available" rather than "sign-in refused".
 *
 * Only these three are worth retrying by redirect. Retrying a genuine refusal —
 * a closed popup, a cancelled request — would drag the user into a full page
 * navigation they just declined.
 */
function isPopupUnavailable(error: unknown): boolean {
  const code = (error as { code?: string } | null)?.code;
  return (
    code === 'auth/popup-blocked' ||
    code === 'auth/operation-not-supported-in-this-environment' ||
    code === 'auth/web-storage-unsupported'
  );
}
