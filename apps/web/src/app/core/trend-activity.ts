import type { Clip, Job } from '@clipforge/contracts';

import { jobNote, stageProgress } from './job-list';

/**
 * What became of a trend: the jobs made from it, and the clips those made.
 *
 * Worked out here rather than in the page because the sentences are the
 * point. "Clip it" on a trend has one visible effect — the button says
 * Queued — and the job it made then lives on the Jobs page, its clips on the
 * Review page, and nothing led back. A job that finished with nothing to cut
 * was the worst case: COMPLETED on one page, an empty queue on another, and
 * no sentence anywhere joining the two.
 */

export interface JobActivity {
  readonly job: Job;
  /** "Clip" or "Compilation" — what was asked for, in a word. */
  readonly kind: string;
  /** "3 of 4 stages · RENDER" */
  readonly progress: string;
  /** What the running stage says it is doing, or where the failed one stopped. */
  readonly note: string | null;
  /** The clips this job produced, best-known first. */
  readonly clips: readonly Clip[];
  /** Ran to the end and made nothing — not a failure, and the one outcome that needs saying. */
  readonly finishedEmpty: boolean;
}

export interface TrendActivity {
  readonly rows: readonly JobActivity[];
  readonly running: number;
  readonly toReview: number;
  readonly approved: number;
  readonly empty: number;
}

export function kindOf(job: Job): string {
  switch (job.type) {
    case 'CLIP':
      return 'Clip';
    case 'COMPILE':
      return 'Compilation';
    case 'COMPOSE':
      return 'Drawn video';
    default:
      return job.type.charAt(0) + job.type.slice(1).toLowerCase();
  }
}

/** Newest first, so what was just pressed is at the top. */
function newestFirst(jobs: readonly Job[]): Job[] {
  return [...jobs].sort((a, b) => b.createdAt.localeCompare(a.createdAt));
}

/**
 * Group the clips under the jobs that made them, and count what matters.
 *
 * `finishedEmpty` is judged from the clips actually present rather than from
 * a checkpoint, because this is read beside the clips: a COMPLETED job with
 * no clip listed under it is the fact the reader sees, whatever the worker
 * recorded.
 */
export function activityFor(jobs: readonly Job[], clips: readonly Clip[]): TrendActivity {
  const byJob = new Map<string, Clip[]>();
  for (const clip of clips) {
    // A clip always has a job on this path — it was queried by jobId — but the
    // contract allows none, for clips made another way.
    if (!clip.jobId) continue;
    const list = byJob.get(clip.jobId) ?? [];
    list.push(clip);
    byJob.set(clip.jobId, list);
  }
  const rows = newestFirst(jobs).map((job): JobActivity => {
    const made = byJob.get(job.id) ?? [];
    const progress = stageProgress(job);
    return {
      job,
      kind: kindOf(job),
      progress:
        `${progress.done} of ${progress.total} stages` +
        (progress.current ? ` · ${progress.current.name}` : ''),
      note: jobNote(job),
      clips: made,
      finishedEmpty: job.status === 'COMPLETED' && made.length === 0,
    };
  });
  return {
    rows,
    running: rows.filter((row) => row.job.status === 'QUEUED' || row.job.status === 'RUNNING')
      .length,
    toReview: clips.filter((clip) => clip.review === 'PENDING').length,
    approved: clips.filter((clip) => clip.review === 'APPROVED').length,
    empty: rows.filter((row) => row.finishedEmpty).length,
  };
}

/**
 * One line for the trend card: "1 running · 2 to review". Null when nothing
 * has been made from the trend, so the card can say "Details" instead.
 */
export function describeActivity(activity: TrendActivity): string | null {
  if (!activity.rows.length) return null;
  const parts: string[] = [];
  if (activity.running) parts.push(`${activity.running} running`);
  if (activity.toReview) parts.push(`${activity.toReview} to review`);
  if (activity.approved) parts.push(`${activity.approved} approved`);
  if (activity.empty) {
    parts.push(activity.empty === 1 ? 'nothing to cut' : `${activity.empty} with nothing to cut`);
  }
  const failed = activity.rows.filter((row) => row.job.status === 'FAILED').length;
  if (failed) parts.push(`${failed} failed`);
  if (!parts.length) {
    const n = activity.rows.length;
    return `${n} job${n === 1 ? '' : 's'}`;
  }
  return parts.join(' · ');
}
