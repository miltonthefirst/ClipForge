import type { Job, JobStatus, Stage } from '@clipforge/contracts';

/**
 * How the Jobs page is cut up, and what a card says about a running stage.
 *
 * Here rather than in the component for the reason `publish-state.ts` is: it is
 * the only part of that screen with a decision in it, and a wrong decision here
 * is quiet. A tab whose statuses do not add up shows a short list and says
 * nothing about what is missing, which is indistinguishable from work that was
 * never submitted.
 */

/** Which slice of the queue the page is showing. */
export type JobTab = 'active' | 'all' | 'completed' | 'failed';

export interface JobTabSpec {
  readonly key: JobTab;
  readonly label: string;
  /**
   * The statuses Firestore is asked for. `null` asks for everything — the one
   * tab that needs no filter, and so the one that needs no composite index.
   */
  readonly statuses: readonly JobStatus[] | null;
  /** What to say when this tab is empty but the queue is not. */
  readonly empty: string;
}

/**
 * The tabs, in the order they are shown, Active first and default.
 *
 * Active is two statuses rather than one because "a worker has it" and "nothing
 * has picked this up" are the same thing to someone asking whether to wait —
 * and splitting them would put a stalled queue on a tab nobody opens.
 *
 * Every `JobStatus` appears in exactly one of the three filtered tabs, so
 * Active + Completed + Failed is the whole collection and nothing can fall
 * between them. `All` is still asked for separately rather than summed: if a
 * status ever turns up that no tab lists, a total that disagrees with the parts
 * is how anyone would find out.
 */
export const JOB_TABS: readonly JobTabSpec[] = [
  {
    key: 'active',
    label: 'Active',
    statuses: ['RUNNING', 'QUEUED'],
    empty: 'Nothing running and nothing waiting.',
  },
  { key: 'all', label: 'All', statuses: null, empty: 'No jobs.' },
  {
    key: 'completed',
    label: 'Completed',
    statuses: ['COMPLETED'],
    empty: 'Nothing has finished yet.',
  },
  {
    key: 'failed',
    label: 'Failed or cancelled',
    statuses: ['FAILED', 'CANCELLED'],
    empty: 'Nothing has failed or been cancelled.',
  },
];

export const DEFAULT_JOB_TAB: JobTab = 'active';

/**
 * Read a tab out of whatever the URL happens to say.
 *
 * The query parameter is user-editable and survives a bookmark taken before a
 * tab was renamed, so anything unrecognised falls back to the default rather
 * than leaving the page with no listener at all.
 */
export function jobTab(value: string | null | undefined): JobTab {
  return JOB_TABS.some((tab) => tab.key === value) ? (value as JobTab) : DEFAULT_JOB_TAB;
}

export function jobTabSpec(key: JobTab): JobTabSpec {
  return JOB_TABS.find((tab) => tab.key === key) ?? JOB_TABS[0]!;
}

/**
 * What to call a job on a card.
 *
 * A CLIP job is its submission, which is what the queue has always shown. The
 * two job types that carry no submission — a research run is a question and a
 * compilation is a theme over several — would otherwise show a document id,
 * which tells the person who created them nothing about which one this is.
 */
export function jobTitle(job: Job): string {
  if (job.type === 'RESEARCH') {
    const topics = job.researchOptions?.topics ?? [];
    return topics.length ? `Research: ${topics.join(', ')}` : 'Research: whatever is trending';
  }
  if (job.type === 'COMPILE') {
    const options = job.compileOptions;
    return options ? `Compilation: ${options.title || options.theme}` : 'Compilation';
  }
  return job.submission ?? job.id;
}

/**
 * Whether this job has stopped for good.
 *
 * The gate on Delete, and the reason it is a gate: deleting a job the worker
 * still holds does not stop the worker. `JobStore.renew` finds no document and
 * only logs it, the stage runs to the end, `_record_success` writes the job
 * back with `batch.set`, and the runner then reads that recreated document,
 * sees RUNNING with its own worker id, and marks it COMPLETED. The job the
 * operator deleted reappears looking finished. MUSIC, PUBLISH, UPLOAD and
 * REMAKE are single-stage, so there is no checkpoint in between where the
 * missing document would be noticed.
 */
export function isTerminal(job: Job): boolean {
  return job.status === 'COMPLETED' || job.status === 'FAILED' || job.status === 'CANCELLED';
}

