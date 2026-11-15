import { Injectable, inject } from '@angular/core';
import type { Track, TrackStatus } from '@clipforge/contracts';

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

@Injectable({ providedIn: 'root' })
export class TracksRepository {
  private readonly db = inject(FirestoreGateway);

  tracks(): Live<Track[]> {
    return this.db.live<Track>(TRACKS_QUERY);
  }

  async decide(trackId: string, uid: string, status: TrackStatus): Promise<void> {
    await this.db.update('tracks', trackId, { ...trackDecision(uid, status) });
  }
}
