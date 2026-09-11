import { Injectable, inject } from '@angular/core';
import type { Clip } from '@clipforge/contracts';
import { getDownloadURL, ref } from 'firebase/storage';

import { CLIPFORGE_CONFIG, FirebaseService } from './firebase';

/**
 * How the worker's file server identifies itself. Mirrors
 * `clipforge/localserver.py` — the two must agree, and there is a test on each
 * side that says so.
 */
const IDENTITY_PATH = '/__clipforge';
/**
 * How long an answer about the file server stays believable.
 *
 * Short, because starting and stopping the worker is a thing people do from
 * inside this app; long enough that resolving a queue of clips costs one probe
 * rather than one each.
 */
const PROBE_TTL_MS = 5_000;
const IDENTITY_SERVICE = 'clipforge-local-file-server';

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
  private probedAt = 0;

  /**
   * Probe the worker's file server once per session.
   *
   * Asks *who is there*, not merely whether anything is. The previous version
   * did a `no-cors` HEAD against `/` and treated any answer — including an
   * opaque one — as proof: it could not distinguish ClipForge's file server
   * from any other process holding the port. When an unrelated app took 8765,
   * every clip pointed at a stranger's 404, the video element sat there showing
   * its poster, and nothing anywhere said why.
   *
   * So the identity path is fetched and the response read. `no-cors` is gone
   * with it — the file server sends `Access-Control-Allow-Origin: *`, so a
   * readable answer was always available and only the question was wrong.
   *
   * Cached because it is asked per clip and cannot change while the page is
   * open in a way the user would notice.
   */
  async probeLocalServer(fetchImpl: typeof fetch = fetch): Promise<boolean> {
    // Cached for a few seconds, not for the life of the page. The old cache
    // was permanent, which made "no worker was running when you opened the
    // app" a verdict rather than an observation: start one from the Worker
    // page and every clip went on showing a poster until the whole app was
    // reloaded, with nothing saying why. The worker is a process someone starts
    // and stops, so its absence has to be a fact with an expiry date.
    if (this.localReachable !== null && Date.now() - this.probedAt < PROBE_TTL_MS) {
      return this.localReachable;
    }
    try {
      const response = await fetchImpl(`${this.config.localServerOrigin}${IDENTITY_PATH}`, {
        signal: AbortSignal.timeout(1500),
      });
      const body = (await response.json()) as { service?: string };
      this.localReachable = response.ok && body.service === IDENTITY_SERVICE;
      this.probedAt = Date.now();
    } catch {
      // Not listening, not ClipForge, or not answering JSON. All the same
      // answer: do not build local URLs.
      this.localReachable = false;
      this.probedAt = Date.now();
    }
    return this.localReachable;
  }

  /**
   * Forget what we knew about the file server.
   *
   * Called when the worker is started or stopped from inside the app, so the
   * next clip resolved reflects what the operator just did rather than what was
   * true a moment before they did it.
   */
  invalidateLocalProbe(): void {
    this.localReachable = null;
    this.probedAt = 0;
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
