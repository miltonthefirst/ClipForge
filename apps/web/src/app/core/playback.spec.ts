import { TestBed } from '@angular/core/testing';
import type { Clip } from '@clipforge/contracts';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { CLIPFORGE_CONFIG, FirebaseService } from './firebase';
import { PlaybackService } from './playback';
import { loadConfig } from '../../environments';

/**
 * Playback resolution is the one piece of product logic the free-tier decision
 * introduced, and the one thing that must keep working unchanged when Blaze is
 * enabled. See docs/adr/0009-spark-tier-local-artefacts.md.
 */
function clip(overrides: Partial<Clip> = {}): Clip {
  return {
    id: 'clip-1',
    uid: 'user-1',
    candidateId: 'cand-1',
    location: 'LOCAL',
    localPath: 'P:\\workspace\\clips\\user-1\\clip-1.mp4',
    playbackUrl: null,
    review: 'PENDING',
    createdAt: '2026-09-08T12:00:00Z',
    ...overrides,
  } as Clip;
}

describe('PlaybackService', () => {
  let service: PlaybackService;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        { provide: CLIPFORGE_CONFIG, useValue: loadConfig() },
        // Never reached by these cases: every one resolves before the bucket
        // branch, which is the precedence they exist to pin down.
        { provide: FirebaseService, useValue: { storage: {} } },
      ],
    });
    service = TestBed.inject(PlaybackService);
  });

  it('prefers a remote URL, which is what enabling Blaze populates', async () => {
    const source = await service.resolve(
      clip({ playbackUrl: 'https://storage.example/clip.mp4', location: 'REMOTE' }),
      true,
    );
    expect(source).toEqual({ kind: 'remote', url: 'https://storage.example/clip.mp4' });
  });

  it('falls back to the local server when the PWA runs on the worker machine', async () => {
    const source = await service.resolve(clip(), true);
    expect(source.kind).toBe('local');
    expect(source.kind === 'local' && source.url).toContain('/clips/user-1/clip-1.mp4');
  });

  it('translates Windows paths into a URL the file server understands', async () => {
    const source = await service.resolve(clip(), true);
    expect(source.kind === 'local' && source.url).not.toContain('\\');
  });

  it('falls back to the poster when nothing can play the file', async () => {
    expect(await service.resolve(clip(), false)).toEqual({ kind: 'poster' });
  });

  it('prefers remote even when the local server is also reachable', async () => {
    // Otherwise a clip would play from the worker on the machine that rendered
    // it and from Storage everywhere else — two code paths for one clip.
    const source = await service.resolve(
      clip({ playbackUrl: 'https://storage.example/clip.mp4' }),
      true,
    );
    expect(source.kind).toBe('remote');
  });

  it('prefers the local server over the bucket when both could serve it', async () => {
    // On the machine that rendered the clip the local file is faster and costs
    // no egress; the bucket exists for the devices that have no local file.
    const source = await service.resolve(
      clip({
        storagePath: 'clips/user-1/clip-1.mp4',
        playbackExpiresAt: '2099-01-01T00:00:00Z',
      }),
      true,
    );
    expect(source.kind).toBe('local');
  });

  it('ignores a bucket copy whose retention window has passed', async () => {
    // The lifecycle rule deletes the object and tells nobody, so an expired
    // clip must fall through here rather than fail when someone presses play.
    const source = await service.resolve(
      clip({
        storagePath: 'clips/user-1/clip-1.mp4',
        playbackExpiresAt: '2020-01-01T00:00:00Z',
      }),
      false,
    );
    expect(source).toEqual({ kind: 'poster' });
  });

  it('knows when a bucket copy is still live', () => {
    expect(service.bucketCopyLive(clip())).toBe(false);
    expect(
      service.bucketCopyLive(
        clip({ storagePath: 'clips/user-1/clip-1.mp4', playbackExpiresAt: '2099-01-01T00:00:00Z' }),
      ),
    ).toBe(true);
    expect(
      service.bucketCopyLive(
        clip({ storagePath: 'clips/user-1/clip-1.mp4', playbackExpiresAt: '2020-01-01T00:00:00Z' }),
      ),
    ).toBe(false);
  });

  it('gives up on a local path it cannot map into the workspace', async () => {
    const source = await service.resolve(clip({ localPath: '/somewhere/else/file.mp4' }), true);
    expect(source).toEqual({ kind: 'poster' });
  });

  it('probes the local server only once per session', async () => {
    // Asked per clip; a failed fetch per card would be slow and noisy.
    const fetchImpl = vi.fn().mockResolvedValue(new Response(null, { status: 404 }));
    await service.probeLocalServer(fetchImpl as unknown as typeof fetch);
    await service.probeLocalServer(fetchImpl as unknown as typeof fetch);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it('treats an unreachable server as simply unavailable', async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new Error('connection refused'));
    expect(await service.probeLocalServer(fetchImpl as unknown as typeof fetch)).toBe(false);
  });
});
