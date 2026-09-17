import { Injectable, inject } from '@angular/core';
import type {
  AgentDesired,
  AgentReport,
  CalibrationReport,
  Candidate,
  Channel,
  Clip,
  ClipPreview,
  Job,
  JobEvent,
  JobStatus,
  MetricSnapshot,
  MusicOptions,
  ObscureOptions,
  Publication,
  Preference,
  PreferenceStatus,
  PublishOptions,
  RemakeOptions,
  ReviewState,
  Source,
  UserProfile,
  UserRole,
  UserStatus,
  WorkerHeartbeat,
} from '@clipforge/contracts';
import {
  collection,
  doc,
  getDoc,
  limit,
  onSnapshot,
  orderBy,
  query,
  setDoc,
  updateDoc,
  where,
  type Unsubscribe,
} from 'firebase/firestore';

import { fromDocument } from './documents';
import { FirebaseService } from './firebase';

// Firestore's own ceiling on a batched write. Paging at this size means one
// enormous source needs no different code path from a small one — it just takes
// more trips.
const BATCH_LIMIT = 500;

/**
 * How many jobs one queue listener delivers.
 *
 * Exported because the page has to say when it is showing fewer jobs than exist,
 * and it can only know that by comparing what arrived against this.
 */
export const JOB_PAGE = 25;

/**
 * Firestore reads and writes, as signals.
 *
 * Every listener is **bounded**, and that is a cost decision: Firestore bills a
 * read per delivered document, and a listener re-delivers its whole result set
 * on reconnect. An unbounded listen over `jobs` is the single easiest way to
 * burn the free daily quota (docs/adr/0009-spark-tier-local-artefacts.md).
 *
 * They are no longer *scoped by uid*. ClipForge is one shared workspace, so the
 * limit is what keeps a listener cheap — not a filter that also happened to
 * hide half the system from the person looking at it.
 */
@Injectable({ providedIn: 'root' })
export class ClipForgeStore {
  private readonly firebase = inject(FirebaseService);

  /**
   * The queue, newest first — everyone's, because there is only one.
   *
   * Not filtered by uid. ClipForge is a single shared workspace, so a job
   * submitted from a phone belongs in the list shown on the desktop beside it.
   * Filtering here was what made one system look like two: the rules would now
   * allow the read, but a query that asks only for its own rows gets only its
   * own rows regardless of what it is permitted to see.
   *
   * Delivers through a callback rather than returning a signal. Returning one
   * would push the caller into creating an `effect` to read it — and an effect
   * created inside another effect is not valid in Angular, which is exactly the
   * shape a re-subscribing watcher wants to take.
   *
   * `statuses` narrows the query rather than the delivered result, and the
   * difference is the point. The bound below is a *window on the newest*, not a
   * sample of each status, so once the queue outgrows it a tab built by
   * filtering this listener's output shows whatever happens to have survived
   * into the window and looks complete — it has no way to know what fell off
   * the end. (Measured on the live project in September 2026, the newest 25
   * jobs held about two thirds of the COMPLETED ones.)
   *
   * Several statuses go in one `in` query rather than one listener each, the
   * same way {@link countJobs} asks for a tab's total. A merged pair would have
   * two bounds where the page needs one: "the newest 25" is a single answer,
   * and "the newest 25 of each, re-sorted" costs twice the reads to arrive at
   * it and leaves the page unable to say plainly how much it is not showing.
   *
   * Filtering by status needs a composite index — `jobs [status ASC, createdAt
   * DESC]`, in firebase/firestore.indexes.json. The existing ASC pair does not
   * serve it: verified against the project, which answered FAILED_PRECONDITION
   * and named that exact index.
   */
  watchJobs(
    onData: (jobs: Job[]) => void,
    statuses: readonly JobStatus[] | null = null,
    onError?: (error: Error) => void,
  ): Unsubscribe {
    const jobs = collection(this.firebase.db, 'jobs');
    const newest = [orderBy('createdAt', 'desc'), limit(JOB_PAGE)] as const;
    return onSnapshot(
      statuses === null
        ? query(jobs, ...newest)
        : query(jobs, where('status', 'in', [...statuses]), ...newest),
      (snapshot) => onData(snapshot.docs.map((d) => fromDocument<Job>(d.data()))),
      (error) => onError?.(error),
    );
  }