export interface StageProgress {
  /** Stages that will not run again — done or deliberately skipped. */
  readonly done: number;
  readonly total: number;
  /** The stage the worker is in, when one of them is. */
  readonly current: Stage | null;
}

/**
 * Where a job has got to, as "2 of 4, TRANSCRIBE".
 *
 * Computed once per job and handed to the template, because the card used to
 * call this three times per row to read three fields off the same object.
 */
export function stageProgress(job: Job): StageProgress {
  let done = 0;
  let current: Stage | null = null;
  for (const stage of job.stages) {
    if (stage.status === 'DONE' || stage.status === 'SKIPPED') done += 1;
    else if (stage.status === 'RUNNING' && !current) current = stage;
  }
  return { done, total: job.stages.length, current };
}

/**
 * What a stage says it is doing, when that is still true.
 *
 * Shown for RUNNING because that is the whole point of the field, and for
 * FAILED because where a stage got to before it died is the one thing the error
 * message usually does not say.
 *
 * The other statuses are excluded by this side rather than trusted to arrive
 * empty. The worker does clear the field — the stage-start copy sets it to
 * null, and the lease heartbeat that carries it will only write it onto a
 * RUNNING stage — but "a note means this is happening now" is a rule about what
 * the screen means, and it is cheaper to state it where the note is read than
 * to inherit it from a writer three processes away. "Mixing" beside a duration
 * and a green chip would describe a moment that has passed.
 */
export function stageNote(stage: Stage): string | null {
  if (stage.status !== 'RUNNING' && stage.status !== 'FAILED') return null;
  return stage.progress?.trim() || null;
}

/**
 * The one note worth putting on a job's card: the running stage's, or the
 * failed one's.
 *
 * Deliberately not `stageProgress().current`. That answers "is a worker inside
 * a stage right now", which is what the stage count and the "· ANALYZE" beside
 * it need — pointing it at a FAILED stage would have a stopped job claiming to
 * be working. This answers a different question, so it is a different function.
 *
 * RUNNING wins when both exist, which happens on a retried job whose earlier
 * attempt left a FAILED stage behind: what is happening now displaces what
 * went wrong before, and the job page shows the full stage list either way.
 *
 * A terminal job's RUNNING stage is ignored, because that combination is a lie
 * nobody is left to correct. Cancelling writes `status` and nothing else — the
 * rules let a client touch status, updatedAt and endedAt, never `stages` — so a
 * worker killed mid-mix leaves a stage marked RUNNING with "Mixing the music
 * into the clip" on it, and the operator who presses Cancel to clear the stuck
 * row gets a CANCELLED job that goes on claiming to be mixing. Nothing polls
 * when no worker is running, which is exactly the state being cancelled out of,
 * so no later write comes along to tidy it. The FAILED stage is still worth
 * reading in that case: it is the one the worker wrote on purpose.
 */
export function jobNote(job: Job): string | null {
  const stopped = isTerminal(job);
  let failed: Stage | null = null;
  for (const stage of job.stages) {
    if (stage.status === 'RUNNING' && !stopped) return stageNote(stage);
    if (stage.status === 'FAILED' && !failed) failed = stage;
  }
  return failed ? stageNote(failed) : null;
}

/**
 * What to say when the queue listener fails outright.
 *
 * A failed query has to look different from an empty one and from one still
 * loading. It used to look like neither: the error went to a banner, the list
 * stayed null, and the page went on saying "Loading the queue…" underneath it
 * for as long as anyone left it open.
 *
 * The missing index gets its own sentence because Firestore's own message for
 * it is a console URL in a paragraph of SDK prose, and because it is the one
 * failure here that is fixed by a deploy rather than by a change to the code —
 * the operator reading this is the person who can fix it. Everything else keeps
 * the raw message: an unrecognised failure rewritten into a friendly sentence
 * is how a real cause gets hidden.
 */
export function queueFailure(error: unknown): string {
  const code = (error as { code?: string } | null)?.code;
  const message = error instanceof Error ? error.message : String(error ?? '');

  if (code === 'failed-precondition' || /requires an index/i.test(message)) {
    return (
      'This tab filters the queue by status, and the database index that serves ' +
      'that query is not deployed on this project yet. Deploying it — firebase ' +
      'deploy --only firestore:indexes — is all this needs. The All tab asks ' +
      'for no filter, so it still works meanwhile.'
    );
  }
  if (code === 'permission-denied') {
    return 'This account is not allowed to read the queue.';
  }
  return message || 'The queue could not be loaded, and gave no reason.';
}
