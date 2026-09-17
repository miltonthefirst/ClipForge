import { Injectable, inject } from '@angular/core';
import type {
  Channel,
  Job,
  MusicOptions,
  Publication,
  PublishOptions,
  RemakeOptions,
} from '@clipforge/contracts';

import { FirestoreGateway, type Live } from '../firestore/gateway';
import type { QuerySpec } from '../firestore/spec';

/**
 * Asking the worker for work, and reading what came of it.
 *
 * Four requests and three ways of reading the result, together because they are
 * one errand. The phone can publish nothing itself — the credentials live on
 * the worker (docs/adr/0010-worker-held-publishing-credentials.md) and so does
 * the media — so each of these buttons writes a job document and then watches
 * for the answer to turn up on a clip, a publication or a channel.
 */

/**
 * A clip's publish attempts.
 *
 * The bound is the caller's, because the two readers want different answers from
 * it — see {@link PublishingRepository.loadPublications} and
 * {@link PublishingRepository.watchPublications}.
 */
export function publicationsSpec(clipId: string, limit?: number): QuerySpec {
  return { collection: `clips/${clipId}/publications`, limit };
}

const JOBS = 'jobs';

/** How many publications one clip's live audit trail delivers. */
export const PUBLICATION_PAGE = 20;

/** How many channels the destination picker delivers. */
export const CHANNEL_PAGE = 50;

/** Every publishing channel, for the destination picker. */
export function channelsSpec(): QuerySpec {
  return { collection: 'channels', limit: CHANNEL_PAGE };
}

/** What differs between the four things this app can ask the worker to do. */
export interface JobRequest {
  readonly id: string;
  readonly uid: string;
  readonly type: Job['type'];
  readonly clipId: string;
  /** Ordered, and each caller's own: the lists differ and so do the reasons. */
  readonly stages: Job['stages'];
  /** Each caller's own, for a reason each caller states. */
  readonly maxAttempts: number;
  /** When the job becomes claimable, ISO. Null means immediately. */
  readonly notBefore?: string | null;
  /**
   * The type's own options block, carried under its own key and untouched.
   * Typed loosely enough to hold any one of the three; the four callers are in
   * this file and none of them passes two.
   */
  readonly options?: Partial<Pick<Job, 'publishOptions' | 'musicOptions' | 'remakeOptions'>>;
  /** ISO. An argument rather than a clock read, so this is a function of its inputs. */
  readonly now: string;
}

/**
 * The job document the four requests share.
 *
 * `status`, `workerId`, `leaseExpiresAt` and `attempts` are fixed here rather
 * than passed, because the rules require exactly these values of a client: "The
 * PWA may enqueue work, but only as itself and only in the one state that means
 * 'not yet started'" (firebase/firestore.rules). A job that arrived already
 * RUNNING would be denied, not corrected.
 *
 * `submission` and `sourceId` are null on all four: those belong to ingestion,
 * and everything here acts on a clip that already exists.
 *
 * Returning a `Job` rather than a bag of fields is what keeps this honest. The
 * contract is generated from packages/contracts/schemas/clipforge.json, so a
 * field the worker stops reading stops compiling here.
 */
export function newJob(request: JobRequest): Job {
  return {
    id: request.id,
    uid: request.uid,
    type: request.type,
    status: 'QUEUED',
    submission: null,
    sourceId: null,
    clipId: request.clipId,
    ...request.options,
    notBefore: request.notBefore ?? null,
    stages: request.stages,
    workerId: null,
    leaseExpiresAt: null,
    attempts: 0,
    maxAttempts: request.maxAttempts,
    error: null,
    createdAt: request.now,
    updatedAt: request.now,
    startedAt: null,
    endedAt: null,
  };
}

/**
 * The publishing side of Firestore, as signals and promises.
 *
 * Live reads hand back a {@link Live} handle rather than taking a callback and
 * returning an `Unsubscribe`: a page reads `data()`, `loading()` and `error()`,
 * and calls `release()` when it is finished. The release is not a formality —
 * these listeners are shared and held warm for fifteen minutes past their last
 * reader, and a reader that never leaves is one that never stops costing.
 */
@Injectable({ providedIn: 'root' })
export class PublishingRepository {
  private readonly db = inject(FirestoreGateway);