  /**
   * How many jobs are in one slice, without reading them.
   *
   * An aggregation rather than a longer listen: the tab labels have to be true
   * about jobs the bounded listener never delivers, and counting them by
   * fetching them would cost a read each for a number. It needs no composite
   * index — there is no ordering to serve — so the counts are right even while
   * the index the lists depend on is still being built.
   */
  async countJobs(statuses: readonly JobStatus[] | null): Promise<number> {
    const { getCountFromServer } = await import('firebase/firestore');
    const jobs = collection(this.firebase.db, 'jobs');
    const snapshot = await getCountFromServer(
      statuses === null ? query(jobs) : query(jobs, where('status', 'in', [...statuses])),
    );
    return snapshot.data().count;
  }

  /**
   * One job, live.
   *
   * A separate listener from {@link watchJobs} rather than a lookup into its
   * result, because the detail page has to work when it is opened directly — a
   * link from a phone notification, a bookmark, a reload — and because the list
   * is capped at 25, so an older job is not in it at all.
   */
  watchJob(
    jobId: string,
    onData: (job: Job | null) => void,
    onError?: (error: Error) => void,
  ): Unsubscribe {
    return onSnapshot(
      doc(this.firebase.db, 'jobs', jobId),
      (snapshot) => onData(snapshot.exists() ? fromDocument<Job>(snapshot.data()) : null),
      (error) => onError?.(error),
    );
  }

  /**
   * A job's event log, in the order things actually happened.
   *
   * Ordered by `(at, seq)`, not by `at` alone — the same ordering the worker
   * reads it back with. One transition can emit several events at the identical
   * instant (a reap emits LEASE_EXPIRED and REQUEUED together), and ordering by
   * timestamp alone leaves Firestore breaking the tie on a random document id,
   * so the log would read in a different order on different loads.
   *
   * This is the only place a stalled job explains itself: the stage list says
   * *where* it stopped, and the log says *what happened* — reclaimed after a
   * lease expiry, retried, cancelled.
   */
  watchJobEvents(
    jobId: string,
    onData: (events: JobEvent[]) => void,
    onError?: (error: Error) => void,
  ): Unsubscribe {
    return onSnapshot(
      query(
        collection(this.firebase.db, 'jobs', jobId, 'events'),
        orderBy('at', 'asc'),
        orderBy('seq', 'asc'),
        limit(200),
      ),
      (snapshot) => onData(snapshot.docs.map((d) => fromDocument<JobEvent>(d.data()))),
      (error) => onError?.(error),
    );
  }

  /** What a job's submission resolved to, once ingestion has worked it out. */
  async loadSource(sourceId: string): Promise<Source | null> {
    const { getDoc } = await import('firebase/firestore');
    const snapshot = await getDoc(doc(this.firebase.db, 'sources', sourceId));
    return snapshot.exists() ? fromDocument<Source>(snapshot.data()) : null;
  }

  /**
   * What one job actually produced.
   *
   * The question a COMPLETED job cannot answer about itself. Every stage can
   * run to DONE and the job still yield nothing — a video with no speech in it
   * transcribes to zero words, so the model is asked to judge nothing, proposes
   * nothing, and RENDER has nothing to do. That job is not failed and it is not
   * broken; it is finished and empty, and saying so is the difference between
   * "it worked" and "why is the review queue still empty?".
   *
   * Keyed on `jobId` alone. It used to filter on `uid` too, because the rules
   * only permitted a list that proved it returned the caller's own documents;
   * in a shared workspace that requirement is gone, and so is the composite
   * index it needed.
   *
   * The candidates side is counted rather than fetched — an aggregation costs a
   * fraction of a read per document instead of one each, and the number is all
   * this page shows.
   */
  async loadJobResults(jobId: string): Promise<{ candidates: number; clips: Clip[] }> {
    const { getCountFromServer, getDocs } = await import('firebase/firestore');

    const ofJob = [where('jobId', '==', jobId)];

    const [candidates, clips] = await Promise.all([
      getCountFromServer(query(collection(this.firebase.db, 'candidates'), ...ofJob)),
      getDocs(query(collection(this.firebase.db, 'clips'), ...ofJob, limit(20))),
    ]);

    return {
      candidates: candidates.data().count,
      clips: clips.docs.map((d) => fromDocument<Clip>(d.data())),
    };
  }

