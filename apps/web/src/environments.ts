/**
 * Runtime configuration for the PWA.
 *
 * Firebase identifiers are configuration, never source — the same rule the
 * worker follows, and what made moving Firebase projects a one-line change
 * during Phase 1 (docs/adr/0004-dedicated-firebase-project.md).
 *
 * Angular has no `process.env`, so these are read from `window.__clipforge`,
 * which `index.html` populates. That keeps the built bundle project-agnostic:
 * the same `dist/` can point at the emulator or at a real project without a
 * rebuild.
 */

export interface ClipForgeConfig {
  readonly firebase: {
    readonly projectId: string;
    readonly apiKey: string;
    readonly authDomain: string;
    readonly appId: string;
  };
  /** When true, connect to the local Emulator Suite instead of a real project. */
  readonly useEmulators: boolean;
  readonly firestoreEmulator: { readonly host: string; readonly port: number };
  readonly authEmulator: string;
  /**
   * Where the worker serves its workspace. Playback branch 2: a clip with no
   * `playbackUrl` can still be played when the PWA is open on the worker
   * machine. See docs/adr/0009-spark-tier-local-artefacts.md.
   */
  readonly localServerOrigin: string;
  /**
   * Force the service worker on or off. Left undefined it follows
   * `useEmulators`, which is right for the browser.
   *
   * The desktop shell sets it to `false` explicitly: a worker there would cache
   * the app shell from Tauri's custom protocol and serve it back after a
   * rebuild, turning "I just rebuilt" into "why is it still the old one".
   */
  readonly serviceWorker?: boolean;
  /**
   * How Google sign-in is presented.
   *
   * `popup` everywhere a popup can open, which is every ordinary browser.
   * `redirect` in the desktop shell, where Tauri's WebView2 blocks
   * `window.open` outright — it returns null rather than a window, so Firebase
   * raises `auth/popup-blocked` and the button looks broken. Left unset the app
   * tries a popup and falls back on its own, so this is an optimisation rather
   * than a requirement.
   */
  readonly authFlow?: 'popup' | 'redirect';
}

const DEFAULTS: ClipForgeConfig = {
  firebase: {
    // `demo-` prefixed ids put the Firebase SDKs in offline emulator mode, which
    // is the right default: routine development must not touch a real project.
    projectId: 'demo-clipforge',
    apiKey: 'demo-key',
    authDomain: 'demo-clipforge.firebaseapp.com',
    appId: 'demo-app',
  },
  useEmulators: true,
  firestoreEmulator: { host: '127.0.0.1', port: 8080 },
  authEmulator: 'http://127.0.0.1:9099',
  localServerOrigin: 'http://127.0.0.1:8765',
};

declare global {
  interface Window {
    __clipforge?: Partial<ClipForgeConfig>;
  }
}

export function loadConfig(): ClipForgeConfig {
  const injected = typeof window !== 'undefined' ? window.__clipforge : undefined;
  return { ...DEFAULTS, ...(injected ?? {}) };
}
