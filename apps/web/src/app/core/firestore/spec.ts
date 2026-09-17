/**
 * A query, described rather than built.
 *
 * ## Why a description and not a `Query`
 *
 * Because a `Query` can only be made by importing Firestore's builders, and the
 * moment a repository imports those, the gate is a convention rather than a
 * boundary. Describing the query as data means `core/firestore` is the only
 * place in the app that knows what `where` or `orderBy` are — which is a rule a
 * linter can enforce and a habit cannot.
 *
 * ## Why it also fixes the cache
 *
 * The shared-listener cache is keyed, and the one way a cache like it fails
 * while still looking like working software is two different queries sharing a
 * key. Hand-written keys made that a thing to remember: `jobs:RUNNING,QUEUED`
 * was correct only because somebody kept it in step with the filter beside it.
 *
 * A described query can be its own key. {@link cacheKey} is a total function of
 * the spec, so two specs collide only if they genuinely are the same query, and
 * a filter that changes changes the key without anyone deciding to.
 */

/** The operators the app actually uses. Deliberately not all of Firestore's. */
export type WhereOp = '==' | '!=' | 'in' | 'not-in' | '<' | '<=' | '>' | '>=' | 'array-contains';

export type WhereClause = readonly [field: string, op: WhereOp, value: unknown];

export interface QuerySpec {
  /** A collection path. Subcollections included: `clips/abc/preview`. */
  readonly collection: string;
  readonly where?: readonly WhereClause[];
  readonly orderBy?: readonly (readonly [field: string, direction?: 'asc' | 'desc'])[];
  /**
   * Bounded by default is not enforced here, but every caller should mean it.
   * Firestore bills a read per delivered document and re-delivers the whole
   * result set on reconnect, so an unbounded listen is the easiest way to spend
   * a day's quota on one page nobody is looking at.
   */
  readonly limit?: number;
}

/**
 * A stable string for one query, suitable as a cache key.
 *
 * Stable across key order and across the same filters written in a different
 * sequence, because two callers describing the same query in different orders
 * are asking the same question and should share the listener. Values are
 * JSON-encoded, so a filter on the string "1" and one on the number 1 do not
 * collide — Firestore treats those as different, and so must this.
 */
export function cacheKey(spec: QuerySpec): string {
  const wheres = [...(spec.where ?? [])]
    .map(([field, op, value]) => `${field}${op}${JSON.stringify(value ?? null)}`)
    .sort()
    .join('&');
  const orders = (spec.orderBy ?? []).map(([field, dir]) => `${field}:${dir ?? 'asc'}`).join(',');
  return `${spec.collection}|${wheres}|${orders}|${spec.limit ?? ''}`;
}
