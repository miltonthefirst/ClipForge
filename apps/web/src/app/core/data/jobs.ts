import { Injectable, inject } from '@angular/core';
import type { Clip, Job, JobEvent, JobStatus } from '@clipforge/contracts';

import { FirestoreGateway, type Live } from '../firestore/gateway';
import type { QuerySpec } from '../firestore/spec';

/**
 * The queue: reading it, adding to it, and stopping or forgetting one job.
 *
 * Every listener here is **bounded**, and that is a cost decision rather than a
 * tidiness one: Firestore bills a read per delivered document, and a listener
 * re-delivers its whole result set on reconnect. An unbounded listen over
 * `jobs` is the single easiest way to burn the free daily quota
 * (docs/adr/0009-spark-tier-local-artefacts.md).
 *
 * Nothing here is *scoped by uid*. ClipForge is one shared workspace, so the
 * limit is what keeps a listener cheap — not a filter that also happened to
 * hide half the system from the person looking at it.
 */

const JOBS = 'jobs';

/**
 * How many jobs one queue listener delivers.
 *
 * Exported because the page has to say when it is showing fewer jobs than exist,
 * and it can only know that by comparing what arrived against this.
 */
export const JOB_PAGE = 25;

/** How much of one clip's operational history the detail page asks for. */
const CLIP_HISTORY = 25;

/** How many clips the job-results panel lists. The rest are only counted. */
const JOB_RESULT_CLIPS = 20;

/** How far back a job's event log is read. Long enough for a retried job. */
const EVENT_LOG = 200;

// ── The queries, as data ───────────────────────────────────────────────────

/**
 * The queue, newest first — everyone's, because there is only one.
 *
 * Not filtered by uid. ClipForge is a single shared workspace, so a job
 * submitted from a phone belongs in the list shown on the desktop beside it.
 * Filtering here was what made one system look like two: the rules would now
 * allow the read, but a query that asks only for its own rows gets only its
 * own rows regardless of what it is permitted to see.
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
 * same way {@link jobCountSpec} asks for a tab's total. A merged pair would
 * have two bounds where the page needs one: "the newest 25" is a single
 * answer, and "the newest 25 of each, re-sorted" costs twice the reads to
 * arrive at it and leaves the page unable to say plainly how much it is not
 * showing.
 *
 * The statuses are sorted before they go in. Firestore does not care what
 * order an `in` list is written in, but the cache does: the spec is its own
 * key, so `['RUNNING', 'QUEUED']` and `['QUEUED', 'RUNNING']` would open two
 * listeners over one question and bill for both.
 *
 * Filtering by status needs a composite index — `jobs [status ASC, createdAt
 * DESC]`, in firebase/firestore.indexes.json. The existing ASC pair does not
 * serve it: verified against the project, which answered FAILED_PRECONDITION
 * and named that exact index.
 */
export function jobsSpec(statuses: readonly JobStatus[] | null): QuerySpec {
  return {
    collection: JOBS,
    ...(statuses === null ? {} : { where: [['status', 'in', [...statuses].sort()]] as const }),
    orderBy: [['createdAt', 'desc']],
    limit: JOB_PAGE,
  };
}

/**
 * The same slice, counted rather than read.
 *
 * No ordering and no bound, deliberately: the tab labels have to be true about
 * jobs the bounded listener never delivers, and a count that stopped at 25
 * would answer the question the list already answered. It needs no composite
 * index — there is no ordering to serve — so the counts are right even while
 * the index the lists depend on is still being built.
 */
export function jobCountSpec(statuses: readonly JobStatus[] | null): QuerySpec {
  return {
    collection: JOBS,
    ...(statuses === null ? {} : { where: [['status', 'in', [...statuses].sort()]] as const }),
  };
}

/**
 * A job's event log, in the order things actually happened.
 *
 * Ordered by `(at, seq)`, not by `at` alone — the same ordering the worker
 * reads it back with. One transition can emit several events at the identical
 * instant (a reap emits LEASE_EXPIRED and REQUEUED together), and ordering by
 * timestamp alone leaves Firestore breaking the tie on a random document id,
 * so the log would read in a different order on different loads.
 */
export function jobEventsSpec(jobId: string): QuerySpec {
  return {
    collection: `${JOBS}/${jobId}/events`,
    orderBy: [
      ['at', 'asc'],
      ['seq', 'asc'],
    ],
    limit: EVENT_LOG,
  };
}

/**
 * Every publish job, whatever state it is in.
 *
 * One equality filter and a bound, so no composite index is needed. Sorting is
 * left to the caller for the same reason: `where` plus `orderBy` on a different
 * field is what would require one.
 */
export function publishJobsSpec(): QuerySpec {
  return { collection: JOBS, where: [['type', '==', 'PUBLISH']], limit: 100 };
}

/** Everything the worker was ever asked to do to one clip. */
export function jobsForClipSpec(clipId: string): QuerySpec {
  return { collection: JOBS, where: [['clipId', '==', clipId]], limit: CLIP_HISTORY };
}

// ── Shaping ────────────────────────────────────────────────────────────────

/**
 * Newest first, sorted here rather than in the query.
 *
 * `where('clipId')` plus `orderBy('createdAt')` is a composite index for a
 * result set that is at most a couple of dozen rows.
 */
