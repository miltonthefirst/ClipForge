import { Injectable, inject } from '@angular/core';
import type { Clip } from '@clipforge/contracts';
import { getDownloadURL, ref } from 'firebase/storage';

import { CLIPFORGE_CONFIG, FirebaseService } from './firebase';

/** Where a clip can be played from, and why. */
export type PlaybackSource =
  | { readonly kind: 'remote'; readonly url: string }
  | { readonly kind: 'local'; readonly url: string }
  | { readonly kind: 'bucket'; readonly url: string }
  | { readonly kind: 'poster' };

/**
 * Resolves where a clip can actually be played from.
 *
 * One documented precedence, answering one question — "what can THIS browser
 * play?" — so the review UI has no branching of its own:
 *
 *   1. `playbackUrl`, if set. A URL any browser can fetch, from wherever it
 *      came: a tunnel, a CDN. The field is "a URL", not "a Firebase thing".
 *   2. The worker's local file server, if reachable. Preferred over the bucket
 *      on purpose — on the machine that rendered the clip it is faster, and it
 *      costs no egress, which the bucket does on every play.
 *   3. The bucket copy, if one exists and has not expired. This is what a phone
 *      gets, and the reason the bucket exists at all.
 *   4. Poster frame only.
 *
 * ## Why expiry is read rather than discovered
 *
 * Bucket copies are deleted by a Cloud Storage lifecycle rule, which tells
 * nobody. Asking for a download URL for a collected object fails at the moment
 * someone presses play — the worst possible time to find out. `playbackExpiresAt`
 * is stamped at upload, so the deadline is known before the attempt, and an
 * expired clip falls through to the poster the same way one that was never
 * uploaded does. The failure is still caught, because a lifecycle rule and a
 * clock will not agree to the second.
 */
@Injectable({ providedIn: 'root' })
export class PlaybackService {
  private readonly config = inject(CLIPFORGE_CONFIG);
  private readonly firebase = inject(FirebaseService);
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

  /** Whether the bucket still holds a playable copy of this clip. */
  bucketCopyLive(clip: Clip, now: number = Date.now()): boolean {
    if (!clip.storagePath) return false;
    if (!clip.playbackExpiresAt) return true;
    return Date.parse(clip.playbackExpiresAt) > now;
  }

  /** Resolve one clip against the precedence above. */
  async resolve(clip: Clip, localAvailable: boolean): Promise<PlaybackSource> {
    if (clip.playbackUrl) {
      return { kind: 'remote', url: clip.playbackUrl };
    }

    if (localAvailable) {
      const key = this.workspaceKey(clip);
      if (key) {
        return { kind: 'local', url: `${this.config.localServerOrigin}/${key}` };
      }
    }

    if (this.bucketCopyLive(clip)) {
      try {
        // Resolved here rather than stored on the document. `getDownloadURL`
        // runs storage.rules on the request, so access is decided when the
        // video is fetched; a URL written into Firestore would be a bearer
        // token in a database row, valid to anyone who ever read it.
        const url = await getDownloadURL(ref(this.firebase.storage, clip.storagePath!));
        return { kind: 'bucket', url };
      } catch {
        // Collected early, or never uploaded. The poster is still true.
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
