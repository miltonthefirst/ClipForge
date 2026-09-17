import type { Job, Publication } from '@clipforge/contracts';

/**
 * What is happening to one clip on its way to a platform.
 *
 * Derived from two sources that each know only half of it, which is why this
 * exists at all rather than being a field somewhere:
 *
 *  - a `Publication` is written by the **worker**, and only once it has picked
 *    the job up. Before that there is nothing to read.
 *  - a PUBLISH `Job` is written by **this app**, the moment the operator asks.
 *    It is the only evidence that a scheduled upload exists, and it stops
 *    existing as evidence the instant it completes.
 *
 * Reading only publications is what made a scheduled publish indistinguishable
 * from a clip nobody had touched: queue it for Friday, reload the page, and the
 * queue offers Publish again — so the same clip goes out twice. Reading only
 * jobs would lose the published-months-ago case, because jobs are pruned and
 * the publication is the permanent record.
 *
 * Ordered most-settled first. A clip that has been published and then queued
 * again is *published*; what is pending is a second upload, and saying "queued"
 * would hide the video already on the channel.
 */
export type PublishStateKind =
  'PUBLISHED' | 'UPLOADING' | 'SCHEDULED' | 'QUEUED' | 'FAILED' | 'READY';

export interface PublishState {
  readonly kind: PublishStateKind;
  /**
   * For a chip, in the operator's words rather than the enum's.
   *
   * Deliberately one or two words. A chip shares a row with the clip's title
   * on a 390px screen, and "Last attempt failed" pushed that title down to
   * three truncated words. The page that has room for a sentence writes its
   * own.
   */
  readonly label: string;
  /** The attempt this state is about, when one exists. */
  readonly publication: Publication | null;
  /** The job this state is about, when one is still outstanding. */
  readonly job: Job | null;
  /** The instant that matters here: published at, scheduled for, failed at. */
  readonly at: string | null;
}

/** Jobs that have not finished, and so still say something about the future. */
function outstanding(jobs: readonly Job[]): Job[] {
  return jobs.filter((job) => job.status === 'QUEUED' || job.status === 'RUNNING');
}

function newest<T extends { createdAt: string }>(items: readonly T[]): T | null {
  let best: T | null = null;
  for (const item of items) {
    if (!best || item.createdAt > best.createdAt) best = item;
  }
  return best;
}

/**
 * What to say about this clip, and what to offer.
 *
 * `now` is a parameter rather than a call to the clock so that "scheduled for a
 * time that has passed" is testable, and because that case is real: a publish
 * scheduled for Friday whose worker was off all weekend is still QUEUED on
 * Monday, and calling it "scheduled" then would be a lie about why nothing has
 * happened.
 */
export function publishStateOf(
  publications: readonly Publication[],
  jobs: readonly Job[],
  now: Date = new Date(),
): PublishState {
  const published = newest(publications.filter((p) => p.state === 'PUBLISHED'));
  if (published) {
    return {
      kind: 'PUBLISHED',
      label: 'Published',
      publication: published,
      job: null,
      at: published.publishedAt ?? published.createdAt,
    };
  }

  const uploading = newest(
    publications.filter((p) => p.state === 'PENDING' || p.state === 'UPLOADING'),
  );
  if (uploading) {
    return {
      kind: 'UPLOADING',
      label: 'Uploading',
      publication: uploading,
      job: outstanding(jobs)[0] ?? null,
      at: uploading.createdAt,
    };
  }

  const live = newest(outstanding(jobs));
  if (live) {
    // A running job with no publication yet is the worker between claiming the
    // job and writing the record. Held apart from QUEUED because "the worker
    // has it" and "nothing has picked this up" are the two states an operator
    // most needs to tell apart — see the worker-must-be-running note in
    // docs/README.
    if (live.status === 'RUNNING') {
      return {
        kind: 'UPLOADING',
        label: 'Uploading',
        publication: null,
        job: live,
        at: live.createdAt,
      };
    }
    const due = live.notBefore ? new Date(live.notBefore) : null;
    if (due && due.getTime() > now.getTime()) {
      return {
        kind: 'SCHEDULED',
        label: 'Scheduled',
        publication: null,
        job: live,
        at: live.notBefore!,
      };
    }
    return {
      kind: 'QUEUED',
      label: 'Queued',
      publication: null,
      job: live,
      at: live.createdAt,
    };
  }

  const failed = newest(publications.filter((p) => p.state === 'FAILED'));
  if (failed) {
    return {
      kind: 'FAILED',
      label: 'Failed',
      publication: failed,
      job: null,
      at: failed.createdAt,
    };
  }

  return { kind: 'READY', label: 'Ready to publish', publication: null, job: null, at: null };
}
