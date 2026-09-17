import { Timestamp, type DocumentData } from 'firebase/firestore';

/**
 * Read a Firestore document as the thing the contracts say it is.
 *
 * `snapshot.data() as Clip` is a cast, and a cast is a claim rather than a
 * conversion. The claim was false for every date the worker writes. The
 * contracts declare `createdAt`, `playbackExpiresAt`, `lastSeenAt` and the rest
 * as ISO strings; the worker writes through the Admin SDK with Python
 * `datetime` values, which Firestore stores as **Timestamps**. The PWA's own
 * writes really are ISO strings (`new Date().toISOString()`), so the same field
 * arrives as one type or the other depending on who last touched it —
 * `reviewedAt` a string, `playbackExpiresAt` a Timestamp, in the same document.
 *
 * Nothing ever complained, because the failure is silent by construction:
 * `Date.parse(aTimestamp)` does not throw, it returns `NaN`, and every
 * comparison against `NaN` is false. So
 *
 *   - a clip sitting in the bucket reported that it had no bucket copy
 *     (`playback.ts`, `bucketCopyLive`), which is the bug that made the review
 *     page offer "ask the worker to upload it" for clips already uploaded;
 *   - a worker that checked in seconds ago never looked stale
 *     (`worker-panel.ts`, `isStale`);
 *   - a stage that started ten minutes ago showed no elapsed time
 *     (`job-page.ts`).
 *
 * Those were read as three unrelated bugs for a while. They are one, and this
 * is it.
 *
 * Converting at this boundary rather than at each call site is the point: there
 * is exactly one place where documents become models, and on the far side of it
 * the declared types are true. Fixing `Date.parse` call sites one at a time
 * would leave the next one to be written wrong again.
 */
export function fromDocument<T>(data: DocumentData): T {
  return normalise(data) as T;
}

/**
 * Depth-first, because the dates are not all at the top level: `Job.stages[]`
 * carries `startedAt`/`endedAt` per stage, and `Job.lease` carries
 * `expiresAt`.
 */
function normalise(value: unknown): unknown {
  if (value instanceof Timestamp) {
    // ISO, not a `Date`, because that is what the contracts declare — the goal
    // is to make the cast honest, not to invent a third representation.
    return value.toDate().toISOString();
  }

  if (Array.isArray(value)) {
    return value.map(normalise);
  }

  // Plain objects only. Firestore also hands back GeoPoint, Bytes and
  // DocumentReference instances, and walking into one would rebuild it as a
  // bare object — losing the very thing that made it useful. ClipForge stores
  // none of them today, so this is guarding a future mistake rather than a
  // present one.
  if (value !== null && typeof value === 'object' && isPlainObject(value)) {
    const out: Record<string, unknown> = {};
    for (const [key, inner] of Object.entries(value)) {
      out[key] = normalise(inner);
    }
    return out;
  }

  return value;
}

function isPlainObject(value: object): boolean {
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}
