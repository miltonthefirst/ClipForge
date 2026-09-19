import type { CompileOptions, Job, JobStatus, Stage } from '@clipforge/contracts';
import { describe, expect, it } from 'vitest';

import {
  JOB_TABS,
  isTerminal,
  NOTHING_TO_CUT,
  finishedEmpty,
  jobNote,
  jobTab,
  jobTitle,
  queueFailure,
  stageNote,
  stageProgress,
} from './job-list';

/**
 * The Jobs page's list logic.
 *
 * What is asserted here rather than eyeballed is the part that is silent when
 * wrong: a tab whose statuses do not cover the enum hides work without saying
 * so, a Delete offered on a running job deletes a record the worker then
 * recreates as COMPLETED, and a query failure with no words of its own leaves
 * the page loading forever.
 */

/**
 * Every `JobStatus`, written so that a sixth one breaks the build.
 *
 * `JobStatus` is a string union, not a runtime enum, so there is nothing here
 * to enumerate at run time — and a hand-written array annotated `JobStatus[]`
 * goes on type-checking after the schema gains a status, which left the
 * coverage assertion below passing in exactly the case it was written to catch.
 * `satisfies Record<JobStatus, true>` makes that case a `npm run typecheck`
 * failure naming the missing key, and a status *removed* from the contract an
 * excess-property failure on the key left behind.
 */
const EVERY_STATUS = Object.keys({
  QUEUED: true,
  RUNNING: true,
  COMPLETED: true,
  FAILED: true,
  CANCELLED: true,
} satisfies Record<JobStatus, true>) as JobStatus[];

function stage(overrides: Partial<Stage> = {}): Stage {
  return { name: 'TRANSCRIBE', lane: 'GPU', status: 'PENDING', ...overrides } as Stage;
}

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: 'job-1',
    uid: 'user-1',
    type: 'CLIP',
    status: 'RUNNING',
    stages: [],
    attempts: 0,
    maxAttempts: 3,
    createdAt: '2026-09-14T11:00:00.000Z',
    updatedAt: '2026-09-14T11:00:00.000Z',
    ...overrides,
  } as Job;
}

describe('the job tabs', () => {
  it('puts every status on exactly one filtered tab', () => {
    // The whole reason the tabs query Firestore per tab: if a status belonged to
    // no tab, its jobs would be reachable only from All and nothing would say
    // so. This half checks the tabs against EVERY_STATUS; EVERY_STATUS is what
    // checks the list against the contract, and it does that at compile time.
    const filtered = JOB_TABS.filter((tab) => tab.statuses !== null);
    const covered = filtered.flatMap((tab) => [...tab.statuses!]);
    expect([...covered].sort()).toEqual([...EVERY_STATUS].sort());
  });

  it('asks for everything on exactly one tab', () => {
    expect(JOB_TABS.filter((tab) => tab.statuses === null)).toHaveLength(1);
  });

  it('defaults to the work that has not finished', () => {
    expect(jobTab(undefined)).toBe('active');
    expect(jobTab(null)).toBe('active');
  });

  it('ignores a tab name the URL made up', () => {
    // The parameter is user-editable and outlives a rename. Falling through to
    // the default beats leaving the page with no listener.
    expect(jobTab('everything')).toBe('active');
    expect(jobTab('')).toBe('active');
  });

  it('keeps a tab name it recognises', () => {
    expect(jobTab('completed')).toBe('completed');
    expect(jobTab('failed')).toBe('failed');
    expect(jobTab('all')).toBe('all');
  });
});

describe('isTerminal', () => {
  it('admits only jobs nothing is still writing', () => {
    expect(isTerminal(job({ status: 'COMPLETED' }))).toBe(true);
    expect(isTerminal(job({ status: 'FAILED' }))).toBe(true);
    expect(isTerminal(job({ status: 'CANCELLED' }))).toBe(true);
  });

  it('refuses a job a worker could still be holding', () => {
    // Deleting either of these leaves the stage running, and the worker writes
    // the document back — the job returns, marked COMPLETED.
    expect(isTerminal(job({ status: 'RUNNING' }))).toBe(false);
    expect(isTerminal(job({ status: 'QUEUED' }))).toBe(false);
  });
});

describe('stageProgress', () => {
  it('counts skipped stages as settled, because they will not run', () => {
    const progress = stageProgress(
      job({
        stages: [
          stage({ name: 'DOWNLOAD', status: 'DONE' }),
          stage({ name: 'TRANSCRIBE', status: 'SKIPPED' }),
          stage({ name: 'ANALYZE', status: 'RUNNING' }),
          stage({ name: 'RENDER', status: 'PENDING' }),
        ],
      }),
    );
    expect(progress.done).toBe(2);
    expect(progress.total).toBe(4);
    expect(progress.current?.name).toBe('ANALYZE');
  });

  it('has no current stage when nothing is running', () => {
    const progress = stageProgress(
      job({ status: 'QUEUED', stages: [stage({ status: 'PENDING' })] }),
    );
    expect(progress.done).toBe(0);
    expect(progress.current).toBeNull();
  });
});