  // ── Asking for work ──────────────────────────────────────────────────────

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
  requestPublish(
    uid: string,
    clipId: string,
    publishAt: Date | null = null,
    options: PublishOptions | null = null,
  ): Promise<string> {
    return this.enqueue({
      id: this.db.newId(JOBS),
      uid,
      type: 'PUBLISH',
      clipId,
      options: { publishOptions: options },
      notBefore: publishAt ? publishAt.toISOString() : null,
      stages: [{ name: 'PUBLISH', lane: 'CPU', status: 'PENDING' }],
      maxAttempts: 3,
      now: new Date().toISOString(),
    });
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
  requestUpload(uid: string, clipId: string): Promise<string> {
    return this.enqueue({
      id: this.db.newId(JOBS),
      uid,
      type: 'UPLOAD',
      clipId,
      stages: [{ name: 'UPLOAD', lane: 'CPU', status: 'PENDING' }],
      maxAttempts: 2,
      now: new Date().toISOString(),
    });
  }

  /**
   * Ask the worker to score a clip with a track.
   *
   * Creates a job, not a render — same shape as {@link requestPublish}, and for
   * the same reason: the media lives on the worker and the work happens there.
   *
   * `maxAttempts` is 1. Nearly every way the music stage fails is a property of
   * its inputs — a link that is not a link, a track with no audio, a source the
   * workspace GC has taken — and a retry reproduces them exactly while paying
   * for the download twice. One failure is no longer of that kind: a wedged
   * ffmpeg is raised as a TimeoutError, which the runner already classifies as
   * retryable (apps/worker/clipforge/media/scoring.py), and a single attempt
   * leaves nothing to retry it with. The number has not been revisited since.
   */
  requestMusic(uid: string, clipId: string, options: MusicOptions): Promise<string> {
    return this.enqueue({
      id: this.db.newId(JOBS),
      uid,
      type: 'MUSIC',
      clipId,
      options: { musicOptions: options },
      stages: [{ name: 'MUSIC', lane: 'CPU', status: 'PENDING' }],
      maxAttempts: 1,
      now: new Date().toISOString(),
    });
  }

  /**
   * Ask the worker to remake a clip with corrections.
   *
   * The same shape as {@link requestMusic}, and for the same reason: the media
   * lives on the worker and a correction is decided while watching something
   * that was finished hours ago. It produces a *new* clip rather than altering
   * this one, so there is nothing here to undo.
   *
   * `maxAttempts` is 1. Nearly every way a remake fails is a property of its
   * inputs — a source the workspace collector has taken, a language with no
   * voice, nudges that cross over — and a retry reproduces them exactly while
   * spending the render time twice. The exception is the one MUSIC has: a wedged
   * ffmpeg times out, the runner counts a timeout as retryable, and a single
   * attempt leaves nothing to retry it with. The number has not been revisited
   * since.
   *
   * The options go into the job verbatim, including `keepMusic`, which decides
   * whether the new version gets the track this clip was scored with, and
   * `captions`, which decides what becomes of the burnt-in ones. Verbatim means
   * every field has to be in the `hasOnly` list `remakeOptionsOk` in
   * firebase/firestore.rules enforces: a field this app invents and the rules
   * have not been told about does not arrive stripped, it gets the whole create
   * denied with "Missing or insufficient permissions", which names nothing.
   */
  requestRemake(uid: string, clipId: string, options: RemakeOptions): Promise<string> {
    return this.enqueue({
      id: this.db.newId(JOBS),
      uid,
      type: 'REMAKE',
      clipId,
      options: { remakeOptions: options },
      stages: [{ name: 'REMAKE', lane: 'CPU', status: 'PENDING' }],
      maxAttempts: 1,
      now: new Date().toISOString(),
    });
  }

  // ── What came of it ──────────────────────────────────────────────────────

  /**
   * A clip's publish history — the audit trail, read-only.
   *
   * Answers "who authorised this, on what basis, and what went out?" from the
   * phone rather than from worker logs (Phase 8, exit criterion 5).
   *
   * Carries no bound, where {@link watchPublications} stops at twenty. Nothing
   * chose that difference — it is what the two have always done — and of the
   * pair it is this one that is wrong if either is.
   */
  loadPublications(clipId: string): Promise<Publication[]> {
    return this.db.once<Publication>(publicationsSpec(clipId));
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
   * Bounded like every other listener here. A clip with more than twenty publish
   * attempts has a problem no screen can solve.
   */
  watchPublications(clipId: string): Live<Publication[]> {
    return this.db.live<Publication>(publicationsSpec(clipId, PUBLICATION_PAGE));
  }

  /**
   * Every publishing channel, for the destination picker.
   *
   * Unbounded and safe to be, for the same reason the account list is: a channel
   * is a destination somebody set up by hand, and an install with enough of them
   * for this query to cost anything has a different problem. Bounded anyway, so
   * "safe today" does not quietly become "unbounded listen" later.
   */
  watchChannels(): Live<Channel[]> {
    return this.db.live<Channel>(channelsSpec());
  }

  /**
   * Follow one publishing channel.
   *
   * Read-only to every client: rules make `channels` worker-written, because the
   * worker is the only thing that can actually reach a channel and therefore the
   * only thing that can honestly report on one.
   *
   * `data()` null once `loading()` is false means there is no such channel. The
   * callback this replaced could only say null, which meant that and "not yet"
   * at the same time, so the settings page could not tell a channel nobody has
   * connected from one whose first snapshot has not arrived.
   */
  watchChannel(channelId: string): Live<Channel> {
    return this.db.liveDoc<Channel>('channels', channelId);
  }

  // ── Writing a job ────────────────────────────────────────────────────────

  private async enqueue(request: JobRequest): Promise<string> {
    const job = newJob(request);
    // Spread into an anonymous object because the gateway takes Firestore's
    // `DocumentData`, which is an index signature, and a named interface is not
    // assignable to one.
    await this.db.create(JOBS, { ...job }, job.id);
    return job.id;
  }
}