  /**
   * Worker heartbeats — who is serving the queue, and what they can do.
   *
   * Bounded at ten: this deployment has one worker, and an unbounded listen is
   * how the free tier's daily read allowance gets spent on nothing.
   *
   * What this answers that nothing else can: a job sitting at QUEUED is either
   * "the worker is busy with something else" or "there is no worker", and those
   * look identical from the job document alone.
   */
  watchWorkers(
    onData: (workers: WorkerHeartbeat[]) => void,
    onError?: (error: Error) => void,
  ): Unsubscribe {
    return onSnapshot(
      query(collection(this.firebase.db, 'workers'), limit(10)),
      (snapshot) => onData(snapshot.docs.map((d) => fromDocument<WorkerHeartbeat>(d.data()))),
      (error) => onError?.(error),
    );
  }

  /**
   * Every machine that has ever run an agent, live.
   *
   * The companion to {@link watchWorkers} and not a replacement for it: a
   * heartbeat says what a worker believes it is doing, and cannot say anything
   * at all when no worker is running — which is the state somebody looking at a
   * stalled queue most needs explained. An agent beating with state STOPPED
   * means the PC is on and waiting; an agent that has gone quiet means the PC
   * is off, and no amount of worker heartbeat can tell those apart.
   *
   * Bounded like every other listener here. Four is generous for a system whose
   * premise is one machine with a GPU.
   */
  watchAgents(
    onData: (agents: AgentReport[]) => void,
    onError?: (error: Error) => void,
  ): Unsubscribe {
    return onSnapshot(
      query(collection(this.firebase.db, 'agents'), limit(4)),
      (snapshot) => onData(snapshot.docs.map((d) => fromDocument<AgentReport>(d.data()))),
      (error) => onError?.(error),
    );
  }

  /**
   * Ask a machine to start or stop its worker.
   *
   * A wish, not a command: this writes what is wanted and returns. The agent on
   * that machine is the only thing that can spawn a process, and what it does
   * about the wish — and how long it takes — is reported back through the same
   * document.
   *
   * `requestedAt` is written on every call, including one that repeats the
   * current value, and that is load-bearing rather than incidental. Pressing
   * Start on a machine whose agent has given up is how a person clears it, and
   * a supervisor comparing only the value could not tell that press from the
   * wish simply still being RUNNING. The rules require the field for the same
   * reason.
   */
  async wish(agentId: string, uid: string, desired: AgentDesired): Promise<void> {
    await updateDoc(doc(this.firebase.db, 'agents', agentId), {
      desired,
      requestedBy: uid,
      requestedAt: new Date().toISOString(),
    });
  }

  /**
   * One clip, live.
   *
   * Separate from the queue listener for the same reason `watchJob` is separate
   * from `watchJobs`: the detail page has to work when opened directly, and a
   * clip that has already been reviewed is not in the queue at all.
   */
  watchClip(
    clipId: string,
    onData: (clip: Clip | null) => void,
    onError?: (error: Error) => void,
  ): Unsubscribe {
    return onSnapshot(
      doc(this.firebase.db, 'clips', clipId),
      (snapshot) => onData(snapshot.exists() ? fromDocument<Clip>(snapshot.data()) : null),
      (error) => onError?.(error),
    );
  }

  /**
   * Change a clip's copy and the reviewer's note, without deciding anything.
   *
   * `title` and `description` are publishable copy; `reviewNote` never leaves
   * the system. They travel together because they are edited together, on one
   * screen, in one thought.
   */
  async editClip(
    clipId: string,
    edits: { title: string | null; description: string | null; reviewNote: string | null },
  ): Promise<void> {
    await updateDoc(doc(this.firebase.db, 'clips', clipId), { ...edits });
  }