describe('stageNote', () => {
  it('shows what a running stage says it is doing', () => {
    expect(stageNote(stage({ status: 'RUNNING', progress: 'Fetching the track' }))).toBe(
      'Fetching the track',
    );
  });

  it('keeps the note a stage failed on, which the error rarely repeats', () => {
    expect(stageNote(stage({ status: 'FAILED', progress: 'Fetching the track' }))).toBe(
      'Fetching the track',
    );
  });

  it('drops a note a finished stage was left holding', () => {
    // The worker clears the field when a stage ends, so this is asserting the
    // reading rule rather than cleaning up after it: a note means "this is
    // happening now", and "Mixing" beside a duration and a green chip would be
    // describing a moment that has passed.
    expect(stageNote(stage({ status: 'DONE', progress: 'Mixing' }))).toBeNull();
    expect(stageNote(stage({ status: 'SKIPPED', progress: 'Mixing' }))).toBeNull();
  });

  it('treats an absent or blank note as no note at all', () => {
    // The card renders nothing rather than an empty line.
    expect(stageNote(stage({ status: 'RUNNING' }))).toBeNull();
    expect(stageNote(stage({ status: 'RUNNING', progress: null }))).toBeNull();
    expect(stageNote(stage({ status: 'RUNNING', progress: '   ' }))).toBeNull();
  });
});

describe('finishedEmpty', () => {
  const rendered = (clipIds: string[] | undefined, status = 'COMPLETED', type = 'CLIP') =>
    ({
      id: 'j',
      uid: 'u',
      type,
      status,
      stages: [
        { name: 'DOWNLOAD', lane: 'CPU', status: 'DONE' },
        { name: 'RENDER', lane: 'CPU', status: 'DONE', checkpoint: clipIds && { clipIds } },
      ],
      attempts: 0,
      maxAttempts: 3,
      createdAt: 'now',
      updatedAt: 'now',
    }) as unknown as Parameters<typeof finishedEmpty>[0];

  it('is a completed clip job whose render step lists no clips', () => {
    expect(finishedEmpty(rendered([]))).toBe(true);
    expect(jobNote(rendered([]))).toBe(NOTHING_TO_CUT);
  });

  it('is not one that made something, is still going, or is another kind of job', () => {
    expect(finishedEmpty(rendered(['c1']))).toBe(false);
    expect(finishedEmpty(rendered([], 'RUNNING'))).toBe(false);
    expect(finishedEmpty(rendered(undefined))).toBe(false);
    expect(finishedEmpty(rendered([], 'COMPLETED', 'RESEARCH'))).toBe(false);
  });
});

describe('jobNote', () => {
  it('says what the stage a worker is inside is doing', () => {
    expect(
      jobNote(
        job({
          stages: [
            stage({ name: 'DOWNLOAD', status: 'DONE' }),
            stage({ name: 'ANALYZE', status: 'RUNNING', progress: 'Judging 40 candidates' }),
          ],
        }),
      ),
    ).toBe('Judging 40 candidates');
  });

  it('falls back to where the stage that failed had got to, without calling it current', () => {
    // The card has no other way to show this, and it is why the worker keeps the
    // note on a failed stage: the error says what broke, not what the stage was
    // part-way through when it broke.
    //
    // The second assertion is the one that keeps this honest. `current` means "a
    // worker is inside this stage right now"; pointed at the failed stage to
    // reach its note, it would print "1 of 2 stages · MUSIC" beside a FAILED
    // chip, as though the job were still working.
    const failed = job({
      status: 'FAILED',
      stages: [
        stage({ name: 'DOWNLOAD', status: 'DONE' }),
        stage({ name: 'MUSIC', status: 'FAILED', progress: 'Fetching the track' }),
      ],
    });
    expect(jobNote(failed)).toBe('Fetching the track');
    expect(stageProgress(failed).current).toBeNull();
  });

  it('prefers what is happening now to what went wrong on the last attempt', () => {
    expect(
      jobNote(
        job({
          stages: [
            stage({ name: 'DOWNLOAD', status: 'FAILED', progress: 'Fetching the video' }),
            stage({ name: 'TRANSCRIBE', status: 'RUNNING', progress: 'Loading the model' }),
          ],
        }),
      ),
    ).toBe('Loading the model');

    // And when the stage that is running has not said anything yet, the card
    // stays quiet rather than reviving a sentence from the attempt before it.
    expect(
      jobNote(
        job({
          stages: [
            stage({ name: 'DOWNLOAD', status: 'FAILED', progress: 'Fetching the video' }),
            stage({ name: 'TRANSCRIBE', status: 'RUNNING' }),
          ],
        }),
      ),
    ).toBeNull();
  });

  it('does not let a cancelled job go on claiming to be mixing', () => {
    // The state this describes is reachable and nothing clears it. Cancelling
    // writes `status` alone — firestore.rules lets a client set status,
    // updatedAt and endedAt, never `stages` — so a worker killed mid-mix leaves
    // MUSIC marked RUNNING with its last note on it, and the operator pressing
    // Cancel to clear the stuck row is by definition in the state where no
    // worker is left to tidy up. Read-side or not at all.
    expect(
      jobNote(
        job({
          status: 'CANCELLED',
          stages: [stage({ name: 'MUSIC', status: 'RUNNING', progress: 'Mixing the music' })],
        }),
      ),
    ).toBeNull();

    // The same rule on a job that ran out of attempts, where the failed stage's
    // note is still the most useful thing on the card and must survive it.
    expect(
      jobNote(
        job({
          status: 'FAILED',
          stages: [
            stage({ name: 'DOWNLOAD', status: 'FAILED', progress: 'Fetching the video' }),
            stage({ name: 'MUSIC', status: 'RUNNING', progress: 'Mixing the music' }),
          ],
        }),
      ),
    ).toBe('Fetching the video');
  });

  it('says nothing about a job that has stopped with nothing left running', () => {
    expect(
      jobNote(
        job({
          status: 'COMPLETED',
          stages: [stage({ name: 'MUSIC', status: 'DONE', progress: 'Mixing' })],
        }),
      ),
    ).toBeNull();
    expect(jobNote(job({ status: 'QUEUED', stages: [stage({ status: 'PENDING' })] }))).toBeNull();
  });
});

