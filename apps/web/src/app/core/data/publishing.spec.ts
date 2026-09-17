import type { RemakeOptions } from '@clipforge/contracts';
import { describe, expect, it } from 'vitest';

import {
  CHANNEL_PAGE,
  PUBLICATION_PAGE,
  channelsSpec,
  newJob,
  publicationsSpec,
  type JobRequest,
} from './publishing';

/**
 * The document a job request puts on the wire.
 *
 * Worth testing rather than reading, because both ways this can go wrong are
 * invisible from the app. A job whose `status` or `attempts` is not what the
 * rules demand is refused with "Missing or insufficient permissions", which
 * names no field; and an options block that arrives reshaped is refused the same
 * way rather than arriving trimmed. Neither failure says which value did it.
 */

const NOW = '2026-09-17T12:00:00.000Z';

function request(overrides: Partial<JobRequest> = {}): JobRequest {
  return {
    id: 'job-1',
    uid: 'user-1',
    type: 'UPLOAD',
    clipId: 'clip-1',
    stages: [{ name: 'UPLOAD', lane: 'CPU', status: 'PENDING' }],
    maxAttempts: 2,
    now: NOW,
    ...overrides,
  };
}

describe('newJob', () => {
  it('queues a job in the one state the rules accept from a client', () => {
    const job = newJob(request());

    expect(job.status).toBe('QUEUED');
    expect(job.workerId).toBeNull();
    expect(job.leaseExpiresAt).toBeNull();
    expect(job.attempts).toBe(0);
  });

  it('carries a scheduled publish time as the job it will be claimed by', () => {
    const job = newJob(request({ type: 'PUBLISH', notBefore: '2026-09-18T07:00:00.000Z' }));

    expect(job.notBefore).toBe('2026-09-18T07:00:00.000Z');
  });

  it('leaves notBefore null when the work is wanted now', () => {
    expect(newJob(request()).notBefore).toBeNull();
  });

  it('keeps the attempt budget it was given rather than one of its own', () => {
    expect(newJob(request({ maxAttempts: 1 })).maxAttempts).toBe(1);
    expect(newJob(request({ maxAttempts: 3 })).maxAttempts).toBe(3);
  });

  it('carries a remake options block through untouched, keepMusic and captions included', () => {
    const options: RemakeOptions = {
      notes: 'the framing lost the ball',
      interpretNotes: true,
      keepMusic: false,
      captions: 'REMOVE',
      startDeltaSec: -2,
    };

    const job = newJob(request({ type: 'REMAKE', options: { remakeOptions: options } }));

    expect(job.remakeOptions).toEqual(options);
  });

  it('writes no options key at all for a type that has none', () => {
    const job = newJob(request());

    // Not "undefined": Firestore rejects an undefined field value outright, so
    // the key has to be absent rather than present and empty.
    expect('publishOptions' in job).toBe(false);
    expect('musicOptions' in job).toBe(false);
    expect('remakeOptions' in job).toBe(false);
  });

  it('records a publish with no operator choices as an explicit null, not an absent field', () => {
    const job = newJob(request({ type: 'PUBLISH', options: { publishOptions: null } }));

    expect(job.publishOptions).toBeNull();
  });

  it('stamps created and updated with the same instant, and starts neither', () => {
    const job = newJob(request());

    expect(job.createdAt).toBe(NOW);
    expect(job.updatedAt).toBe(NOW);
    expect(job.startedAt).toBeNull();
    expect(job.endedAt).toBeNull();
  });
});

describe('publicationsSpec', () => {
  it("reads a clip's publications from that clip's own subcollection", () => {
    expect(publicationsSpec('clip-1').collection).toBe('clips/clip-1/publications');
  });

  it('bounds the live audit trail, because a listener re-delivers everything it covers', () => {
    expect(publicationsSpec('clip-1', PUBLICATION_PAGE).limit).toBe(20);
  });

  it('leaves the one-shot load unbounded, as it has always been', () => {
    expect(publicationsSpec('clip-1').limit).toBeUndefined();
  });
});

describe('channelsSpec', () => {
  it('bounds the destination picker so a safe query cannot quietly become an unbounded one', () => {
    expect(channelsSpec()).toEqual({ collection: 'channels', limit: CHANNEL_PAGE });
    expect(CHANNEL_PAGE).toBe(50);
  });
});