  /** The review queue: every clip awaiting a decision, whoever submitted it. */
  watchReviewQueue(
    onData: (clips: Clip[]) => void,
    review: ReviewState = 'PENDING',
    onError?: (error: Error) => void,
  ): Unsubscribe {
    return onSnapshot(
      query(
        collection(this.firebase.db, 'clips'),
        where('review', '==', review),
        orderBy('createdAt', 'desc'),
        limit(50),
      ),
      (snapshot) => onData(snapshot.docs.map((d) => fromDocument<Clip>(d.data()))),
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
    return snapshot.exists() ? fromDocument<ClipPreview>(snapshot.data()) : null;
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
    return snapshot.exists() ? fromDocument<Candidate>(snapshot.data()) : null;
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

  /**
   * Approve or reject a clip, and release its bucket copy.
   *
   * The copy exists so the clip can be reviewed away from the machine. Once it
   * has been reviewed, that job is done — keeping it would mean paying to store
   * a video whose only purpose has been served, and waiting up to the retention
   * window for a lifecycle rule to notice.
   *
   * Deleting the object is safe in a way it would not be for the local file:
   * `localPath` still points at the render on the worker, which is the copy
   * that gets published. This removes a cache, not an artefact.
   *
   * The object goes first. If that fails the Firestore write is skipped, so the
   * clip keeps a `storagePath` that still resolves — the alternative order can
   * leave a document claiming there is no copy while the bytes sit in the
   * bucket until the lifecycle rule collects them.
   */
  async review(
    clipId: string,
    review: ReviewState,
    storagePath?: string | null,
    edits?: { title: string | null; description: string | null; reviewNote: string | null },
  ): Promise<void> {
    // Edits ride along with the decision. Reviewing from the detail page means
    // the title someone just retyped and the verdict are one intention, and a
    // separate save they might not press is where that edit goes missing.
    const changes: Record<string, unknown> = {
      ...(edits ?? {}),
      review,
      reviewedAt: new Date().toISOString(),
    };

    if (storagePath) {
      const { deleteObject, ref } = await import('firebase/storage');
      try {
        await deleteObject(ref(this.firebase.storage, storagePath));
        changes['storagePath'] = null;
        changes['playbackExpiresAt'] = null;
      } catch (error) {
        // Already collected is the common case and not a failure: the document
        // should stop pointing at it either way. Anything else leaves the
        // pointer alone rather than lying about what is in the bucket.
        if ((error as { code?: string } | null)?.code === 'storage/object-not-found') {
          changes['storagePath'] = null;
          changes['playbackExpiresAt'] = null;
        }
      }
    }

    await updateDoc(doc(this.firebase.db, 'clips', clipId), changes);
    await this.settleLineage(clipId, review);
  }

  /**
   * Carry a decision to the rest of the versions it was also about.
   *
   * A reviewer decides about a video, not about an attempt. The fifth cut is
   * what the first four were for, so approving it settles them too — and
   * rejecting it rejects the idea, not the latest render of it.
   *
   * Before this they were left PENDING for ever. `latestOfEachLineage` keeps
   * them out of the queue, so nothing looked wrong; they simply accumulated,
   * and the reviewer met them later as a pile of tidying that the decision
   * should already have done.
   *
   * Approving marks the others superseded rather than deleting them here: the
   * worker's tidy pass removes them after a grace period, which is what leaves
   * room to change your mind. Rejecting marks the whole lineage REJECTED and
   * the same pass takes the records and bins the files.
   *
   * Failures are swallowed on purpose. The decision itself is already written,
   * and a lineage that did not settle is untidy rather than wrong — throwing
   * here would report a failed review that actually succeeded.
   */
  private async settleLineage(clipId: string, review: ReviewState): Promise<void> {
    if (review !== 'APPROVED' && review !== 'REJECTED') return;
    try {
      const decided = await getDoc(doc(this.firebase.db, 'clips', clipId));
      const lineageId = (decided.data() as Clip | undefined)?.lineageId ?? clipId;
      const versions = await this.loadLineage(lineageId);
      const others = versions.filter((clip) => clip.id !== clipId);
      if (others.length === 0) return;

      const { writeBatch } = await import('firebase/firestore');
      const now = new Date().toISOString();
      const batch = writeBatch(this.firebase.db);
      for (const clip of others) {
        batch.update(
          doc(this.firebase.db, 'clips', clip.id),
          review === 'REJECTED'
            ? { review: 'REJECTED', reviewedAt: now }
            : { supersededAt: now },
        );
      }
      await batch.commit();
    } catch (error) {
      console.warn('could not settle the rest of the lineage', error);
    }
  }

  /**
   * Every version of one clip, oldest first.
   *
   * The history behind a queue row. A remake produces a new clip rather than
   * editing the one that was reviewed, which is what makes a correction
   * reversible — but it also means the reasoning is spread across several
   * documents, and only together do they answer "what did I ask for, and what
   * did it do about it?".
   *
   * Keyed on `lineageId` rather than by walking `derivedFromClipId`: one
   * equality filter instead of a chain of round trips, and it still works when
   * a clip in the middle has been deleted.
   */
  async loadLineage(lineageId: string): Promise<Clip[]> {
    const { getDocs } = await import('firebase/firestore');
    const snapshot = await getDocs(
      query(collection(this.firebase.db, 'clips'), where('lineageId', '==', lineageId), limit(25)),
    );
    return snapshot.docs
      .map((d) => fromDocument<Clip>(d.data()))
      .sort((a, b) => (a.version ?? 1) - (b.version ?? 1));
  }

  /**
   * What the system has worked out about you, and has not yet been told to keep.
   *
   * Live rather than fetched once: a remake proposes preferences while the
   * reviewer is still on the page that queued it, and a list that needed a
   * reload to show them would be a list nobody ever saw.
   */
  watchPreferences(
    onData: (preferences: Preference[]) => void,
    status: PreferenceStatus = 'PROPOSED',
    onError?: (error: Error) => void,
  ): Unsubscribe {
    return onSnapshot(
      query(collection(this.firebase.db, 'preferences'), where('status', '==', status), limit(50)),
      (snapshot) => onData(snapshot.docs.map((d) => fromDocument<Preference>(d.data()))),
      (error) => onError?.(error),
    );
  }

  /**
   * Keep a preference, or turn it down for good.
   *
   * Rejecting stores the decision rather than deleting the row, and that is the
   * point: the worker checks every proposal against what it already holds in
   * ANY status, so a suggestion that has been turned down once cannot come back
   * on the next correction of the same kind of clip.
   */
  async decidePreference(
    preferenceId: string,
    uid: string,
    status: 'ACCEPTED' | 'REJECTED',
  ): Promise<void> {
    await updateDoc(doc(this.firebase.db, 'preferences', preferenceId), {
      status,
      decidedAt: new Date().toISOString(),
      decidedBy: uid,
    });
  }

  /**
   * Make a set of rectangles a property of the channel rather than of one clip.
   *
   * This is the whole difference between a feature and a chore. A broadcaster's
   * bug is in the same place on every video it will ever publish, so a reviewer
   * who has to ask for it on each clip is doing the system's bookkeeping. Once
   * this is set, RENDER applies it as it cuts and the clip arrives clean.
   *
   * `null` clears it. The write is a single field — firestore.rules pins it to
   * exactly that name, because everything else on a source describes a file on
   * the worker.
   */
  async rememberObscure(sourceId: string, obscure: ObscureOptions | null): Promise<void> {
    await updateDoc(doc(this.firebase.db, 'sources', sourceId), { obscure });
  }

  // ── Tidying up ─────────────────────────────────────────────────────────────
  //
  // **Deleting a record never touches a file.** The two are separate acts
  // because they answer separate questions: whether a clip belongs in the
  // review queue is decided dozens of times a day and is cheap to get wrong,
  // and whether the 400 MB behind it is still wanted is decided rarely and is
  // expensive to get wrong. Removing media lives on the worker's local API,
  // reachable only from the machine holding it — see `LocalApiService`.

  /**
   * Forget one clip. Its media stays on whichever machine holds it.
   *
   * Publications are left behind on purpose. They are the record of what was
   * actually posted and where, and an audit trail that vanishes when somebody
   * tidies their queue is not an audit trail.
   */
  async deleteClip(clipId: string): Promise<void> {
    const { deleteDoc } = await import('firebase/firestore');
    await deleteDoc(doc(this.firebase.db, 'clips', clipId));
  }

  /** Forget a proposal nobody acted on. */
  async deleteCandidate(candidateId: string): Promise<void> {
    const { deleteDoc } = await import('firebase/firestore');
    await deleteDoc(doc(this.firebase.db, 'candidates', candidateId));
  }

  /**
   * Forget one job. Its event log stays where it is.
   *
   * Deleting a document does not delete its subcollections, and `events` is
   * `allow write: if false` in the rules, so nothing on this side could clear
   * the log even in a loop. Orphaning it is the accepted outcome
   * (docs/adr/0017-deleting-a-record-is-not-deleting-a-file.md) — this says so
   * because the sentence that used to be here claimed the log went too, and the
   * confirmation the operator reads was repeating it.
   */
  async deleteJob(jobId: string): Promise<void> {
    const { deleteDoc } = await import('firebase/firestore');
    await deleteDoc(doc(this.firebase.db, 'jobs', jobId));
  }

  /**
   * What deleting a source would take with it.
   *
   * Asked before the confirmation rather than after, because "delete this
   * source" and "delete this source, eleven clips and forty candidates" are
   * different decisions and only one of them was offered.
   */
  async sourceFootprint(sourceId: string): Promise<{ clips: number; candidates: number }> {
    const { getCountFromServer } = await import('firebase/firestore');
    const ofSource = where('sourceId', '==', sourceId);
    const [clips, candidates] = await Promise.all([
      getCountFromServer(query(collection(this.firebase.db, 'clips'), ofSource)),
      getCountFromServer(query(collection(this.firebase.db, 'candidates'), ofSource)),
    ]);
    return {
      clips: clips.data().count,
      candidates: candidates.data().count,
    };
  }

  /**
   * Forget a source and everything cut from it.
   *
   * The cascade is here rather than in the rules because rules see one document
   * at a time and cannot express "and its children". Doing it in batches is
   * what keeps it close to atomic: each batch commits or does not, so a failure
   * leaves a partly-cleared tree rather than a half-written document, and
   * running it again finishes the job.
   *
   * Returns how much went, so the confirmation can be answered with a fact.
   */
  async deleteSource(sourceId: string): Promise<{ clips: number; candidates: number }> {
    const { getDocs, writeBatch } = await import('firebase/firestore');
    const ofSource = where('sourceId', '==', sourceId);

    let removed = { clips: 0, candidates: 0 };
    for (const name of ['clips', 'candidates'] as const) {
      // Firestore caps a batch at 500 writes. Looping a bounded page at a time
      // keeps one enormous source from needing a different code path.
      for (;;) {
        const page = await getDocs(
          query(collection(this.firebase.db, name), ofSource, limit(BATCH_LIMIT)),
        );
        if (page.empty) break;
        const batch = writeBatch(this.firebase.db);
        page.docs.forEach((d) => batch.delete(d.ref));
        await batch.commit();
        removed = { ...removed, [name]: removed[name] + page.size };
        if (page.size < BATCH_LIMIT) break;
      }
    }

    const { deleteDoc } = await import('firebase/firestore');
    await deleteDoc(doc(this.firebase.db, 'sources', sourceId));
    return removed;
  }

  /** Approved clips, the publish queue's input. */
  watchApproved(onData: (clips: Clip[]) => void, onError?: (error: Error) => void): Unsubscribe {
    return this.watchReviewQueue(onData, 'APPROVED', onError);
  }

  /**
   * Ask the worker to publish an approved clip.
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
   * Ask the worker to put a clip in the bucket so this device can play it.
   *
   * The reason this is a job and not a request to the worker: the worker's
   * control API answers on 127.0.0.1, which on a phone is the phone. The queue
   * is the only channel that reaches it from the sofa, and it already handles
   * leases, retries and reporting — so the button writes a document and the
   * clip's own `storagePath` arriving is the answer.
   *
   * `maxAttempts` is 2. The failures worth retrying are network ones; the rest
   * — no local copy, no bucket configured — are refused by the stage without
   * burning an attempt, so a higher number would only slow down the message.
   */
  async requestUpload(uid: string, clipId: string): Promise<string> {
    const reference = doc(collection(this.firebase.db, 'jobs'));
    const now = new Date().toISOString();
    await setDoc(reference, {
      id: reference.id,
      uid,
      type: 'UPLOAD',
      status: 'QUEUED',
      submission: null,
      sourceId: null,
      clipId,
      notBefore: null,
      stages: [{ name: 'UPLOAD', lane: 'CPU', status: 'PENDING' }],
      workerId: null,
      leaseExpiresAt: null,
      attempts: 0,
      maxAttempts: 2,
      error: null,
      createdAt: now,
      updatedAt: now,
      startedAt: null,
      endedAt: null,
    });
    return reference.id;
  }

  /**
   * Ask the worker to score a clip with a track.
   *
   * Creates a job, not a render — same shape as `requestPublish`, and for the
   * same reason: the media lives on the worker and the work happens there.
   *
   * `maxAttempts` is 1. Every way the music stage fails is a property of its
   * inputs — a link that is not a link, a track with no audio, a source the
   * workspace GC has taken — and a retry reproduces them exactly while paying
   * for the download twice.
   */
  async requestMusic(uid: string, clipId: string, options: MusicOptions): Promise<string> {
    const reference = doc(collection(this.firebase.db, 'jobs'));
    const now = new Date().toISOString();
    await setDoc(reference, {
      id: reference.id,
      uid,
      type: 'MUSIC',
      status: 'QUEUED',
      submission: null,
      sourceId: null,
      clipId,
      musicOptions: options,
      notBefore: null,
      stages: [{ name: 'MUSIC', lane: 'CPU', status: 'PENDING' }],
      workerId: null,
      leaseExpiresAt: null,
      attempts: 0,
      maxAttempts: 1,
      error: null,
      createdAt: now,
      updatedAt: now,
      startedAt: null,
      endedAt: null,
    });
    return reference.id;
  }

  /**
   * Ask the worker to remake a clip with corrections.
   *
   * The same shape as `requestMusic`, and for the same reason: the media lives
   * on the worker and a correction is decided while watching something that was
   * finished hours ago. It produces a *new* clip rather than altering this one,
   * so there is nothing here to undo.
   *
   * `maxAttempts` is 1. Every way a remake fails is a property of its inputs —
   * a source the workspace collector has taken, a language with no voice, nudges
   * that cross over — and a retry reproduces them exactly while spending the
   * render time twice.
   *
   * The options go into the job verbatim, including `keepMusic`, which decides
   * whether the new version gets the track this clip was scored with. Verbatim
   * means every field has to be in the `hasOnly` list `remakeOptionsOk` in
   * firebase/firestore.rules enforces: a field this app invents and the rules
   * have not been told about does not arrive stripped, it gets the whole create
   * denied with "Missing or insufficient permissions", which names nothing.
   */
  async requestRemake(uid: string, clipId: string, options: RemakeOptions): Promise<string> {
    const reference = doc(collection(this.firebase.db, 'jobs'));
    const now = new Date().toISOString();
    await setDoc(reference, {
      id: reference.id,
      uid,
      type: 'REMAKE',
      status: 'QUEUED',
      submission: null,
      sourceId: null,
      clipId,
      remakeOptions: options,
      notBefore: null,
      stages: [{ name: 'REMAKE', lane: 'CPU', status: 'PENDING' }],
      workerId: null,
      leaseExpiresAt: null,
      attempts: 0,
      maxAttempts: 1,
      error: null,
      createdAt: now,
      updatedAt: now,
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
    return snapshot.docs.map((d) => fromDocument<Publication>(d.data()));
  }

  /**
   * The same audit trail, live.
   *
   * The one-shot load is right for a queue of fifty rows; it is wrong for the
   * page you are looking at while the upload happens. The worker writes a
   * PENDING publication before it calls YouTube and stamps it PUBLISHED after,
   * so a page that read once shows "pending" until somebody reloads it — which
   * is exactly the moment an operator concludes the worker is stuck.
   *
   * Bounded like every other listener here. A clip with more than twenty
   * publish attempts has a problem no screen can solve.
   */
  watchPublications(
    clipId: string,
    onData: (publications: Publication[]) => void,
    onError?: (error: Error) => void,
  ): Unsubscribe {
    return onSnapshot(
      query(collection(this.firebase.db, 'clips', clipId, 'publications'), limit(20)),
      (snapshot) => onData(snapshot.docs.map((d) => fromDocument<Publication>(d.data()))),
      (error) => onError?.(error),
    );
  }

  /**
   * Every publish job, whatever state it is in.
   *
   * What a `Publication` cannot tell you: a publish that has been *asked for*
   * and not yet run has no publication at all — the worker writes that document
   * when it starts. Without this, a scheduled upload and an untouched clip look
   * identical the moment the page is reloaded, and the queue invites the
   * operator to publish the same clip twice.
   *
   * One equality filter and a bound, so no composite index is needed. Sorting
   * is left to the caller for the same reason: `where` plus `orderBy` on a
   * different field is what would require one.
   */
  watchPublishJobs(onData: (jobs: Job[]) => void, onError?: (error: Error) => void): Unsubscribe {
    return onSnapshot(
      query(collection(this.firebase.db, 'jobs'), where('type', '==', 'PUBLISH'), limit(100)),
      (snapshot) => onData(snapshot.docs.map((d) => fromDocument<Job>(d.data()))),
      (error) => onError?.(error),
    );
  }

  /**
   * Everything the worker was ever asked to do to one clip.
   *
   * Publish, upload, music, remake — the operational history behind a single
   * video, which is otherwise only legible by scrolling the Jobs page and
   * matching ids by eye.
   */
  async loadJobsForClip(clipId: string): Promise<Job[]> {
    const { getDocs } = await import('firebase/firestore');
    const snapshot = await getDocs(
      query(collection(this.firebase.db, 'jobs'), where('clipId', '==', clipId), limit(25)),
    );
    return snapshot.docs
      .map((d) => fromDocument<Job>(d.data()))
      .sort((a, b) => (a.createdAt < b.createdAt ? 1 : -1));
  }

  /**
   * What actually happened to one clip after it went out, day by day.
   *
   * The Insights page asks this across every clip at once; this asks it about
   * the one on screen, which is the question somebody looking at a published
   * video actually has. Ordered here rather than in the query, because
   * `where` plus `orderBy('date')` is a composite index for a result set that
   * is at most a few dozen rows.
   */
  async loadMetricsForClip(clipId: string): Promise<MetricSnapshot[]> {
    const { getDocs } = await import('firebase/firestore');
    const snapshot = await getDocs(
      query(collection(this.firebase.db, 'metrics'), where('clipId', '==', clipId), limit(400)),
    );
    return snapshot.docs
      .map((d) => fromDocument<MetricSnapshot>(d.data()))
      .sort((a, b) => (a.date < b.date ? -1 : 1));
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
      (snapshot) => onData(snapshot.docs.map((d) => fromDocument<Channel>(d.data()))),
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
      (snapshot) => onData(snapshot.exists() ? fromDocument<Channel>(snapshot.data()) : null),
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
      (snapshot) => onData(snapshot.docs.map((d) => fromDocument<UserProfile>(d.data()))),
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

  /**
   * Every metric snapshot in the workspace, oldest day first.
   *
   * **Not scoped by uid**, like every other listener here. The first two clips
   * this project published went out under two different accounts, and a
   * uid-filtered version of this would have drawn half the channel while
   * looking complete — the same failure the review queue already fixed.
   */
  watchMetrics(
    onData: (snapshots: MetricSnapshot[]) => void,
    onError?: (error: Error) => void,
  ): Unsubscribe {
    return onSnapshot(
      // **Descending**, then reversed. Every listener here is bounded, and a
      // bound only makes sense with the newest end kept: ordered ascending, the
      // 2001st snapshot would push the dashboard into showing the oldest 2000
      // for ever — freezing on the first clips ever published while new ones
      // silently never appeared, with nothing on screen to say so.
      query(collection(this.firebase.db, 'metrics'), orderBy('date', 'desc'), limit(2000)),
      (snapshot) =>
        onData(snapshot.docs.map((d) => fromDocument<MetricSnapshot>(d.data())).reverse()),
      (error) => onError?.(error),
    );
  }

  /**
   * The most recent calibration report, whoever ran it.
   *
   * One, not all of them. The history matters — a conclusion that changed is
   * the interesting case — but it belongs on a screen somebody asks for, not on
   * the one that answers "how are we doing". `uid` on a report records who
   * generated it, not whose data it covers, so it is not filtered on.
   */
  watchLatestCalibration(
    onData: (report: CalibrationReport | null) => void,
    onError?: (error: Error) => void,
  ): Unsubscribe {
    return onSnapshot(
      query(collection(this.firebase.db, 'calibrations'), orderBy('generatedAt', 'desc'), limit(1)),
      (snapshot) =>
        onData(snapshot.empty ? null : fromDocument<CalibrationReport>(snapshot.docs[0]!.data())),
      (error) => onError?.(error),
    );
  }
}
