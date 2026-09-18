import { Injectable, inject } from '@angular/core';
import type { Clip } from '@clipforge/contracts';

import { CLIPFORGE_CONFIG, FirebaseService } from './firebase';
import { StorageGateway } from './storage/gateway';

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
  /**
   * There is a bucket copy, and this device was refused it.
   *
   * Split out from `poster` because the two look identical on screen and call
   * for opposite actions. "No cloud copy" is answered by asking the worker to
   * upload one. "A cloud copy exists and Storage said no" is answered by fixing
   * access — and asking for another upload is precisely useless, because the
   * worker will find the clip already there and skip. That loop is what a
   * reviewer hit: press the button, watch the job complete, see no change,
   * conclude the upload never happened.
   */
  | { readonly kind: 'blocked'; readonly reason: string }
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
  private readonly bucket = inject(StorageGateway);
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

  /**
   * Whether the bucket still holds a playable copy of this clip.
   *
   * An expiry that will not parse is treated as *live*, which is the opposite
   * of what this used to do and the reason it is spelled out rather than left
   * to `Date.parse`. `storagePath` is the fact — something was uploaded — and
   * `playbackExpiresAt` is only a hint about when it stops being true. Letting
   * a bad hint override a good fact is what turned a date-format mismatch into
   * "every uploaded clip reports that it was never uploaded": `Date.parse` of a
   * Firestore Timestamp returns NaN, `NaN > now` is false, and the clip fell
   * through to the poster with an upload button beside it. See
   * `core/documents.ts` for the mismatch itself, now fixed at the read
   * boundary; this stays because the safe direction is worth stating.
   *
   * Being wrong this way costs one refused fetch, which `resolve` already
   * handles. Being wrong the other way hides a clip that is sitting right there.
   */
  bucketCopyLive(clip: Clip, now: number = Date.now()): boolean {
    if (!clip.storagePath) return false;
    if (!clip.playbackExpiresAt) return true;
    const expires = Date.parse(clip.playbackExpiresAt);
    return Number.isNaN(expires) || expires > now;
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
      // The gate resolves on demand rather than storing a URL on the
      // document: `getDownloadURL` runs storage.rules on the request, so
      // access is decided when the video is fetched, and a URL written into
      // Firestore would be a bearer token in a database row valid to anyone
      // who ever read it.
      //
      // A refusal is answered, not thrown, and that distinction cost a
      // release. This used to be a bare `catch {}` falling through to the
      // poster, on the reasoning that a collected object and an absent one
      // look the same to a viewer. They do — and that is exactly why the
      // difference has to survive: every bucket read in the project was once
      // denied, because the rules ask Firestore whether the viewer is
      // approved and that cross-service call needs an IAM grant the deploy
      // never made. Every uploaded clip showed a poster and an upload button,
      // and every upload the button asked for was skipped as already done.
      const resolved = await this.bucket.downloadUrl(clip.storagePath!);
      if (resolved.url) return { kind: 'bucket', url: resolved.url };

      // Collected early by the lifecycle rule. Nothing is wrong, and the
      // poster is the honest answer.
      if (resolved.refusal === 'missing') return { kind: 'poster' };

      return {
        kind: 'blocked',
        reason: describeStorageFailure(resolved.code ?? 'storage/unknown'),
      };
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

/**
 * What to tell someone who cannot play a clip that is definitely in the bucket.
 *
 * Written for the person holding the phone, not for a log: each of these has a
 * different fix and only one of them is "try again". The unauthorized case is
 * spelled out at length because it is the one that has actually happened, and
 * because nothing about it is discoverable from the app — the clip is there,
 * the account is approved, and Storage still says no.
 */
function describeStorageFailure(code: string): string {
  switch (code) {
    case 'storage/unauthorized':
    case 'storage/unauthenticated':
      return (
        'This clip is in the cloud, but Cloud Storage refused this device access to it. ' +
        'That is a project setting rather than anything about this clip: storage.rules ' +
        'asks Firestore whether the signed-in account is approved, and that cross-service ' +
        'lookup needs an IAM grant that deploying the rules does not make. ' +
        'Run tools/deploy.ps1 -GrantStorageRulesAccess, or grant ' +
        'roles/firebaserules.firestoreServiceAgent to the Cloud Storage service agent. ' +
        'Asking the worker to upload it again will not help — the clip is already there.'
      );
    case 'storage/retry-limit-exceeded':
    case 'storage/server-file-wrong-size':
      return 'The cloud copy would not download. Worth another try in a moment.';
    case 'storage/quota-exceeded':
      return 'The project is over its Cloud Storage quota, so the clip cannot be fetched.';
    default:
      return `The cloud copy could not be fetched (${code}).`;
  }
}