export function newestFirst(jobs: readonly Job[]): Job[] {
  return [...jobs].sort((a, b) => (a.createdAt < b.createdAt ? 1 : -1));
}

/**
 * The document a client is allowed to write.
 *
 * The shape is constrained by firestore.rules: a client may create only a
 * QUEUED job, owned by itself, with no worker and no lease. Anything else is
 * rejected — the PWA may create work, never pipeline state.
 *
 * A function rather than an inline literal so the shape the rules police can be
 * asserted without a database: a job that arrives claiming a worker is rejected
 * at the server, but it is rejected long after the change that caused it.
 */
export function newJob(id: string, uid: string, submission: string, now: string) {
  return {
    id,
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
  } satisfies Job;
}

// ── The repository ─────────────────────────────────────────────────────────

@Injectable({ providedIn: 'root' })
export class JobsRepository {
  private readonly db = inject(FirestoreGateway);

  /**
   * The queue, live. See {@link jobsSpec} for what it asks for and why.
   *
   * Returns signals rather than delivering through a callback. The caller reads
   * `data`, `loading` and `error` where it renders them, which is what retires
   * the shape this used to have: a watcher that re-subscribed on a tab change
   * had to create an `effect` to read its own result, and an effect created
   * inside another effect is not valid in Angular.
   *
   * `release()` when the page is done with it. The listener is held warm for
   * fifteen minutes after that, so coming back to the same tab costs nothing.
   */
  watchJobs(statuses: readonly JobStatus[] | null = null): Live<Job[]> {
    return this.db.live<Job>(jobsSpec(statuses));
  }

  /**
   * How many jobs are in one slice, without reading them.
   *
   * An aggregation rather than a longer listen: counting them by fetching them
   * would cost a read each for a number.
   */
  countJobs(statuses: readonly JobStatus[] | null): Promise<number> {
    return this.db.count(jobCountSpec(statuses));
  }

  /**
   * One job, live.
   *
   * A separate listener from {@link watchJobs} rather than a lookup into its
   * result, because the detail page has to work when it is opened directly — a
   * link from a phone notification, a bookmark, a reload — and because the list
   * is capped at 25, so an older job is not in it at all.
   *
   * `data` null once `loading` is false means the job does not exist, which is
   * what a deleted job looks like to a page still open on it.
   */
  watchJob(jobId: string): Live<Job> {
    return this.db.liveDoc<Job>(JOBS, jobId);
  }

  /**
   * A job's event log, live. Ordering and its reasons: {@link jobEventsSpec}.
   *
   * This is the only place a stalled job explains itself: the stage list says
   * *where* it stopped, and the log says *what happened* — reclaimed after a
   * lease expiry, retried, cancelled.
   */
  watchJobEvents(jobId: string): Live<JobEvent[]> {
    return this.db.live<JobEvent>(jobEventsSpec(jobId));
  }

  /**
   * Every publish job, whatever state it is in.
   *
   * What a `Publication` cannot tell you: a publish that has been *asked for*
   * and not yet run has no publication at all — the worker writes that document
   * when it starts. Without this, a scheduled upload and an untouched clip look
   * identical the moment the page is reloaded, and the queue invites the
   * operator to publish the same clip twice.
   */
  watchPublishJobs(): Live<Job[]> {
    return this.db.live<Job>(publishJobsSpec());
  }

  /**
   * Enqueue a job. The shape written is {@link newJob}; the id comes from the
   * gate, because a job carries its own id as a field and so needs one before
   * the write rather than from it.
   */
  async submit(uid: string, submission: string): Promise<string> {
    const id = this.db.newId(JOBS);
    await this.db.create(JOBS, { ...newJob(id, uid, submission, new Date().toISOString()) }, id);
    return id;
  }

  /** Cancel a job. The only job transition the rules let a client drive. */
  async cancel(jobId: string): Promise<void> {
    await this.db.update(JOBS, jobId, {
      status: 'CANCELLED',
      updatedAt: new Date().toISOString(),
    });
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
   *
   * Only offer this on a job that has stopped. Deleting one a worker still
   * holds does not stop the worker: it runs the stage to the end, writes the
   * job back with `batch.set`, and the runner then marks the document it just
   * recreated COMPLETED. The job the operator deleted reappears looking
   * finished. `isTerminal` in core/job-list.ts is that gate and the full
   * account of it; the rules allow any approved user to delete any job, so this
   * side is the only thing enforcing it.
   */
  async deleteJob(jobId: string): Promise<void> {
    await this.db.remove(JOBS, jobId);
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
    const ofJob = [['jobId', '==', jobId]] as const;
    const [candidates, clips] = await Promise.all([
      this.db.count({ collection: 'candidates', where: ofJob }),
      this.db.once<Clip>({ collection: 'clips', where: ofJob, limit: JOB_RESULT_CLIPS }),
    ]);
    return { candidates, clips };
  }

  /**
   * Everything the worker was ever asked to do to one clip.
   *
   * Publish, upload, music, remake — the operational history behind a single
   * video, which is otherwise only legible by scrolling the Jobs page and
   * matching ids by eye.
   */
  async loadJobsForClip(clipId: string): Promise<Job[]> {
    return newestFirst(await this.db.once<Job>(jobsForClipSpec(clipId)));
  }
}
