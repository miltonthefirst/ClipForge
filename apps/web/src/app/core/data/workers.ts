import { Injectable, computed, inject } from '@angular/core';
import type {
  AgentDesired,
  AgentReport,
  HomeRouteDesired,
  ProjectUsage,
  WorkerHeartbeat,
} from '@clipforge/contracts';

import { FirestoreGateway, type Live } from '../firestore/gateway';
import type { QuerySpec } from '../firestore/spec';

/**
 * The worker, and the agent that can start one.
 *
 * Two collections rather than one, because each is blind where the other sees.
 * A worker document says what a worker last reported doing and cannot say
 * anything at all when no worker is running — which is exactly the state somebody looking at
 * a stalled queue most needs explained. The agent keeps reporting either way.
 *
 * ## A wish, not a command
 *
 * Nothing here starts a process. A browser tab cannot spawn one, and neither can
 * Firestore: the PWA writes what is wanted to `agents/{machineId}` and the agent
 * on that machine — the only party that can — does it and reports back through
 * the same document. That asymmetry is the whole design of this domain, not an
 * implementation detail of {@link WorkersRepository.wish}.
 */

/**
 * Anything that reports. Both collections stamp the same way, and the freshest
 * one is the one that counts in both.
 *
 * `lastSeenAt` is when a document last had something to say, not a liveness
 * beat: neither the agent nor the worker writes on a timer. Whether either is
 * there *now* is what a ping answers; see `core/presence.ts`.
 */
interface Beacon {
  readonly lastSeenAt: string;
}

/**
 * Worker heartbeats.
 *
 * Bounded at ten: this deployment has one worker, and an unbounded listen is how
 * the free tier's daily read allowance gets spent on nothing.
 */
export const WORKERS_QUERY: QuerySpec = { collection: 'workers', limit: 10 };

/**
 * Every machine that has ever run an agent.
 *
 * Bounded like every other listener here. Four is generous for a system whose
 * premise is one machine with a GPU.
 */
export const AGENTS_QUERY: QuerySpec = { collection: 'agents', limit: 4 };

/**
 * Freshest first, so a machine that was replaced does not outrank the one
 * actually beating.
 *
 * This orders what arrived; it is not an `orderBy` on the query and cannot be
 * one cheaply, because a document missing `lastSeenAt` would drop out of an
 * ordered query altogether and vanish from a panel whose job is to account for
 * every machine. So the limit above picks some ten, and this sorts those ten —
 * fine while one machine is the premise, and worth revisiting alongside the
 * limit if that ever stops being true.
 *
 * A timestamp that will not parse sorts last rather than wherever the comparator
 * happens to leave it: a corrupt field must not let a dead machine outrank a
 * live one.
 */
export function byFreshest<T extends Beacon>(reports: readonly T[]): T[] {
  return [...reports].sort((a, b) => beatAt(b) - beatAt(a));
}

function beatAt(report: Beacon): number {
  const at = Date.parse(report.lastSeenAt);
  return Number.isNaN(at) ? 0 : at;
}

/**
 * How long a ping is given to come back before the machine is called off.
 *
 * The agent hears a ping through a listener it already holds, so on a machine
 * that is on the answer is a second or two. Fifteen is generous for a slow
 * network and short enough that "off" is said while someone is still looking.
 */
export const PING_ANSWER_MS = 15_000;

/**
 * The rules refuse a second ping to one machine inside this window, whoever
 * sends it. A client that asked within it waits on its own ping instead of
 * writing one the rules will refuse.
 */
export const PING_THROTTLE_MS = 15_000;

/** What a ping writes to the agent document, beside the server's clock. */
export interface PingWrite {
  readonly pingToken: string;
  readonly pingRequestedBy: string;
}

/**
 * A fresh ping token. Random rather than a timestamp, because the answer is
 * matched by equality and two devices asking in the same millisecond must not
 * read each other's answers as their own — and between 8 and 64 characters,
 * which is what the rules accept.
 */
