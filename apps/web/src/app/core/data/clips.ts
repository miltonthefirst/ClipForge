import { Injectable, inject } from '@angular/core';
import type {
  Candidate,
  Clip,
  ClipPreview,
  MetricSnapshot,
  ReviewState,
} from '@clipforge/contracts';

import { StorageGateway } from '../storage/gateway';
import { FirestoreGateway, type Live } from '../firestore/gateway';
import type { QuerySpec } from '../firestore/spec';

/**
 * Clips, and the decision made about them.
 *
 * Candidates are here too, and deliberately. A candidate exists only to become
 * a clip: the review screen reads one beside the other, and the only two things
 * anyone ever does with one are look at it and forget it. A service of its own
 * would be separation without a concern.
 *
 * Every listener in here is **bounded**, and that is a cost decision rather than
 * tidiness: Firestore bills a read per delivered document, and a listener
 * re-delivers its whole result set on reconnect
 * (docs/adr/0009-spark-tier-local-artefacts.md).
 *
 * Nothing is scoped by uid. ClipForge is one shared workspace, so a clip
 * rendered from a job submitted on a phone belongs in the queue shown on the
 * desktop beside it — the limit is what keeps a listener cheap, not a filter
 * that also hid half the system from the person looking at it.
 *
 * The live reads keep their `watch` prefix although they now hand back signals
 * rather than callbacks. What comes back holds a listener until it is released,
 * and a name like `reviewQueue()` reads like a cheap getter — which is exactly
 * the thing that must not be called from a template.
 */

// ── Queries ──────────────────────────────────────────────────────────────────

/** The review queue: every clip in one review state, newest first. */
export function reviewQueueSpec(review: ReviewState): QuerySpec {
  return {
    collection: 'clips',
    where: [['review', '==', review]],
    orderBy: [['createdAt', 'desc']],
    // Fifty. Every one of them is delivered again on each reconnect, so this
    // number is what the queue costs each time the phone wakes up.
    limit: 50,
  };
}

/**
 * Every version of one clip.
 *
 * Keyed on `lineageId` rather than by walking `derivedFromClipId`: one equality
 * filter instead of a chain of round trips, and it still works when a clip in
 * the middle has been deleted.
 */
export function lineageSpec(lineageId: string): QuerySpec {
  // Twenty-five, which also keeps `settleLineage` inside a single batched
  // write: the gate caps a batch at Firestore's 500 and leaves paging to the
  // caller, and a bound of 25 means there is nothing here to page.
  return { collection: 'clips', where: [['lineageId', '==', lineageId]], limit: 25 };
}

/**
 * Every daily snapshot for one clip.
 *
 * Unordered on purpose: `where` plus `orderBy('date')` is a composite index for
 * a result set that is at most a few dozen rows, so {@link byDate} does it in
 * memory instead.
 */
export function clipMetricsSpec(clipId: string): QuerySpec {
  // Four hundred day-rows — a clip polled daily for over a year.
  return { collection: 'metrics', where: [['clipId', '==', clipId]], limit: 400 };
}

// ── Shaping ──────────────────────────────────────────────────────────────────

/**
 * A lineage, oldest attempt first.
 *
 * By `version` and not by `createdAt`: two remakes of the same parent are
 * siblings rather than a sequence, and the number is what says which attempt
 * each one is. A clip written before versions existed is the first attempt.
 */
export function byVersion(clips: readonly Clip[]): Clip[] {
  return [...clips].sort((a, b) => (a.version ?? 1) - (b.version ?? 1));
}

/**
 * Snapshots, oldest day first.
 *
 * `date` is YYYY-MM-DD in the channel's reporting timezone, so comparing the
 * strings is comparing the days.
 */
export function byDate(snapshots: readonly MetricSnapshot[]): MetricSnapshot[] {
  return [...snapshots].sort((a, b) => (a.date < b.date ? -1 : 1));
}

/**
 * What a decision writes onto the other versions in the lineage.
 *
 * Approving marks them superseded rather than deleting them here: the worker's
 * tidy pass removes them after a grace period, which is what leaves room to
 * change your mind. Rejecting marks the whole lineage REJECTED and the same
 * pass takes the records and bins the files.
 *
 * `null` for PENDING. Putting a clip back in the queue is the absence of a
 * decision, and there is nothing to carry to the versions it was also about.
 */
export function lineageSettlement(
  review: ReviewState,
  now: string,
): Record<string, unknown> | null {
  if (review === 'REJECTED') return { review: 'REJECTED', reviewedAt: now };
  if (review === 'APPROVED') return { supersededAt: now };
  return null;
}

// ── The repository ───────────────────────────────────────────────────────────

@Injectable({ providedIn: 'root' })
export class ClipsRepository {
  private readonly db = inject(FirestoreGateway);
  /** The bucket copy `review` releases. A separate service, and a separate gate. */
  private readonly bucket = inject(StorageGateway);

  // ── Reading ────────────────────────────────────────────────────────────────