describe('queueFailure', () => {
  function firestoreError(code: string, message: string): Error {
    return Object.assign(new Error(message), { code });
  }

  it('explains a missing index as a deploy rather than as a console URL', () => {
    const said = queueFailure(
      firestoreError(
        'failed-precondition',
        'The query requires an index. You can create it here: ' +
          'https://console.firebase.google.com/v1/r/project/bytepic-clipforge/firestore/indexes?create_composite=Ci',
      ),
    );
    expect(said).toContain('firestore:indexes');
    expect(said).toContain('All tab');
    expect(said).not.toContain('https://');
  });

  it('recognises the missing index from the wording when the code is stripped', () => {
    // A listener error arrives through a callback typed as `Error`, and not
    // every path preserves the FirestoreError code on the way.
    expect(queueFailure(new Error('The query requires an index.'))).toContain('firestore:indexes');
  });

  it('says plainly when the account may not read the queue', () => {
    expect(
      queueFailure(firestoreError('permission-denied', 'Missing or insufficient permissions.')),
    ).toBe('This account is not allowed to read the queue.');
  });

  it('repeats a failure it does not recognise rather than inventing one', () => {
    // Rewriting an unknown cause into a friendly sentence is how the real one
    // gets hidden from the only person who could act on it.
    expect(queueFailure(firestoreError('unavailable', 'Backend unavailable.'))).toBe(
      'Backend unavailable.',
    );
  });

  it('still says something when there is no message at all', () => {
    expect(queueFailure(null)).toBe('The queue could not be loaded, and gave no reason.');
    expect(queueFailure(new Error(''))).toBe('The queue could not be loaded, and gave no reason.');
  });
});

describe('jobTitle', () => {
  it('is the submission for a clip, as the queue has always shown', () => {
    expect(jobTitle(job({ submission: 'https://youtu.be/x' }))).toBe('https://youtu.be/x');
  });

  it('falls back to the id when a clip somehow has no submission', () => {
    expect(jobTitle(job({ id: 'job-9' }))).toBe('job-9');
  });

  it('names a research run by its topics, or by the absence of any', () => {
    expect(jobTitle(job({ type: 'RESEARCH', researchOptions: { topics: ['f1', 'nba'] } }))).toBe(
      'Research: f1, nba',
    );
    expect(jobTitle(job({ type: 'RESEARCH', researchOptions: {} }))).toBe(
      'Research: whatever is trending',
    );
  });

  it('names a compilation by its title, else its theme', () => {
    const items: CompileOptions['items'] = [{ submission: 'a' }, { submission: 'b' }];
    expect(
      jobTitle(
        job({ type: 'COMPILE', compileOptions: { theme: 'goals', title: 'Goals!', items } }),
      ),
    ).toBe('Compilation: Goals!');
    expect(jobTitle(job({ type: 'COMPILE', compileOptions: { theme: 'goals', items } }))).toBe(
      'Compilation: goals',
    );
  });
});
