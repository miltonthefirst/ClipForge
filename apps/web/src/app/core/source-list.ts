import type { Source } from '@clipforge/contracts';

/**
 * Ordering and describing the library.
 *
 * Pure, and in `core/` rather than in the page, because this is the part worth
 * testing: "which of these have I actually used" is the question the page
 * exists to answer, and getting the order wrong is the kind of mistake that
 * looks like a working list.
 */

export type SourceTab = 'all' | 'video' | 'music';

export interface SourceTabDef {
  readonly key: SourceTab;
  readonly label: string;
}

export const SOURCE_TABS: readonly SourceTabDef[] = [
  { key: 'all', label: 'Everything' },
  { key: 'video', label: 'Video' },
  { key: 'music', label: 'Music' },
];

export function sourceTab(raw: string | null | undefined): SourceTab {
  return SOURCE_TABS.some((tab) => tab.key === raw) ? (raw as SourceTab) : 'all';
}

/**
 * Most-used first, and among equals the most recently touched.
 *
 * The reviewer asked for "sorted by how many times I have used them", and the
 * tie-break is what makes that useful rather than merely true: a fresh library
 * is all zeroes and ones, so without a second key the order would be whatever
 * Firestore happened to return and would change under them between visits.
 */
export function byUse(sources: readonly Source[]): Source[] {
  return [...sources].sort((a, b) => {
    const used = (b.useCount ?? 0) - (a.useCount ?? 0);
    if (used !== 0) return used;
    return String(b.lastAccessedAt ?? b.createdAt).localeCompare(
      String(a.lastAccessedAt ?? a.createdAt),
    );
  });
}

/**
 * Whether the collector may take this file back.
 *
 * Mirrors the worker: `pinned` by hand, or used more than once. Shown because
 * "why is this still here / why did that go" is otherwise invisible, and a
 * library you cannot predict is one you keep re-downloading.
 */
export function isKept(source: Source): boolean {
  return Boolean(source.pinned) || (source.useCount ?? 0) > 1;
}

/** A size a person can read, from bytes. */
export function readableSize(bytes: number | null | undefined): string | null {
  if (!bytes || bytes < 0) return null;
  const mb = bytes / 1_048_576;
  if (mb < 1) return `${Math.round(bytes / 1024)} KB`;
  if (mb < 1024) return `${mb < 10 ? mb.toFixed(1) : Math.round(mb)} MB`;
  return `${(mb / 1024).toFixed(1)} GB`;
}

/** A duration a person can read, from seconds. */
export function readableLength(seconds: number | null | undefined): string | null {
  if (!seconds || seconds <= 0) return null;
  const whole = Math.round(seconds);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const rest = whole % 60;
  const pad = (n: number) => String(n).padStart(2, '0');
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(rest)}` : `${minutes}:${pad(rest)}`;
}

/**
 * What to show where a thumbnail would be, when there is not one.
 *
 * A letter from the title rather than a generic icon, so a list of them is
 * still scannable — and rather than a generated waveform, which identifies
 * nothing and only looks like it does.
 */
export function initial(source: Source): string {
  const name = (source.title ?? source.externalId ?? '?').trim();
  return (name[0] ?? '?').toUpperCase();
}

/**
 * How many uses, in words, because "1 use" and "used once" read differently in
 * a column of numbers and only one of them is a sentence.
 */
export function describeUse(source: Source): string {
  const count = source.useCount ?? 0;
  if (count === 0) return 'never used';
  if (count === 1) return 'used once';
  return `used ${count} times`;
}
