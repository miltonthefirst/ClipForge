import type { Job, Publication } from '@clipforge/contracts';
import { describe, expect, it } from 'vitest';

import { publishStateOf } from './publish-state';

/**
 * The publish queue's one piece of real reasoning.
 *
 * Worth testing rather than eyeballing because the failure it exists to prevent
 * is silent and expensive: a state that reads "ready" for a clip already queued
 * invites a second upload of the same video to the same channel, and YouTube
 * will happily accept it.
 */

const NOW = new Date('2026-09-14T12:00:00.000Z');

function publication(overrides: Partial<Publication> = {}): Publication {
  return {
    id: 'pub-1',
    clipId: 'clip-1',
    uid: 'user-1',
    platform: 'YOUTUBE',
    state: 'PUBLISHED',
    externalId: 'vid-1',
    externalUrl: 'https://www.youtube.com/watch?v=vid-1',
    createdAt: '2026-09-14T09:00:00.000Z',
    publishedAt: '2026-09-14T09:01:00.000Z',
    ...overrides,
  };
}

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: 'job-1',
    uid: 'user-1',
    type: 'PUBLISH',
    status: 'QUEUED',
    clipId: 'clip-1',
    stages: [{ name: 'PUBLISH', lane: 'CPU', status: 'PENDING' }],
    createdAt: '2026-09-14T11:00:00.000Z',
    updatedAt: '2026-09-14T11:00:00.000Z',
    ...overrides,
  } as Job;
}

describe('publishStateOf', () => {
  it('calls an untouched clip ready', () => {
    expect(publishStateOf([], [], NOW).kind).toBe('READY');
  });

  it('calls a queued publish waiting rather than ready', () => {
    // The bug this whole module exists for: reload the page after queueing and
    // the queue used to offer Publish again.
    const state = publishStateOf([], [job()], NOW);
    expect(state.kind).toBe('QUEUED');
    expect(state.job?.id).toBe('job-1');
  });

  it('separates a future publish from one that is merely waiting', () => {
    const later = job({ notBefore: '2026-09-20T09:00:00.000Z' });
    expect(publishStateOf([], [later], NOW).kind).toBe('SCHEDULED');
  });

  it('stops calling it scheduled once its time has passed', () => {
    // A publish scheduled for Friday whose worker was off all weekend is not
    // "scheduled" on Monday — it is waiting, and the reason nothing happened is
    // the worker, not the clock.
    const overdue = job({ notBefore: '2026-09-13T09:00:00.000Z' });
    expect(publishStateOf([], [overdue], NOW).kind).toBe('QUEUED');
  });

  it('reports the worker having it as uploading, before any publication exists', () => {
    expect(publishStateOf([], [job({ status: 'RUNNING' })], NOW).kind).toBe('UPLOADING');
  });

  it('ignores jobs that have finished', () => {
    // A COMPLETED job is not evidence of anything outstanding; the
    // publication is, and it outlives the job.
    expect(publishStateOf([], [job({ status: 'COMPLETED' })], NOW).kind).toBe('READY');
    expect(publishStateOf([], [job({ status: 'CANCELLED' })], NOW).kind).toBe('READY');
  });

  it('prefers what is on the platform over what is queued', () => {
    // Published, then queued again. The video is live; what is pending is a
    // second upload, and "queued" would hide the first.
    const state = publishStateOf([publication()], [job()], NOW);
    expect(state.kind).toBe('PUBLISHED');
    expect(state.publication?.externalUrl).toContain('vid-1');
  });

  it('surfaces a failure only when nothing is outstanding', () => {
    const failed = publication({ state: 'FAILED', externalId: null, externalUrl: null });
    expect(publishStateOf([failed], [], NOW).kind).toBe('FAILED');
    // A retry is already queued, so the failure is history, not the headline.
    expect(publishStateOf([failed], [job()], NOW).kind).toBe('QUEUED');
  });

  it('takes the newest of several attempts', () => {
    const old = publication({ id: 'pub-0', createdAt: '2026-09-01T09:00:00.000Z' });
    const recent = publication({ id: 'pub-2', createdAt: '2026-09-12T09:00:00.000Z' });
    expect(publishStateOf([old, recent], [], NOW).publication?.id).toBe('pub-2');
  });
});
