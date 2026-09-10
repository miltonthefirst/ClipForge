import { Injectable, inject } from '@angular/core';
import type {
  Candidate,
  Channel,
  Clip,
  ClipPreview,
  Job,
  Publication,
  PublishOptions,
  ReviewState,
  RightsBasis,
  UserProfile,
  UserRole,
  UserStatus,
} from '@clipforge/contracts';
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

  /** Approved clips, the publish queue's input. */
  watchApproved(
    uid: string,
    onData: (clips: Clip[]) => void,
    onError?: (error: Error) => void,
  ): Unsubscribe {
    return this.watchReviewQueue(uid, onData, 'APPROVED', onError);
  }

  /**
   * Record why this clip may be published.
   *
   * `attestedBy` is the caller's own uid and `attestedAt` is set here rather
   * than accepted from the caller: an attestation whose author or date could be
   * supplied by whoever wrote it would answer neither of the questions the audit
   * log exists to answer. firestore.rules requires both to be present.
   */
  async attest(
    uid: string,
    clipId: string,
    basis: RightsBasis,
    note: string | null,
  ): Promise<void> {
    await updateDoc(doc(this.firebase.db, 'clips', clipId), {
      rights: {
        basis,
        attestedBy: uid,
        attestedAt: new Date().toISOString(),
        note: note?.trim() ? note.trim() : null,
      },
    });
  }

  /**
   * Ask the worker to publish an approved, attested clip.
   *
   * This creates a job, not an upload. The credentials live on the worker
   * (docs/adr/0010-worker-held-publishing-credentials.md), so the phone's role
   * ends at "I want this published, on this basis, at this time".
   *
   * `publishAt` becomes the job's `notBefore`, which is the same field the
   * scheduler already consults before claiming anything — so a scheduled publish
   * needs no second timer anywhere.
   *
   * `options` is what the operator chose for this upload and nothing else. Null
   * fields inside it mean "use the channel's default", which is not the same as
   * an empty one — `tags: []` says "no tags on this one" and `tags: null` says
   * "I did not touch the tags". The worker's resolver honours that distinction
   * (apps/worker/clipforge/publish/metadata.py), so the UI must preserve it.
   */
  async requestPublish(
    uid: string,
    clipId: string,
    publishAt: Date | null = null,
    options: PublishOptions | null = null,
  ): Promise<string> {
    const reference = doc(collection(this.firebase.db, 'jobs'));
    const now = new Date().toISOString();
    await setDoc(reference, {
      id: reference.id,
      uid,
      type: 'PUBLISH',
      status: 'QUEUED',
      submission: null,
      sourceId: null,
      clipId,
      publishOptions: options,
      notBefore: publishAt ? publishAt.toISOString() : null,
      stages: [{ name: 'PUBLISH', lane: 'CPU', status: 'PENDING' }],
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

  /**
   * A clip's publish history — the audit trail, read-only.
   *
   * Answers "who authorised this, on what basis, and what went out?" from the
   * phone rather than from worker logs (Phase 8, exit criterion 5).
   */
  async loadPublications(clipId: string): Promise<Publication[]> {
    const { getDocs } = await import('firebase/firestore');
    const snapshot = await getDocs(collection(this.firebase.db, 'clips', clipId, 'publications'));
    return snapshot.docs.map((d) => d.data() as Publication);
  }

  /**
   * Every publishing channel, for the destination picker.
   *
   * Unbounded and safe to be, for the same reason `watchUsers` is: a channel is
   * a destination somebody set up by hand, and an install with enough of them
   * for this query to cost anything has a different problem. Bounded anyway, so
   * "safe today" does not quietly become "unbounded listen" later.
   */
  watchChannels(
    onData: (channels: Channel[]) => void,
    onError?: (error: Error) => void,
  ): Unsubscribe {
    return onSnapshot(
      query(collection(this.firebase.db, 'channels'), limit(50)),
      (snapshot) => onData(snapshot.docs.map((d) => d.data() as Channel)),
      (error) => onError?.(error),
    );
  }

  /**
   * Follow one publishing channel.
   *
   * Read-only to every client: rules make `channels` worker-written, because the
   * worker is the only thing that can actually reach a channel and therefore the
   * only thing that can honestly report on one.
   */
  watchChannel(
    channelId: string,
    onData: (channel: Channel | null) => void,
    onError?: (error: Error) => void,
  ): Unsubscribe {
    return onSnapshot(
      doc(this.firebase.db, 'channels', channelId),
      (snapshot) => onData(snapshot.exists() ? (snapshot.data() as Channel) : null),
      (error) => onError?.(error),
    );
  }

  /**
   * Every account, for the admin People page.
   *
   * Unbounded on purpose and safe to be: `users` holds one document per person
   * with an account, and a deployment where that is a costly read is a
   * deployment with problems this query is not one of. `list` is admin-only in
   * the rules, so a member's call fails rather than returning a filtered set.
   */
  watchUsers(
    onData: (users: UserProfile[]) => void,
    onError?: (error: Error) => void,
  ): Unsubscribe {
    return onSnapshot(
      query(collection(this.firebase.db, 'users'), orderBy('createdAt', 'desc'), limit(200)),
      (snapshot) => onData(snapshot.docs.map((d) => d.data() as UserProfile)),
      (error) => onError?.(error),
    );
  }

  /**
   * Approve, reject or disable an account, or change its role.
   *
   * `decidedBy` is stamped from the caller's own uid rather than accepted as an
   * argument, and the rules require the two to match — an audit trail whose
   * author could be supplied by whoever wrote it answers nothing.
   */
  async decideUser(
    adminUid: string,
    uid: string,
    changes: { status?: UserStatus; role?: UserRole },
  ): Promise<void> {
    await updateDoc(doc(this.firebase.db, 'users', uid), {
      ...changes,
      decidedAt: new Date().toISOString(),
      decidedBy: adminUid,
    });
  }

  /** The parts of your own profile you own. Rules allow these two and no more. */
  async updateOwnProfile(uid: string, changes: { displayName?: string }): Promise<void> {
    await updateDoc(doc(this.firebase.db, 'users', uid), changes);
  }

  /** Cancel a job. The only job transition the rules let a client drive. */
  async cancel(jobId: string): Promise<void> {
    await updateDoc(doc(this.firebase.db, 'jobs', jobId), {
      status: 'CANCELLED',
      updatedAt: new Date().toISOString(),
    });
  }
}
