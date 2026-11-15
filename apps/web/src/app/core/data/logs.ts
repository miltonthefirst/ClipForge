import { Injectable, inject } from '@angular/core';
import type { LogGroup, LogOccurrence } from '@clipforge/contracts';

import { FirestoreGateway, type Live } from '../firestore/gateway';
import type { QuerySpec } from '../firestore/spec';

/**
 * Grouped errors from the worker and the agent (apps/worker/clipforge/logs),
 * for admins: the logging blueprint's admin half.
 *
 * One bounded query, the 500 most recently active groups, so every column can
 * be sorted and filtered in memory with no composite index (blueprint §7).
 */
export const LOG_GROUPS_QUERY: QuerySpec = {
  collection: 'logGroups',
  orderBy: [['lastSeenAt', 'desc']],
  limit: 500,
};

export function occurrencesQuery(fingerprint: string): QuerySpec {
  return {
    collection: `logGroups/${fingerprint}/occurrences`,
    orderBy: [['timestamp', 'desc']],
    limit: 25,
  };
}

@Injectable({ providedIn: 'root' })
export class LogsRepository {
  private readonly db = inject(FirestoreGateway);

  groups(): Live<LogGroup[]> {
    return this.db.live<LogGroup>(LOG_GROUPS_QUERY);
  }

  group(fingerprint: string): Live<LogGroup> {
    return this.db.liveDoc<LogGroup>('logGroups', fingerprint);
  }

  occurrences(fingerprint: string): Live<LogOccurrence[]> {
    return this.db.live<LogOccurrence>(occurrencesQuery(fingerprint));
  }

  async resolve(fingerprints: readonly string[], uid: string): Promise<void> {
    const at = new Date().toISOString();
    await Promise.all(
      fingerprints.map((fp) =>
        this.db.update('logGroups', fp, { status: 'resolved', resolvedAt: at, resolvedBy: uid }),
      ),
    );
  }

  async reopen(fingerprint: string): Promise<void> {
    await this.db.update('logGroups', fingerprint, {
      status: 'unresolved',
      resolvedAt: null,
      resolvedBy: null,
    });
  }

  async watch(fingerprint: string, watched: boolean): Promise<void> {
    await this.db.update('logGroups', fingerprint, { watched });
  }

  /**
   * Delete a group and its occurrences. The blueprint does this with a
   * server-side recursiveDelete; ClipForge has no server, so the occurrences
   * go first, from here: a fingerprint is deterministic, and orphans would be
   * inherited by the next recurrence.
   */
  async remove(fingerprint: string, occurrenceIds: readonly string[]): Promise<void> {
    const path = `logGroups/${fingerprint}/occurrences`;
    await Promise.all(occurrenceIds.map((id) => this.db.remove(path, id)));
    await this.db.remove('logGroups', fingerprint);
  }
}
