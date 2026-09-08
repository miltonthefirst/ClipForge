import { Injectable, inject } from '@angular/core';
import type { Clip } from '@clipforge/contracts';

import { CLIPFORGE_CONFIG } from './firebase';

/** Where a clip can be played from, and why. */
export type PlaybackSource =
  | { readonly kind: 'remote'; readonly url: string }
  | { readonly kind: 'local'; readonly url: string }
  | { readonly kind: 'poster' };

/**
 * Resolves where a clip can actually be played from.
 *
 * One documented precedence, answering one question — "what can THIS browser
 * play?" — so the review UI has no branching of its own:
 *
 *   1. `playbackUrl`, if set. Works from anywhere. Populated by Cloud Storage
 *      when Blaze is enabled, and equally by a tunnel: the field is "a URL a
 *      browser can fetch", not "a Firebase thing".
 *   2. The worker's local file server, if reachable. Full video review when the
 *      PWA is opened on the machine. Browsers exempt `localhost` from
 *      mixed-content blocking, so this works even over HTTPS.
 *   3. Poster frame only. What a phone gets today.
 *
 * Enabling Blaze later lights up branch 1 and changes nothing else.
 * See docs/adr/0009-spark-tier-local-artefacts.md.
 */
@Injectable({ providedIn: 'root' })
export class PlaybackService {
  private readonly config = inject(CLIPFORGE_CONFIG);
  private localReachable: boolean | null = null;

  /**
   * Probe the worker's file server once per session.
   *
   * Cached because it is asked per clip and the answer cannot change while the
   * page is open in any way the user would notice — and a failed fetch per card
   * would be both slow and noisy in the console.
   */
  async probeLocalServer(fetchImpl: typeof fetch = fetch): Promise<boolean> {
    if (this.localReachable !== null) return this.localReachable;
    try {
      // A HEAD against a path that will 404 is enough: we are testing whether
      // anything is listening, not whether a particular file exists.
      await fetchImpl(`${this.config.localServerOrigin}/`, {
        method: 'HEAD',
        mode: 'no-cors',
        signal: AbortSignal.timeout(1500),
      });
      this.localReachable = true;
    } catch {
      this.localReachable = false;
    }
    return this.localReachable;
  }

  /** Resolve one clip against the precedence above. */
  resolve(clip: Clip, localAvailable: boolean): PlaybackSource {
    if (clip.playbackUrl) {
      return { kind: 'remote', url: clip.playbackUrl };
    }
    if (localAvailable) {
      const key = this.workspaceKey(clip);
      if (key) {
        return { kind: 'local', url: `${this.config.localServerOrigin}/${key}` };
      }
    }
    return { kind: 'poster' };
  }

  /**
   * Turn an absolute worker path into a path the file server will serve.
   *
   * The server is rooted at the workspace, so what it wants is the part of the
   * path from `clips/` onwards. Done by string rather than by storing the key
   * separately because `localPath` is what the worker already records, and a
   * second field would be one more thing to keep in step.
   */
  private workspaceKey(clip: Clip): string | null {
    // Windows paths come back with backslashes; the file server speaks URLs.
    const normalised = clip.localPath.split('\\').join('/');
    const index = normalised.lastIndexOf('/clips/');
    if (index === -1) return null;
    return normalised.slice(index + 1);
  }
}
