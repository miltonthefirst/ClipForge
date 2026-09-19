import { Injectable, inject } from '@angular/core';
import type {
  Clip,
  CompileOptions,
  ComposeOptions,
  Job,
  ResearchOptions,
  Trend,
  TrendStatus,
} from '@clipforge/contracts';

import { FirestoreGateway, type Live } from '../firestore/gateway';
import type { QuerySpec } from '../firestore/spec';

/**
 * Trend research, and the compilations that come out of it.
 *
 * Two job types the client may create — RESEARCH asks the worker what the web
 * is talking about, COMPILE asks it to stitch several videos into one — and
 * one collection the worker writes that a person decides about. Nothing here
 * runs anything: a research job is a question, and its answer is a list to
 * pick from. The picking is what the Trends page is for.
 *
 * Bounded like every other listener in `core/data`: a read bills per delivered
 * document, and a listener re-delivers everything on reconnect.
 */

const JOBS = 'jobs';
const TRENDS = 'trends';
const CLIPS = 'clips';

/**
 * Firestore's cap on the values an `in` filter may carry. It is also a run's
 * `maxTrends`, which is not a coincidence: one query finds the jobs for every
 * trend in a run.
 */
export const IN_LIMIT = 30;

/** How many past runs the Trends page offers to look back through. */
export const RESEARCH_RUNS = 10;

/** The most rows one run can write, from the contract's `maxTrends`. */
export const TRENDS_PER_RUN = 30;

// ── Queries ──────────────────────────────────────────────────────────────────

/**
 * Every research run, newest first once sorted.
 *
 * One equality filter and a bound, so no composite index: `where('type')`
 * plus `orderBy('createdAt')` would need one for a result set of ten rows,
 * and {@link newestFirst} in `jobs.ts` does the sort in memory instead.
 */
export function researchRunsSpec(): QuerySpec {
  return { collection: JOBS, where: [['type', '==', 'RESEARCH']], limit: RESEARCH_RUNS };
}

/** One run's list. Ranked in memory by {@link byRank}, for the same reason. */
export function trendsSpec(jobId: string): QuerySpec {
  return { collection: TRENDS, where: [['jobId', '==', jobId]], limit: TRENDS_PER_RUN };
}

// ── What became of a trend ──────────────────────────────────────────────────
//
// A job made from a trend carries `trendId`; a clip carries `jobId`. Two hops,
// each one equality-or-`in` filter and a bound, and no `orderBy` — so no
// composite index, and the page sorts in memory as the Jobs page does.
// See docs/adr/0026-what-became-of-a-trend.md.

/** Every job one trend produced. Thirty is a lot of jobs for one trend. */
export function jobsForTrendSpec(trendId: string): QuerySpec {
  return { collection: JOBS, where: [['trendId', '==', trendId]], limit: IN_LIMIT };
}

/** The jobs for every trend in a run, in one query. Callers pass at most {@link IN_LIMIT} ids. */
export function jobsForTrendsSpec(trendIds: readonly string[]): QuerySpec {
  return {
    collection: JOBS,
    where: [['trendId', 'in', trendIds.slice(0, IN_LIMIT)]],
    limit: 100,
  };
}

/** The clips a set of jobs made. Callers pass at most {@link IN_LIMIT} ids. */
export function clipsForJobsSpec(jobIds: readonly string[]): QuerySpec {
  return { collection: CLIPS, where: [['jobId', 'in', jobIds.slice(0, IN_LIMIT)]], limit: 100 };
}

// ── Shaping ──────────────────────────────────────────────────────────────────

/** Best first. `rank` is written by the worker and rewritten by curation. */
export function byRank(trends: readonly Trend[]): Trend[] {
  return [...trends].sort((a, b) => a.rank - b.rank || a.topic.localeCompare(b.topic));
}

/**
 * The research job a client is allowed to write.
 *
 * Mirrors `RESEARCH_PIPELINE` on the worker: a stage list is authoritative
 * for the job's whole life, so the shape here has to be the one the worker
 * registers stages for, or the job runs until it reaches a stage nothing can
 * run. A function rather than a literal so the shape can be asserted without
 * a database.
 */
export function newResearchJob(id: string, uid: string, options: ResearchOptions, now: string) {
  return {
    id,
    uid,
    type: 'RESEARCH',
    status: 'QUEUED',
    submission: null,
    sourceId: null,
    researchOptions: options,
    stages: [
      { name: 'RESEARCH', lane: 'CPU', status: 'PENDING' },
      { name: 'CURATE', lane: 'GPU', status: 'PENDING' },
    ],
    workerId: null,
    leaseExpiresAt: null,
    attempts: 0,
    // Two, not three: the failures worth retrying are network ones, and a
    // third attempt over the same feeds would only re-ask the same question.
    maxAttempts: 2,
    error: null,
    createdAt: now,
    updatedAt: now,
    startedAt: null,
    endedAt: null,
  } satisfies Job;
}

/**
 * The compose job a client is allowed to write. Mirrors `COMPOSE_PIPELINE`.
 *
 * No submission and no source: the job is asked for a topic and makes the
 * rest. Two attempts, as a compilation has — the script and the narration
 * are checkpointed, so a retry redraws rather than rewrites.
 */
