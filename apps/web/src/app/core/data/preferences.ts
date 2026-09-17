import { Injectable, inject } from '@angular/core';
import type { ObscureOptions, Preference, PreferenceStatus } from '@clipforge/contracts';

import { FirestoreGateway, type Live } from '../firestore/gateway';
import type { QuerySpec } from '../firestore/spec';

/**
 * What the reviewer has taught the system.
 *
 * A preference is a **proposal** until a human accepts it. The worker writes
 * one out of a correction it has just made sense of, and nothing in the
 * pipeline reads it while it is PROPOSED — which is why the listener below
 * filters by status rather than handing the page everything and asking it to
 * mark the pending ones. Acting on an unaccepted preference would mean the
 * system teaching itself from its own guesses.
 *
 * Deciding is the only write a client may make here. `firestore.rules` allows
 * an update to exactly `status`, `decidedAt` and `decidedBy`, and refuses
 * create and delete outright: proposing is the worker's job, and a rejection is
 * itself worth keeping.
 */

/**
 * How many proposals one listener delivers.
 *
 * Bounded like every other listener in this app, and for the same reason:
 * Firestore bills a read per delivered document and re-delivers the whole
 * result set on reconnect (docs/adr/0009-spark-tier-local-artefacts.md). Fifty
 * is the number this query has always used.
 */
const PAGE = 50;

/**
 * Proposals in one status.
 *
 * No `orderBy`, deliberately. An equality filter plus a sort on another field
 * needs a composite index, none is deployed for `preferences`, and a listener
 * against a missing index does not come back slow — it comes back failed. That
 * is the failure this whole layer is built around, and there is no ordering
 * here worth buying it.
 */
export function preferencesSpec(status: PreferenceStatus): QuerySpec {
  return { collection: 'preferences', where: [['status', '==', status]], limit: PAGE };
}

/**
 * The write onto the *source* that accepting a preference may also mean.
 *
 * This is where the difference between a note and a rule lives. Most of what
 * gets learned here is a sentence, and a sentence earns its keep by going into
 * the next note-reading prompt. A rectangle cannot: it has to be somewhere
 * RENDER reads *before* a clip exists, or the reviewer keeps being asked about
 * a logo they have already decided about.
 *
 * Returns `null` for the three cases where there is nothing to make a rule of:
 * a rejection, a preference carrying no rectangles, and an EVERYTHING-scope
 * preference, which is stored unattached and so names no source to pin them to.
 */
export function obscureRuleFrom(
  preference: Preference,
  status: 'ACCEPTED' | 'REJECTED',
): { sourceId: string; obscure: ObscureOptions } | null {
  if (status !== 'ACCEPTED') return null;
  const regions = preference.defaults?.obscure?.regions ?? [];
  if (!preference.sourceId || regions.length === 0) return null;
  return {
    sourceId: preference.sourceId,
    obscure: {
      // Detection stays off: these boxes are already known, and `auto` would
      // buy a decode pass over every future cut to rediscover them.
      auto: false,
      regions,
      // Left unset so the pipeline's own defaults apply. The reviewer decided
      // *where*, not how hard to blur it.
      method: null,
      strength: null,
    },
  };
}

@Injectable({ providedIn: 'root' })
export class PreferencesRepository {
  private readonly db = inject(FirestoreGateway);

  /**
   * What the system has worked out about you, and has not yet been told to keep.
   *
   * Live rather than fetched once: a remake proposes preferences while the
   * reviewer is still on the page that queued it, and a list that needed a
   * reload to show them would be a list nobody ever saw.
   */
  watch(status: PreferenceStatus = 'PROPOSED'): Live<Preference[]> {
    return this.db.live<Preference>(preferencesSpec(status));
  }

  /**
   * Keep a preference, or turn it down for good.
   *
   * Rejecting stores the decision rather than deleting the row, and that is the
   * point: the worker checks every proposal against what it already holds in
   * ANY status, so a suggestion that has been turned down once cannot come back
   * on the next correction of the same kind of clip.
   *
   * Accepting one that carries rectangles does a second write, onto the source
   * — see {@link obscureRuleFrom} for why a rectangle cannot stay a sentence.
   * Ordered so the rule lands first. If the source write is refused, the
   * preference stays PROPOSED and can be accepted again, which is recoverable.
   * Accepting first and failing second would leave a preference marked kept
   * that does nothing, with nothing on screen to say so.
   *
   * Takes the whole preference rather than its id because the rectangles are on
   * the document the caller is already holding, and re-reading it here to find
   * out whether there are any would be a read to answer a question that had
   * already been answered.
   */
  async decide(
    preference: Preference,
    uid: string,
    status: 'ACCEPTED' | 'REJECTED',
  ): Promise<void> {
    const rule = obscureRuleFrom(preference, status);
    if (rule) {
      // A single field, and the rules pin it to exactly that name: everything
      // else on a source describes a file on the worker.
      await this.db.update('sources', rule.sourceId, { obscure: rule.obscure });
    }
    await this.db.update('preferences', preference.id, {
      status,
      decidedAt: new Date().toISOString(),
      decidedBy: uid,
    });
  }
}