export function newPingToken(): string {
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

/**
 * The fields one ping writes. `pingRequestedAt` is added by the gateway as the
 * server's timestamp: the rules compare it with `request.time`.
 */
export function pingWrite(uid: string, token: string): PingWrite {
  return { pingToken: token, pingRequestedBy: uid };
}

/** What a wish writes to the agent document. */
export interface WishWrite {
  readonly desired: AgentDesired;
  readonly requestedBy: string;
  readonly requestedAt: string;
}

/**
 * The fields one press of Start or Stop writes.
 *
 * `requestedAt` is written on every call, including one that repeats the current
 * value, and that is load-bearing rather than incidental. Pressing Start on a
 * machine whose agent has given up is how a person clears it, and a supervisor
 * comparing only the value could not tell that press from the wish simply still
 * being RUNNING. The rules require the field for the same reason.
 */
export function wishWrite(uid: string, desired: AgentDesired, at: Date = new Date()): WishWrite {
  return { desired, requestedBy: uid, requestedAt: at.toISOString() };
}

/** What switching the home wifi route on or off writes (admins only, by rule). */
export interface HomeRouteWrite {
  readonly homeRouteDesired: HomeRouteDesired;
  readonly homeRouteRequestedBy: string;
  readonly homeRouteRequestedAt: string;
}

export function homeRouteWrite(
  uid: string,
  desired: HomeRouteDesired,
  at: Date = new Date(),
): HomeRouteWrite {
  return {
    homeRouteDesired: desired,
    homeRouteRequestedBy: uid,
    homeRouteRequestedAt: at.toISOString(),
  };
}

@Injectable({ providedIn: 'root' })
export class WorkersRepository {
  private readonly db = inject(FirestoreGateway);

  /**
   * Worker heartbeats, live — who is serving the queue, and what they can do.
   *
   * What this answers that nothing else can: a job sitting at QUEUED is either
   * "the worker is busy with something else" or "there is no worker", and those
   * look identical from the job document alone.
   *
   * Freshest first, so the first heartbeat is the one to read.
   */
  heartbeats(): Live<WorkerHeartbeat[]> {
    return this.freshest<WorkerHeartbeat>(WORKERS_QUERY);
  }

  /**
   * Every machine that has ever run an agent, live.
   *
   * The companion to {@link heartbeats} and not a replacement for it: a
   * heartbeat says what a worker believes it is doing, and cannot say anything
   * at all when no worker is running — which is the state somebody looking at a
   * stalled queue most needs explained. An agent that answers a ping with state
   * STOPPED means the PC is on and waiting; one that does not answer means the
   * PC is off, and no worker document can tell those apart.
   *
   * Freshest first, so the first agent is the machine to offer to start.
   */
  agents(): Live<AgentReport[]> {
    return this.freshest<AgentReport>(AGENTS_QUERY);
  }

  /**
   * Ask a machine to start or stop its worker.
   *
   * A wish, not a command: this writes what is wanted and returns. The agent on
   * that machine is the only thing that can spawn a process, and what it does
   * about the wish — and how long it takes — is reported back through the same
   * document, which {@link agents} is already listening to. There is nothing to
   * refresh here and nothing to await beyond the write itself.
   */
  async wish(agentId: string, uid: string, desired: AgentDesired): Promise<void> {
    await this.db.update('agents', agentId, { ...wishWrite(uid, desired) });
  }

  /**
   * Ask a machine whether it is there.
   *
   * Writes a fresh token to the agent document. The agent echoes it in
   * `answeredPing` the moment its listener delivers it, and passes it to the
   * worker over loopback, which echoes it in its own document — so both of the
   * listeners above see the answer arrive with no further request. Rejected by
   * the rules within fifteen seconds of the last ping to that machine; the
   * caller waits on that one instead.
   */
  /**
   * What the project used of Firebase, from Google's own counts, measured by
   * the agent when it starts and when someone opens the app (clipforge/usage.py).
   */
  usage(): Live<ProjectUsage> {
    return this.db.liveDoc<ProjectUsage>('usage', 'current');
  }

  /**
   * Switch the home wifi route on or off (docs/HOME-NETWORK-PLAN.md).
   *
   * A wish like {@link wish}, carried out by the agent and reported back on the
   * same document as `home`. Admins only: the rules check the role, because
   * switching it on opens a port on the owner's network.
   */
  async homeRoute(agentId: string, uid: string, desired: HomeRouteDesired): Promise<void> {
    await this.db.update('agents', agentId, { ...homeRouteWrite(uid, desired) });
  }

  async ping(agentId: string, uid: string, token: string): Promise<void> {
    await this.db.updateStamped('agents', agentId, { ...pingWrite(uid, token) }, 'pingRequestedAt');
  }

  /**
   * One live query, sorted.
   *
   * The sort is a `computed` over the gateway's signal rather than a copy of it,
   * so the listener stays shared and the three states survive: `null` still
   * means "nothing has arrived", which is a different answer from an empty list
   * and the distinction this whole layer exists to keep.
   */
  private freshest<T extends Beacon>(spec: QuerySpec): Live<T[]> {
    const live = this.db.live<T>(spec);
    return {
      ...live,
      data: computed(() => {
        const rows = live.data();
        return rows === null ? null : byFreshest(rows);
      }),
    };
  }
}
