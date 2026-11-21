import type { Track } from '@clipforge/contracts';

/**
 * The Music page's tabs: the library grouped by what a person decided, so
 * the takes still to listen to are not buried under the kept ones.
 */

export type TrackTab = 'NEW' | 'KEPT' | 'DISCARDED' | 'ALL';

export const TRACK_TABS: readonly { readonly tab: TrackTab; readonly label: string }[] = [
  { tab: 'NEW', label: 'To listen to' },
  { tab: 'KEPT', label: 'Kept' },
  { tab: 'DISCARDED', label: 'Discarded' },
  { tab: 'ALL', label: 'All' },
];

export function tracksIn(tracks: readonly Track[], tab: TrackTab): Track[] {
  return tab === 'ALL' ? [...tracks] : tracks.filter((track) => track.status === tab);
}

export function tabCounts(tracks: readonly Track[]): Record<TrackTab, number> {
  const counts: Record<TrackTab, number> = { NEW: 0, KEPT: 0, DISCARDED: 0, ALL: tracks.length };
  for (const track of tracks) counts[track.status] += 1;
  return counts;
}

/**
 * The tab to open on: the one chosen last time if it still has tracks, else
 * the takes waiting to be listened to, else the kept ones, else everything.
 */
export function openingTab(tracks: readonly Track[], remembered: string | null): TrackTab {
  const counts = tabCounts(tracks);
  const known = TRACK_TABS.find((entry) => entry.tab === remembered)?.tab;
  if (known && counts[known] > 0) return known;
  if (counts.NEW > 0) return 'NEW';
  if (counts.KEPT > 0) return 'KEPT';
  return 'ALL';
}
