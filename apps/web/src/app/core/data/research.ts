import { Injectable, inject } from '@angular/core';
import type {
  CompileOptions,
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
