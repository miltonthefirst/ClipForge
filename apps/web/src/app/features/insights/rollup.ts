import type { MetricSnapshot } from '@clipforge/contracts';

/**
 * Folding daily snapshots into something a screen can draw.
 *
 * Separated from the component for the same reason the worker separates its
 * arithmetic from its store: these rules are where a dashboard silently lies.
 * Summing a field that is already an average, or normalising each curve to its
 * own maximum, produces a chart that renders perfectly and means nothing — and
 * neither mistake is visible in a screenshot.
 */

/** One clip's lifetime totals, folded down from its daily snapshots. */
export interface PublicationRollup {
  readonly publicationId: string;
  readonly clipId: string;
  readonly externalId: string;
  readonly views: number;
  readonly likes: number;
  readonly comments: number;
  readonly days: number;
  readonly viewPercentage: number | null;
  readonly curve: readonly CurvePoint[];
}

export interface CurvePoint {
  readonly x: number;
  readonly y: number;
}

/** A retention curve as an SVG polyline, plus the number worth reading off it. */
export interface CurvePath {
  readonly publicationId: string;
  readonly points: string;
  readonly midpoint: number | null;
}

/**
 * Snapshots to one row per publication, most viewed first.
 *
 * **Views sum; percentages do not.** The API reports views per day, so lifetime
 * views is their sum. `averageViewPercentage` is already an average over the
 * video's life, so the latest snapshot carrying one wins — averaging the
 * sequence would weight the first sparse day as heavily as the settled ones,
 * and would drift further from the truth the longer a clip was tracked.
 *
 * The retention curve likewise comes from the most recent snapshot that has
 * one, because an absent curve means "withheld so far", not "flat".
 */
export function rollUp(snapshots: readonly MetricSnapshot[]): PublicationRollup[] {
  const byPublication = new Map<string, MetricSnapshot[]>();
  for (const snapshot of snapshots) {
    const held = byPublication.get(snapshot.publicationId);
    if (held) held.push(snapshot);
    else byPublication.set(snapshot.publicationId, [snapshot]);
  }

  const rows: PublicationRollup[] = [];
  for (const [publicationId, group] of byPublication) {
    const ordered = [...group].sort((a, b) => a.date.localeCompare(b.date));
    const latest = ordered[ordered.length - 1]!;

    let viewPercentage: number | null = null;
    let curve: readonly CurvePoint[] = [];
    for (let i = ordered.length - 1; i >= 0; i -= 1) {
      const candidate = ordered[i]!;
      if (viewPercentage === null && candidate.averageViewPercentage != null) {
        viewPercentage = candidate.averageViewPercentage;
      }
      if (curve.length === 0 && candidate.retention && candidate.retention.length > 0) {
        curve = candidate.retention.map((point) => ({
          x: point.elapsedRatio,
          y: point.audienceWatchRatio,
        }));
      }
    }

    rows.push({
      publicationId,
      clipId: latest.clipId,
      externalId: latest.externalId,
      views: ordered.reduce((total, s) => total + (s.views ?? 0), 0),
      likes: ordered.reduce((total, s) => total + (s.likes ?? 0), 0),
      comments: ordered.reduce((total, s) => total + (s.comments ?? 0), 0),
      days: ordered.length,
      viewPercentage,
      curve,
    });
  }
  return rows.sort((a, b) => b.views - a.views);
}

/**
 * Audience still watching halfway through, interpolated.
 *
 * Interpolated rather than nearest-sampled, matching the worker exactly:
 * YouTube's curve resolution varies with video length, so "the sample nearest
 * the middle" is a different distance from the middle for a 20-second clip than
 * for a 60-second one, and comparing those compares two different questions.
 */
export function retentionAtHalf(curve: readonly CurvePoint[]): number | null {
  if (curve.length === 0) return null;
  const ordered = [...curve].sort((a, b) => a.x - b.x);
  if (ordered[0]!.x >= 0.5) return ordered[0]!.y;
  for (let i = 0; i < ordered.length - 1; i += 1) {
    const left = ordered[i]!;
    const right = ordered[i + 1]!;
    if (left.x <= 0.5 && 0.5 <= right.x) {
      if (right.x === left.x) return left.y;
      return left.y + ((0.5 - left.x) / (right.x - left.x)) * (right.y - left.y);
    }
  }
  return ordered[ordered.length - 1]!.y;
}

/**
 * Curves as SVG polylines on one shared vertical scale.
 *
 * **One scale for every curve**, so two drawn side by side can be compared.
 * Normalising each to its own maximum would make every clip look identically
 * well retained, which is the exact opposite of what the screen is for.
 *
 * The ceiling never drops below 1. A clip nobody finished should read as a
 * curve falling away down the box, not as one rescaled to fill it.
 */
export function toCurves(
  rollups: readonly PublicationRollup[],
  width: number,
  height: number,
): CurvePath[] {
  const rows = rollups.filter((row) => row.curve.length > 1);
  if (rows.length === 0) return [];

  const ceiling = Math.max(1, ...rows.flatMap((row) => row.curve.map((point) => point.y)));

  return rows.map((row) => ({
    publicationId: row.publicationId,
    points: row.curve
      .map((point) => {
        const x = point.x * width;
        const y = height - (point.y / ceiling) * height;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(' '),
    midpoint: retentionAtHalf(row.curve),
  }));
}
