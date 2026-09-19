/**
 * ClipForge category catalogue — GENERATED FILE, DO NOT EDIT.
 *
 * Source of truth: packages/contracts/data/categories.json
 * Regenerate with: npm --prefix packages/contracts run generate
 *
 * Editing this file by hand is pointless: CI regenerates it and fails on any
 * difference. Change the catalogue instead.
 */

/** One entry of the catalogue. `code` is what travels on `ResearchOptions.category`. */
export interface Category {
  readonly code: string;
  readonly label: string;
  readonly group: string;
  /** Appended to a feed phrase when videos are looked up for it. */
  readonly hint: string;
  /** Other words a person might search the list by. */
  readonly aliases: readonly string[];
  /** Searched on YouTube when a run has no topics of its own. */
  readonly terms: readonly string[];
  /** Read when a run names no subreddits of its own. */
  readonly subreddits: readonly string[];
}

export declare const CATEGORIES: readonly Category[];
