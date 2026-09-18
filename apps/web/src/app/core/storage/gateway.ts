import { Injectable, inject } from '@angular/core';

import { FirebaseService } from '../firebase';

/**
 * The one door to Cloud Storage.
 *
 * Its own gate rather than a wing of the Firestore one, because they are not
 * the same service and the only thing they share is a vendor. A document read
 * is billed per document, cached by a live listener and reconciled from
 * Timestamps; an object read is billed per byte, resolved through a URL that
 * expires, and decided by a different rules file at fetch time. One class
 * holding both would be a class with two reasons to change and a name that
 * could only be `Firebase`.
 *
 * ## Why every call is lazy
 *
 * The SDK is loaded on first use, not at startup. Most sessions never touch an
 * object at all — on the free tier nothing is uploaded, and a review done on
 * the machine that holds the files plays them from the local server — so
 * importing it eagerly would cost every visitor bytes for a code path they will
 * not reach. The dynamic import is inherited from the code this replaces and is
 * the reason it was written that way.
 *
 * ## Why failures are returned rather than thrown
 *
 * Because both callers need to tell two failures apart and neither should have
 * to know Firebase's error codes to do it. `remove` answers "may the document
 * stop pointing at this object", where an object already collected is a yes.
 * `downloadUrl` answers with a reason, because a refusal and an absence look
 * identical to a viewer and are not the same thing at all — that distinction
 * cost a release once, when a missing IAM grant denied every bucket read in the
 * project and each one arrived looking like a clip that had never been
 * uploaded.
 */

/** What a failed URL resolution was: forbidden, gone, or something else. */
export type StorageRefusal = 'denied' | 'missing' | 'unknown';

export interface ResolvedUrl {
  readonly url: string | null;
  readonly refusal: StorageRefusal | null;
  /**
   * The raw `storage/…` code, kept rather than flattened into `refusal`.
   *
   * The caller tells a reviewer what to do about it, and the advice differs
   * per code in ways three buckets cannot carry: an over-quota project, a
   * download that failed and is worth retrying, and a rules refusal that needs
   * an IAM grant are all `unknown` here and are three different sentences
   * there. A gate that decides how much of an error its callers may see is a
   * gate that has started making their decisions.
   */
  readonly code: string | null;
}

@Injectable({ providedIn: 'root' })
export class StorageGateway {
  private readonly firebase = inject(FirebaseService);

  /**
   * A URL for one object, or why there is not one.
   *
   * Resolved on demand rather than stored on the document. `getDownloadURL`
   * runs storage.rules on the request, so access is decided when the video is
   * fetched — a URL written into Firestore would be a bearer token in a
   * database row, valid to anyone who ever read it.
   */
  async downloadUrl(path: string): Promise<ResolvedUrl> {
    const { getDownloadURL, ref } = await import('firebase/storage');
    try {
      const url = await getDownloadURL(ref(this.firebase.storage, path));
      return { url, refusal: null, code: null };
    } catch (error) {
      return { url: null, refusal: classify(error), code: codeOf(error) };
    }
  }

  /**
   * Delete one object, and say whether the pointer to it may now go.
   *
   * True when the object is gone, which includes it having been gone already —
   * a lifecycle rule collecting it first is the common case and not a failure.
   * False means the bytes are still there and the document must keep pointing
   * at them, because the alternative is a record claiming there is no copy
   * while the copy sits in the bucket.
   */
  async remove(path: string): Promise<boolean> {
    const { deleteObject, ref } = await import('firebase/storage');
    try {
      await deleteObject(ref(this.firebase.storage, path));
      return true;
    } catch (error) {
      return classify(error) === 'missing';
    }
  }
}

function codeOf(error: unknown): string {
  const code = (error as { code?: unknown } | null)?.code;
  return typeof code === 'string' ? code : 'storage/unknown';
}

function classify(error: unknown): StorageRefusal {
  const code = codeOf(error);
  // Already collected is the common case and not a failure: the lifecycle rule
  // removes an object after the retention window and reports to nobody, so a
  // document should stop pointing at it either way.
  if (code === 'storage/object-not-found') return 'missing';
  if (code === 'storage/unauthorized' || code === 'storage/unauthenticated') return 'denied';
  return 'unknown';
}
