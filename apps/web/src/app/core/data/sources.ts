import { Injectable, computed, inject } from '@angular/core';
import type { ObscureOptions, Source, SourceKind, SourcePreview } from '@clipforge/contracts';

import { FirestoreGateway, type Live } from '../firestore/gateway';
import type { QuerySpec } from '../firestore/spec';

/**
 * Sources: the long-form videos and music tracks everything else is cut from.
 *
 * A source is the root of a tree — clips and candidates both carry `sourceId` —
 * so this is also where deleting one has to answer for its children. Firestore
 * has no "delete by query" and its rules see one document at a time, which is
 * why the cascade is application code and not a rule.
 */

/**
 * Firestore's own ceiling on a batched write. Paging at this size means one
 * enormous source needs no different code path from a small one — it just takes
 * more trips.
 */
const BATCH_LIMIT = 500;

/**
 * How many sources one listener delivers.
 *
 * Exported because a list that is showing fewer sources than exist has to say
 * so, and it can only know that by comparing what arrived against this.
 */
export const SOURCE_PAGE = 50;

/**
 * Every source, newest first.
 *
 * Ordered by `createdAt` rather than by `useCount`, which is what the contract
 * says the Sources list sorts by. Firestore's `orderBy` drops any document
 * missing the field, and `useCount` was added this week: sorting on it in the
 * query would silently omit every source written before then. `createdAt` is
 * required on a Source, so nothing can go missing. A page that wants the
 * contract's order can sort the delivered page, where an absent count is a zero
 * rather than a disappearance.
 *
 * One spec for every kind, so the gate's cache gives every caller the same
 * listener however they intend to filter it. See {@link SourcesRepository.listSources}.
 */
export function sourcesSpec(pageSize: number = SOURCE_PAGE): QuerySpec {
  return { collection: 'sources', orderBy: [['createdAt', 'desc']], limit: pageSize };
}

/**
 * Everything cut from one source.
 *
 * `limit` is left off when the spec is going to be counted: an aggregation
 * returns a number rather than documents, so there is nothing for a bound to
 * hold back and one read pays for the answer whatever the size of the result.
 */
export function ofSourceSpec(
  collection: 'clips' | 'candidates',
  sourceId: string,
  limit?: number,
): QuerySpec {
  return { collection, where: [['sourceId', '==', sourceId]], limit };
}

/**
 * What a source is, including the ones that never said.
 *
 * Null and absent both read as video, which is what every source written before
 * the field existed was. Worth a function rather than a `??` at each call site
 * because the alternative is a Firestore `where('kind', '==', 'video')`, and
 * that matches on the stored field: it would quietly return none of the older
 * library at all.
 */
export function sourceKind(source: Source): SourceKind {
  return source.kind ?? 'video';
}

export function sourcesOfKind(sources: readonly Source[], kind: SourceKind): Source[] {
  return sources.filter((source) => sourceKind(source) === kind);
}

@Injectable({ providedIn: 'root' })
export class SourcesRepository {
  private readonly db = inject(FirestoreGateway);

  // ── Reading ────────────────────────────────────────────────────────────────

  /**
   * The library, optionally narrowed to one kind.
   *
   * The kind is applied here and not in the query, for the reason
   * {@link sourceKind} gives: an older source has no `kind` field and a
   * Firestore equality filter cannot match one that is absent. Filtering the
   * delivered page also keeps this off a composite index, and costs nothing
   * extra — every caller shares the one listener {@link sourcesSpec} names, so
   * asking for music and asking for video is one query, not two.
   *
   * The trade is that {@link SOURCE_PAGE} bounds the page before the filter, so
   * a library dominated by one kind can deliver few of the other. Acceptable
   * while a workspace holds tens of sources on one disk; if that stops being
   * true, the fix is a backfill giving every source a `kind`, then the filter
   * moves into the spec.
   */
  listSources(kind?: SourceKind): Live<Source[]> {
    const all = this.db.live<Source>(sourcesSpec());
    if (kind === undefined) return all;
    return {
      // `null` survives the filter as `null`. It means "nothing has arrived
      // yet", and turning it into an empty array here would hand the page back
      // the conflation the three states exist to retire.
      data: computed(() => {
        const sources = all.data();
        return sources === null ? null : sourcesOfKind(sources, kind);
      }),
      loading: all.loading,
      error: all.error,
      release: all.release,
    };
  }

