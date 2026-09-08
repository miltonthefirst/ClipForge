import { Injectable, inject } from '@angular/core';
import type { Candidate, Clip, ClipPreview, Job, ReviewState } from '@clipforge/contracts';
import {
  collection,
  doc,
  limit,
  onSnapshot,
  orderBy,
  query,
  setDoc,
  updateDoc,
  where,
  type Unsubscribe,
} from 'firebase/firestore';

import { FirebaseService } from './firebase';

/**
 * Firestore reads and writes, as signals.
 *
 * Every listener is **narrowly scoped and bounded**. That is a cost decision as
 * much as a correctness one: Firestore bills a read per delivered document, and
 * a listener re-delivers its whole result set on reconnect. An unfiltered listen
 * over `jobs` is the single easiest way to burn the free daily quota
 * (docs/adr/0009-spark-tier-local-artefacts.md), and the security rules would
 * reject it anyway.
 */
@Injectable({ providedIn: 'root' })
export class ClipForgeStore {
  private readonly firebase = inject(FirebaseService);

  /**
   * Live jobs for one user, newest first.
   *
   * Delivers through a callback rather than returning a signal. Returning one
   * would push the caller into creating an `effect` to read it — and an effect
   * created inside another effect is not valid in Angular, which is exactly the
   * shape a re-subscribing watcher wants to take.
   */
  watchJobs(
    uid: string,
    onData: (jobs: Job[]) => void,
    onError?: (error: Error) => void,
  ): Unsubscribe {
    return onSnapshot(
      query(
        collection(this.firebase.db, 'jobs'),
        where('uid', '==', uid),
        orderBy('createdAt', 'desc'),
        limit(25),
      ),
      (snapshot) => onData(snapshot.docs.map((d) => d.data() as Job)),
      (error) => onError?.(error),
    );
  }

  /** The review queue: this user's clips awaiting a decision. */
  watchReviewQueue(
    uid: string,
    onData: (clips: Clip[]) => void,
    review: ReviewState = 'PENDING',
    onError?: (error: Error) => void,
  ): Unsubscribe {
    return onSnapshot(
      query(
        collection(this.firebase.db, 'clips'),
        where('uid', '==', uid),
        where('review', '==', review),
        orderBy('createdAt', 'desc'),
        limit(50),
      ),
      (snapshot) => onData(snapshot.docs.map((d) => d.data() as Clip)),
      (error) => onError?.(error),
    );
  }

  /**
   * Fetch a clip's poster on demand.
   *
   * A subcollection read rather than a field on the clip, so the review-queue
   * listener does not drag ~50 KB of base64 per card on every reconnect. On the
   * free tier the poster is most of what a phone review has to go on, so it has
   * to load — but it does not have to load *with the list*.
   */
  async loadPreview(clipId: string): Promise<ClipPreview | null> {
    const { getDoc } = await import('firebase/firestore');
    const snapshot = await getDoc(doc(this.firebase.db, 'clips', clipId, 'preview', 'poster'));
    return snapshot.exists() ? (snapshot.data() as ClipPreview) : null;
  }

  /**
   * The candidate a clip came from — its score breakdown, hook and reason.
   *
   * On the free tier this is most of what a phone review has to go on, since
   * the video itself cannot be played remotely.
   */
  async loadCandidate(candidateId: string): Promise<Candidate | null> {
    const { getDoc } = await import('firebase/firestore');
    const snapshot = await getDoc(doc(this.firebase.db, 'candidates', candidateId));
    return snapshot.exists() ? (snapshot.data() as Candidate) : null;
  }

  /**
   * Enqueue a job.
   *
   * The shape is constrained by firestore.rules: a client may create only a
   * QUEUED job, owned by itself, with no worker and no lease. Anything else is
   * rejected — the PWA may create work, never pipeline state.
   */
  async submit(uid: string, submission: string): Promise<string> {
    const reference = doc(collection(this.firebase.db, 'jobs'));
    const now = new Date().toISOString();
    await setDoc(reference, {
      id: reference.id,
      uid,
      type: 'CLIP',
      status: 'QUEUED',
      submission,
      sourceId: null,
      stages: [
        { name: 'DOWNLOAD', lane: 'CPU', status: 'PENDING' },
        { name: 'TRANSCRIBE', lane: 'GPU', status: 'PENDING' },
        { name: 'ANALYZE', lane: 'GPU', status: 'PENDING' },
        { name: 'RENDER', lane: 'CPU', status: 'PENDING' },
      ],
      workerId: null,
      leaseExpiresAt: null,
      attempts: 0,
      maxAttempts: 3,
      error: null,
      createdAt: now,
      updatedAt: now,
      startedAt: null,
      endedAt: null,
    });
    return reference.id;
  }

  /** Approve or reject a clip. The one state transition the user owns. */
  async review(clipId: string, review: ReviewState): Promise<void> {
    await updateDoc(doc(this.firebase.db, 'clips', clipId), {
      review,
      reviewedAt: new Date().toISOString(),
    });
  }

  /** Cancel a job. The only job transition the rules let a client drive. */
  async cancel(jobId: string): Promise<void> {
    await updateDoc(doc(this.firebase.db, 'jobs', jobId), {
      status: 'CANCELLED',
      updatedAt: new Date().toISOString(),
    });
  }
}
