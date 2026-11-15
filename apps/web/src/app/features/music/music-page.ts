import { DecimalPipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  computed,
  inject,
  signal,
} from '@angular/core';
import type { Track, TrackStatus } from '@clipforge/contracts';

import { TracksRepository, workspaceKey } from '../../core/data/tracks';
import { CLIPFORGE_CONFIG } from '../../core/firebase';
import { HomeRouteService } from '../../core/home-route';
import { PlaybackService } from '../../core/playback';
import { SessionService } from '../../core/session';

/** Where the track that is playing came from. */
interface Playing {
  readonly id: string;
  readonly url: string;
  readonly from: 'this PC' | 'the PC, over the home wifi';
}

/**
 * Generated music: listen, then keep or discard (docs/MUSIC-PLAN.md).
 *
 * A track has no cloud copy. It plays from the PC itself — through the
 * loopback file server when this app is open on the PC, or over the home wifi
 * from a phone (docs/HOME-NETWORK-PLAN.md). Anywhere else the page says so
 * rather than offering a play button that cannot work.
 */
@Component({
  selector: 'app-music-page',
  imports: [DecimalPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './music-page.html',
})
export class MusicPage implements OnDestroy {
  private readonly repository = inject(TracksRepository);
  private readonly session = inject(SessionService);
  private readonly homeRoute = inject(HomeRouteService);
  private readonly playback = inject(PlaybackService);
  private readonly config = inject(CLIPFORGE_CONFIG);

  private readonly live = this.repository.tracks();
  protected readonly tracks = this.live.data;
  protected readonly loadError = this.live.error;
  protected readonly atHome = this.homeRoute.home;
  protected readonly routeOffered = computed(() => this.homeRoute.url() !== null);

  protected readonly playing = signal<Playing | null>(null);
  protected readonly opening = signal<string | null>(null);
  protected readonly notes = signal<Record<string, string>>({});
  protected readonly deciding = signal<string | null>(null);

  protected readonly counts = computed(() => {
    const all = this.tracks() ?? [];
    return {
      total: all.length,
      kept: all.filter((track) => track.status === 'KEPT').length,
      waiting: all.filter((track) => track.status === 'NEW').length,
    };
  });

  ngOnDestroy(): void {
    this.live.release();
  }

  protected async play(track: Track): Promise<void> {
    if (this.playing()?.id === track.id) return;
    this.opening.set(track.id);
    this.note(track.id, null);
    try {
      // The PC itself first: no route, no copy, nothing in between.
      const key = workspaceKey(track.localPath);
      if (key && (await this.playback.probeLocalServer())) {
        this.playing.set({
          id: track.id,
          url: `${this.config.localServerOrigin}/${key}`,
          from: 'this PC',
        });
        return;
      }
      const url = await this.homeRoute.mediaUrl('track', track.id);
      if (url) {
        this.playing.set({ id: track.id, url, from: 'the PC, over the home wifi' });
        return;
      }
      this.note(
        track.id,
        this.atHome()
          ? 'The PC answered, but does not have this file any more.'
          : this.routeOffered()
            ? 'Tracks play from the PC. Join the home wifi, or open the desktop app on the PC.'
            : 'Tracks play from the PC. Open the desktop app there, or ask an admin to switch the home wifi route on (Worker page).',
      );
    } finally {
      this.opening.set(null);
    }
  }

  /** Keep or Discard; pressing the verdict a track already has undoes it. */
  protected async decide(track: Track, status: Exclude<TrackStatus, 'NEW'>): Promise<void> {
    const uid = this.session.uid;
    if (!uid) return;
    this.deciding.set(track.id);
    try {
      await this.repository.decide(track.id, uid, track.status === status ? 'NEW' : status);
    } catch (error) {
      this.note(track.id, error instanceof Error ? error.message : String(error));
    } finally {
      this.deciding.set(null);
    }
  }

  protected statusLabel(status: TrackStatus): string {
    return status === 'KEPT' ? 'Kept' : status === 'DISCARDED' ? 'Discarded' : 'Not decided';
  }

  private note(id: string, message: string | null): void {
    const next = { ...this.notes() };
    if (message) next[id] = message;
    else delete next[id];
    this.notes.set(next);
  }
}
