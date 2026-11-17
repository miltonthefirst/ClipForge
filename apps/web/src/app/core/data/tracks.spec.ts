import { describe, expect, it } from 'vitest';

import {
  SONG_STYLES,
  TRACKS_QUERY,
  newGenerateMusicJob,
  newMusicVideoJob,
  trackDecision,
  workspaceKey,
} from './tracks';

describe('tracks', () => {
  it('maps a worker path to what the loopback file server serves', () => {
    expect(
      workspaceKey('P:\\ClipForge\\apps\\worker\\workspace\\music\\generated\\trk_1.wav'),
    ).toBe('music/generated/trk_1.wav');
    expect(workspaceKey('/home/x/workspace/music/generated/trk_1.flac')).toBe(
      'music/generated/trk_1.flac',
    );
  });

  it('refuses a path that is not under a music folder', () => {
    expect(workspaceKey('P:\\workspace\\clips\\job\\clip.mp4')).toBeNull();
  });

  it('writes exactly the fields the rules allow for a verdict', () => {
    const at = new Date('2026-11-15T13:00:00Z');
    expect(trackDecision('alice', 'KEPT', at)).toEqual({
      status: 'KEPT',
      decidedBy: 'alice',
      decidedAt: '2026-11-15T13:00:00.000Z',
    });
  });

  it('listens to a bounded page, newest first', () => {
    expect(TRACKS_QUERY.limit).toBeGreaterThan(0);
    expect(TRACKS_QUERY.orderBy).toEqual([['createdAt', 'desc']]);
  });

  it('builds the one job the rules let a client write for a kept track', () => {
    const job = newMusicVideoJob('j1', 'alice', { trackId: 'trk_1' }, '2026-11-17T00:00:00.000Z');
    expect(job.type).toBe('MUSIC_VIDEO');
    expect(job.stages).toEqual([{ name: 'VISUALIZE', lane: 'CPU', status: 'PENDING' }]);
    expect(job.musicVideoOptions).toEqual({ trackId: 'trk_1' });
    expect(job.attempts).toBe(0);
  });

  it('builds the song job the worker runs: write, then sing, both on the GPU', () => {
    const job = newGenerateMusicJob(
      'j2',
      'alice',
      { lyricsFrom: 'IDEA', style: 'amapiano', idea: 'a slow Sunday' },
      '2026-11-17T00:00:00.000Z',
    );
    expect(job.type).toBe('GENERATE_MUSIC');
    expect(job.stages.map((stage) => [stage.name, stage.lane])).toEqual([
      ['LYRICS', 'GPU'],
      ['SING', 'GPU'],
    ]);
    expect(job.generateMusicOptions.idea).toBe('a slow Sunday');
  });

  it('offers styles in words, within what the rules accept', () => {
    for (const style of SONG_STYLES) {
      expect(style.style.length).toBeLessThanOrEqual(200);
      expect(style.style).not.toMatch(/\bstyle of\b|\blike\b/i);
    }
  });
});