export function newComposeJob(id: string, uid: string, options: ComposeOptions, now: string) {
  return {
    id,
    uid,
    type: 'COMPOSE',
    status: 'QUEUED',
    submission: null,
    sourceId: null,
    composeOptions: options,
    trendId: options.trendId ?? null,
    stages: [
      { name: 'SCRIPT', lane: 'GPU', status: 'PENDING' },
      { name: 'NARRATE', lane: 'CPU', status: 'PENDING' },
      { name: 'ALIGN', lane: 'GPU', status: 'PENDING' },
      { name: 'DRAW', lane: 'CPU', status: 'PENDING' },
      { name: 'ASSEMBLE', lane: 'CPU', status: 'PENDING' },
    ],
    workerId: null,
    leaseExpiresAt: null,
    attempts: 0,
    maxAttempts: 2,
    error: null,
    createdAt: now,
    updatedAt: now,
    startedAt: null,
    endedAt: null,
  } satisfies Job;
}

/** The compile job a client is allowed to write. Mirrors `COMPILE_PIPELINE`. */
export function newCompileJob(id: string, uid: string, options: CompileOptions, now: string) {
  return {
    id,
    uid,
    type: 'COMPILE',
    status: 'QUEUED',
    submission: null,
    sourceId: null,
    compileOptions: options,
    // Repeated from the options so a trend's page finds this job the same
    // way it finds a clip job: by one field, with one filter.
    trendId: options.trendId ?? null,
    stages: [
      { name: 'GATHER', lane: 'CPU', status: 'PENDING' },
      { name: 'SELECT', lane: 'GPU', status: 'PENDING' },
      { name: 'ASSEMBLE', lane: 'CPU', status: 'PENDING' },
    ],
    workerId: null,
    leaseExpiresAt: null,
    attempts: 0,
    maxAttempts: 2,
    error: null,
    createdAt: now,
    updatedAt: now,
    startedAt: null,
    endedAt: null,
  } satisfies Job;
}

// ── The repository ───────────────────────────────────────────────────────────

@Injectable({ providedIn: 'root' })
export class ResearchRepository {
  private readonly db = inject(FirestoreGateway);

  /** The last few runs, so a list from yesterday is still a click away. */
  watchRuns(): Live<Job[]> {
    return this.db.live<Job>(researchRunsSpec());
  }

  /**
   * One run's list, live.
   *
   * Live rather than once because the list fills in while the run is still
   * going: RESEARCH writes the rows and CURATE annotates them a minute later,
   * and a person who pressed the button is looking at the page when it does.
   */
  watchTrends(jobId: string): Live<Trend[]> {
    return this.db.live<Trend>(trendsSpec(jobId));
  }

  /** One trend, live: its status changes under the person reading it. */
  watchTrend(trendId: string): Live<Trend> {
    return this.db.liveDoc<Trend>(TRENDS, trendId);
  }

  /** What one trend produced, live, because a job just pressed is still moving. */
  watchTrendJobs(trendId: string): Live<Job[]> {
    return this.db.live<Job>(jobsForTrendSpec(trendId));
  }

  /** What every trend in a run produced, for the one-line summaries on the cards. */
  watchJobsForTrends(trendIds: readonly string[]): Live<Job[]> {
    return this.db.live<Job>(jobsForTrendsSpec(trendIds));
  }

  /** The clips those jobs made, so a trend can point at Review. */
  watchClipsForJobs(jobIds: readonly string[]): Live<Clip[]> {
    return this.db.live<Clip>(clipsForJobsSpec(jobIds));
  }

  /** Ask. Nothing runs until a worker claims it. */
  async startResearch(uid: string, options: ResearchOptions): Promise<string> {
    const id = this.db.newId(JOBS);
    await this.db.create(
      JOBS,
      { ...newResearchJob(id, uid, options, new Date().toISOString()) },
      id,
    );
    return id;
  }

  /** Make one clip from several videos. Also nothing until a worker claims it. */
  async startCompile(uid: string, options: CompileOptions): Promise<string> {
    const id = this.db.newId(JOBS);
    await this.db.create(
      JOBS,
      { ...newCompileJob(id, uid, options, new Date().toISOString()) },
      id,
    );
    return id;
  }

  /** Write, speak and draw a video about a topic. Nothing until a worker claims it. */
  async startCompose(uid: string, options: ComposeOptions): Promise<string> {
    const id = this.db.newId(JOBS);
    await this.db.create(
      JOBS,
      { ...newComposeJob(id, uid, options, new Date().toISOString()) },
      id,
    );
    return id;
  }

  /**
   * Record what a person made of a trend.
   *
   * The only thing a client may change on a trend, and the rules pin it to
   * exactly these three fields under the caller's own name. PROMOTED and
   * DISMISSED are both decisions; NEW puts it back.
   */
  async decide(trendId: string, status: TrendStatus, uid: string): Promise<void> {
    const decided = status === 'NEW';
    await this.db.update(TRENDS, trendId, {
      status,
      decidedAt: decided ? null : new Date().toISOString(),
      decidedBy: decided ? null : uid,
    });
  }

  /** Forget a row. There are no files behind it. */
  async deleteTrend(trendId: string): Promise<void> {
    await this.db.remove(TRENDS, trendId);
  }
}