  /**
   * One clip, live.
   *
   * Separate from the queue listener for the same reason `watchJob` is separate
   * from `watchJobs`: the detail page has to work when opened directly, and a
   * clip that has already been reviewed is not in the queue at all.
   *
   * `data()` is null while it loads and null again when there is no such clip.
   * `loading()` is what tells those apart, and a page that reads both cannot
   * show "not found" over a clip that is still on its way.
   */
  watchClip(clipId: string): Live<Clip> {
    return this.db.liveDoc<Clip>('clips', clipId);
  }

  /**
   * The review queue: every clip awaiting a decision, whoever submitted it.
   *
   * The query most worth holding open. A reviewer works through it one clip at
   * a time, and Review → Clip → Review used to pay for the whole queue twice,
   * because a listener closed on the way out re-reads every document when it is
   * attached again. The gate keeps it warm for fifteen minutes past its last
   * reader, so the trip back renders from what is already in hand.
   */
  watchReviewQueue(review: ReviewState = 'PENDING'): Live<Clip[]> {
    return this.db.live<Clip>(reviewQueueSpec(review));
  }

  /** Approved clips, the publish queue's input. */
  watchApproved(): Live<Clip[]> {
    return this.watchReviewQueue('APPROVED');
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
    // Write-once: RENDER writes it and nothing updates it, so coming back to
    // the queue redraws from memory instead of re-fetching 28 KB of base64.
    return this.db.stableDoc<ClipPreview>(`clips/${clipId}/preview`, 'poster');
  }

  /**
   * The candidate a clip came from — its score breakdown, hook and reason.
   *
   * On the free tier this is most of what a phone review has to go on, since
   * the video itself cannot be played remotely.
   */
  async loadCandidate(candidateId: string): Promise<Candidate | null> {
    // Write-once, the same as the poster: ANALYZE proposed this window and
    // nothing rewrites it.
    return this.db.stableDoc<Candidate>('candidates', candidateId);
  }

  /**
   * Every version of one clip, oldest first.
   *
   * The history behind a queue row. A remake produces a new clip rather than
   * editing the one that was reviewed, which is what makes a correction
   * reversible — but it also means the reasoning is spread across several
   * documents, and only together do they answer "what did I ask for, and what
   * did it do about it?".
   */
  async loadLineage(lineageId: string): Promise<Clip[]> {
    return byVersion(await this.db.once<Clip>(lineageSpec(lineageId)));
  }

  /**
   * What actually happened to one clip after it went out, day by day.
   *
   * The Insights page asks this across every clip at once; this asks it about
   * the one on screen, which is the question somebody looking at a published
   * video actually has.
   */
  async loadMetricsForClip(clipId: string): Promise<MetricSnapshot[]> {
    return byDate(await this.db.once<MetricSnapshot>(clipMetricsSpec(clipId)));
  }

  // ── Deciding ───────────────────────────────────────────────────────────────

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
    await this.db.update('clips', clipId, { ...edits });
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
   * The object goes first. If that fails the pointer is left alone, so the clip
   * keeps a `storagePath` that still resolves — the alternative order can leave
   * a document claiming there is no copy while the bytes sit in the bucket
   * until the lifecycle rule collects them.
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

    if (storagePath && (await this.bucket.remove(storagePath))) {
      changes['storagePath'] = null;
      changes['playbackExpiresAt'] = null;
    }

    await this.db.update('clips', clipId, changes);
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
   * Failures are swallowed on purpose. The decision itself is already written,
   * and a lineage that did not settle is untidy rather than wrong — throwing
   * here would report a failed review that actually succeeded.
   */
  private async settleLineage(clipId: string, review: ReviewState): Promise<void> {
    const settlement = lineageSettlement(review, new Date().toISOString());
    if (!settlement) return;
    try {
      const decided = await this.db.onceDoc<Clip>('clips', clipId);
      // A clip written before lineages existed is its own root.
      const lineageId = decided?.lineageId ?? clipId;
      const versions = await this.loadLineage(lineageId);
      const others = versions.filter((clip) => clip.id !== clipId);
      if (others.length === 0) return;

      await this.db.writeAll(
        others.map((clip) => ({
          path: 'clips',
          id: clip.id,
          data: settlement,
          op: 'update' as const,
        })),
      );
    } catch (error) {
      console.warn('could not settle the rest of the lineage', error);
    }
  }

  // ── Tidying up ─────────────────────────────────────────────────────────────
  //
  // **Deleting a record never touches a file.** The two are separate acts
  // because they answer separate questions: whether a clip belongs in the
  // review queue is decided dozens of times a day and is cheap to get wrong,
  // and whether the 400 MB behind it is still wanted is decided rarely and is
  // expensive to get wrong. Removing media lives on the worker's local API,
  // reachable only from the machine holding it — see `LocalApiService`
  // (docs/adr/0017-deleting-a-record-is-not-deleting-a-file.md).

  /**
   * Forget one clip. Its media stays on whichever machine holds it.
   *
   * Publications are left behind on purpose. They are the record of what was
   * actually posted and where, and an audit trail that vanishes when somebody
   * tidies their queue is not an audit trail.
   */
  async deleteClip(clipId: string): Promise<void> {
    await this.db.remove('clips', clipId);
  }

  /** Forget a proposal nobody acted on. */
  async deleteCandidate(candidateId: string): Promise<void> {
    await this.db.remove('candidates', candidateId);
  }
}