  /**
   * A source's thumbnail, fetched per row rather than with the list.
   *
   * The same shape as a clip's poster and the same reason: ~20 KB of base64
   * each, and a listener that carried them would pay for every one of them
   * again on every reconnect. The list arrives first and the pictures fill in.
   */
  async loadPreview(sourceId: string): Promise<SourcePreview | null> {
    return this.db.onceDoc<SourcePreview>(`sources/${sourceId}/preview`, 'poster');
  }

  /** What a job's submission resolved to, once ingestion has worked it out. */
  async loadSource(sourceId: string): Promise<Source | null> {
    return this.db.onceDoc<Source>('sources', sourceId);
  }

  /**
   * What deleting a source would take with it.
   *
   * Asked before the confirmation rather than after, because "delete this
   * source" and "delete this source, eleven clips and forty candidates" are
   * different decisions and only one of them was offered.
   */
  async sourceFootprint(sourceId: string): Promise<{ clips: number; candidates: number }> {
    const [clips, candidates] = await Promise.all([
      this.db.count(ofSourceSpec('clips', sourceId)),
      this.db.count(ofSourceSpec('candidates', sourceId)),
    ]);
    return { clips, candidates };
  }

  // ── Writing ────────────────────────────────────────────────────────────────

  /**
   * Make a set of rectangles a property of the channel rather than of one clip.
   *
   * This is the whole difference between a feature and a chore. A broadcaster's
   * bug is in the same place on every video it will ever publish, so a reviewer
   * who has to ask for it on each clip is doing the system's bookkeeping. Once
   * this is set, RENDER applies it as it cuts and the clip arrives clean.
   *
   * `null` clears it. The write is a single field — firestore.rules pins it to
   * exactly that name, because everything else on a source describes a file on
   * the worker.
   */
  async rememberObscure(sourceId: string, obscure: ObscureOptions | null): Promise<void> {
    await this.db.update('sources', sourceId, { obscure });
  }

  // ── Tidying up ─────────────────────────────────────────────────────────────
  //
  // **Deleting a record never touches a file.** The two are separate acts
  // because they answer separate questions: whether a clip belongs in the
  // review queue is decided dozens of times a day and is cheap to get wrong,
  // and whether the 400 MB behind it is still wanted is decided rarely and is
  // expensive to get wrong. Removing media lives on the worker's local API,
  // reachable only from the machine holding it — see `LocalApiService`.

  /**
   * Forget a source and everything cut from it.
   *
   * The cascade is here rather than in the rules because rules see one document
   * at a time and cannot express "and its children".
   *
   * Each page used to commit as one Firestore batch, which made a page close to
   * atomic. The gate's `writeAll` carries sets and updates and not deletes, so
   * each document now goes on its own. The recovery story is the part that
   * mattered and it is unchanged: a failure part-way leaves a partly-cleared
   * tree rather than a half-written document, and running it again finishes the
   * job. Running it again is possible because the source document goes last —
   * until it does, the source is still in the library to be asked a second time.
   *
   * Returns how much went, so the confirmation can be answered with a fact.
   */
  async deleteSource(sourceId: string): Promise<{ clips: number; candidates: number }> {
    const removed = { clips: 0, candidates: 0 };

    for (const name of ['clips', 'candidates'] as const) {
      // A page at a time. The size is Firestore's old batch ceiling and is kept
      // for what it still buys: one enormous source needs no different code
      // path from a small one, and no more than 500 deletes are ever in flight.
      for (;;) {
        // Only the id is wanted. Reading the documents at all is unavoidable —
        // Firestore has no delete-by-query — so this is the same read the batch
        // version paid for, asked for the one field it uses.
        const page = await this.db.once<{ id: string }>(
          ofSourceSpec(name, sourceId, BATCH_LIMIT),
        );
        if (page.length === 0) break;
        await Promise.all(page.map((document) => this.db.remove(name, document.id)));
        removed[name] += page.length;
        if (page.length < BATCH_LIMIT) break;
      }
    }

    await this.db.remove('sources', sourceId);
    return removed;
  }
}
