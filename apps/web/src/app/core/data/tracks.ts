import { Injectable, inject } from '@angular/core';
import type {
  GenerateMusicOptions,
  Job,
  MusicVideoOptions,
  Track,
  TrackStatus,
  Trend,
} from '@clipforge/contracts';

import { FirestoreGateway, type Live } from '../firestore/gateway';
import type { QuerySpec } from '../firestore/spec';

/**
 * Generated music, at `tracks/{id}` (docs/MUSIC-PLAN.md).
 *
 * The worker writes a track when it imports what the generator made; a person
 * listens and keeps it or not. That verdict is the only thing the rules let a
 * client write here, and it is what decides which tracks may go under a clip or
 * out as a video of their own.
 */

/** Enough for a library at this project's volume; newest first. */
export const TRACK_PAGE = 100;

export const TRACKS_QUERY: QuerySpec = {
  collection: 'tracks',
  orderBy: [['createdAt', 'desc']],
  limit: TRACK_PAGE,
};

/**
 * The latest trends, for a song about one. One `orderBy` and a bound: no
 * composite index. Thirty is the most one research run writes.
 */
export const RECENT_TRENDS_QUERY: QuerySpec = {
  collection: 'trends',
  orderBy: [['createdAt', 'desc']],
  limit: 30,
};

/** A sound to start from. Words only: never an artist (docs/MUSIC-PLAN.md). */
export interface SongStyle {
  readonly name: string;
  readonly style: string;
}

export const SONG_STYLES: readonly SongStyle[] = [
  { name: 'Amapiano', style: 'amapiano, deep log drum bass, shakers, warm piano chords, soulful' },
  {
    name: 'Afrobeats',
    style: 'afrobeats, bouncy percussion, bright guitar licks, warm bass, sunny',
  },
  { name: 'Pop', style: 'upbeat pop, catchy hook, bright synths, punchy drums' },
  { name: 'Hip-hop', style: 'boom bap hip hop, deep bass, crisp snare, jazzy chords' },
  { name: 'Gospel', style: 'uplifting gospel, choir backing vocals, organ, handclaps, soulful' },
  { name: 'Acoustic', style: 'acoustic guitar ballad, warm and intimate, light percussion' },
  { name: 'Lo-fi', style: 'lo-fi hip hop, mellow Rhodes piano, vinyl crackle, laid-back drums' },
  { name: 'Cinematic', style: 'cinematic, strings and piano, building, big drums' },
];

/**
 * The song job a client is allowed to write. Mirrors `GENERATE_MUSIC_PIPELINE`:
 * LYRICS then SING, both on the GPU lane. The rules check the options: the
 * words' source must be there, and the rest is bounded.
 */
export function newGenerateMusicJob(
  id: string,
  uid: string,
  options: GenerateMusicOptions,
  now: string,
) {
  return {
    id,
    uid,
    type: 'GENERATE_MUSIC',
    status: 'QUEUED',
    submission: null,
    sourceId: null,
    generateMusicOptions: options,
    stages: [
      { name: 'LYRICS', lane: 'GPU', status: 'PENDING' },
      { name: 'SING', lane: 'GPU', status: 'PENDING' },
    ],
    workerId: null,
    leaseExpiresAt: null,
    attempts: 0,
    maxAttempts: 2,
    error: null,
    createdAt: now,
    updatedAt: now,
    startedAt: null,
    endedAt: null,
  } satisfies Job;
}

/** What one Keep or Discard writes. Exactly the fields the rules allow. */
export interface TrackDecision {
  readonly status: TrackStatus;
  readonly decidedBy: string;
  readonly decidedAt: string;
}

export function trackDecision(
  uid: string,
  status: TrackStatus,
  at: Date = new Date(),
): TrackDecision {
  return { status, decidedBy: uid, decidedAt: at.toISOString() };
}

/**
 * Where the worker's file sits inside its workspace, as the loopback file
 * server wants it: `music/generated/trk_….wav`. Null when the path is not
 * under a `music` folder, which the server would refuse anyway.
 */
export function workspaceKey(localPath: string): string | null {
  const normalised = localPath.split('\\').join('/');
  const index = normalised.lastIndexOf('/music/');
  return index === -1 ? null : normalised.slice(index + 1);
}

/**
 * The music video job a client is allowed to write. Mirrors
 * `MUSIC_VIDEO_PIPELINE`: one stage, VISUALIZE, on the CPU lane. The rules
 * accept it only for a track that was kept.
 */
export function newMusicVideoJob(id: string, uid: string, options: MusicVideoOptions, now: string) {
  return {
    id,
    uid,
    type: 'MUSIC_VIDEO',
    status: 'QUEUED',
    submission: null,
    sourceId: null,
    musicVideoOptions: options,
    stages: [{ name: 'VISUALIZE', lane: 'CPU', status: 'PENDING' }],
    workerId: null,
    leaseExpiresAt: null,
    attempts: 0,
    maxAttempts: 2,
    error: null,
    createdAt: now,
    updatedAt: now,
    startedAt: null,
    endedAt: null,
  } satisfies Job;
}

@Injectable({ providedIn: 'root' })
export class TracksRepository {
  private readonly db = inject(FirestoreGateway);

  tracks(): Live<Track[]> {
    return this.db.live<Track>(TRACKS_QUERY);
  }

  recentTrends(): Live<Trend[]> {
    return this.db.live<Trend>(RECENT_TRENDS_QUERY);
  }

  /** A fresh id for an upload's folder under `notes/{uid}/`. */
  newUploadId(): string {
    return this.db.newId('jobs');
  }

  /** Ask the PC for a new song. Its takes arrive here as NEW tracks. */
  async makeSong(uid: string, options: GenerateMusicOptions): Promise<string> {
    const id = this.db.newId('jobs');
    await this.db.create(
      'jobs',
      { ...newGenerateMusicJob(id, uid, options, new Date().toISOString()) },
      id,
    );
    return id;
  }

  /** Ask the PC to draw a kept track's videos: a full one and a Short. */
  async makeVideos(trackId: string, uid: string): Promise<string> {
    const id = this.db.newId('jobs');
    await this.db.create(
      'jobs',
      {
        ...newMusicVideoJob(
          id,
          uid,
          { trackId, full: true, short: true },
          new Date().toISOString(),
        ),
      },
      id,
    );
    return id;
  }

  async decide(trackId: string, uid: string, status: TrackStatus): Promise<void> {
    await this.db.update('tracks', trackId, { ...trackDecision(uid, status) });
  }
}
