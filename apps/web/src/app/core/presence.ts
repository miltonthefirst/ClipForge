import { Injectable, inject, signal } from '@angular/core';
import type { AgentReport, WorkerHeartbeat } from '@clipforge/contracts';

import { PING_ANSWER_MS, PING_THROTTLE_MS, WorkersRepository, newPingToken } from './data/workers';

/**
 * Is the machine there, and is a worker running on it — asked, not assumed.
 *
 * Neither the agent nor the worker writes on a timer any more. They used to:
 * the agent once a minute, the worker every thirty seconds, so that a page
 * could call a machine "off" when its documents stopped moving. Each of those
 * writes was also a read for every open listener, all day, to say nothing new.
 * Now a document moves when something happens, and when a page wants to know
 * whether anyone is there it *pings*: writes a fresh token to the agent's
 * document, and watches the two documents it is already listening to for that
 * token to come back. See docs/adr/0030-status-on-events-and-pings.md.
 *
 * So the answer to "is it there?" has four states rather than two:
 *
 * - `unknown`  nobody has asked, or there is no agent to ask;
 * - `checking` asked, and the answer window is still open;
 * - `online`   it answered — the token came back, or the document moved since;
 * - `offline`  the window closed with no answer.
 *
 * The answer is as of the ping. Nothing re-asks on a timer: a page that has
 * been open for an hour says when it last checked, and has a button.
 */
export type Presence = 'unknown' | 'checking' | 'online' | 'offline';

/** One question put to one machine, and what its documents said when it was asked. */
export interface PresenceCheck {
  readonly agentId: string;
  readonly token: string;
  /** This device's clock, for "checked 20s ago" — never compared with the machine's. */
  readonly askedAt: number;
  /**
   * Each document's `lastSeenAt` at the moment of asking. Any later value means
   * the document was written after the question, which is an answer as good
   * as the token: a worker that started, or finished a job, a moment after the
   * ping was alive after the ping, whether or not it saw the token.
   */
  readonly agentSeenAt: string | null;
  readonly workerSeenAt: string | null;
  /** The answer window has closed. Set once, by a timer per question. */
  readonly expired: boolean;
}

/** The agent states in which it is saying no worker is running. */
function saysNoWorker(agent: AgentReport): boolean {
  return agent.state === 'STOPPED' || agent.state === 'FAILED';
}

/** Whether the agent on that machine is there, as of the last question. */
export function agentPresence(check: PresenceCheck | null, agent: AgentReport | null): Presence {
  if (!check || !agent || agent.agentId !== check.agentId) return 'unknown';
  if (agent.answeredPing === check.token || agent.lastSeenAt !== check.agentSeenAt) {
    return 'online';
  }
  return check.expired ? 'offline' : 'checking';
}

/**
 * Whether a worker is running on that machine, as of the last question.
 *
 * The worker answers for itself: the agent relays the token to it over
 * loopback and the worker echoes it in its own document. The agent's word is
 * used only to say *no* — an agent that has answered with STOPPED or FAILED is
 * saying no worker is running, and waiting out the window for a worker it has
 * just said is not there would be fifteen seconds of "checking" for nothing.
 *
 * And when the worker's last word is older than the agent's "none running",
 * the agent wins. Both stamps come from the same machine's clock, so they can
 * be compared, and this is the case of a worker that died without writing
 * OFFLINE: the agent notices within two seconds and says so; the worker never
 * will.
 */
export function workerPresence(
  check: PresenceCheck | null,
  agent: AgentReport | null,
  beat: WorkerHeartbeat | null,
): Presence {
  if (!check) return 'unknown';
  const machine = agentPresence(check, agent);
  const answered =
    !!beat && (beat.answeredPing === check.token || beat.lastSeenAt !== check.workerSeenAt);

  if (beat && answered) {
    if (beat.status === 'OFFLINE') return 'offline';
    const supersededBy =
      machine === 'online' && agent && saysNoWorker(agent)
        ? Date.parse(agent.lastSeenAt) > Date.parse(beat.lastSeenAt)
        : false;
    return supersededBy ? 'offline' : 'online';
  }
  if (machine === 'online' && agent && saysNoWorker(agent)) return 'offline';
  return check.expired ? 'offline' : 'checking';
}

/** A write the rules refused, which for a ping almost always means the throttle. */
function refused(error: unknown): boolean {
  return (error as { code?: unknown } | null)?.code === 'permission-denied';
}

/**
 * The one question in flight, shared by every panel that shows the answer.
 *
 * Root-provided so that leaving the Jobs page for the Worker page does not ask
 * again: the panel on each pings when it opens, and a question asked a few
 * seconds ago is still being answered.
 */
@Injectable({ providedIn: 'root' })
export class PresenceService {
  private readonly data = inject(WorkersRepository);

  readonly check = signal<PresenceCheck | null>(null);
  readonly error = signal<string | null>(null);

  private expiry: ReturnType<typeof setTimeout> | null = null;
  private owner: string | null = null;

  /**
   * Ask `agent`'s machine whether it is there.
   *
   * Does nothing if this device asked the same machine within the throttle
   * window: the rules would refuse the write, and the question already in
   * flight — or just answered — is the same question. If the rules refuse it
   * anyway, because another device asked first, this waits on that device's
   * token, which the documents carry.
   */
  async ping(
    agent: AgentReport,
    worker: WorkerHeartbeat | null,
    uid: string,
    now: number = Date.now(),
  ): Promise<void> {
    const current = this.check();
    if (current && current.agentId === agent.agentId && now - current.askedAt < PING_THROTTLE_MS) {
      return;
    }

    const asked = {
      agentId: agent.agentId,
      askedAt: now,
      agentSeenAt: agent.lastSeenAt ?? null,
      workerSeenAt: worker?.lastSeenAt ?? null,
      expired: false,
    };
    const token = newPingToken();
    this.ask({ ...asked, token });

    try {
      await this.data.ping(agent.agentId, uid, token);
      this.error.set(null);
    } catch (error) {
      if (refused(error) && agent.pingToken) {
        // Somebody asked moments ago. Their token answers the same question.
        this.ask({ ...asked, token: agent.pingToken });
        this.error.set(null);
        return;
      }
      this.error.set(error instanceof Error ? error.message : 'Could not ask the machine.');
    }
  }

  /**
   * Keep the question only for the user who asked it. A panel calls this with
   * the signed-in uid whenever it (re)subscribes; the same user keeps the
   * question in flight, a different one — or nobody — starts clean.
   */
  scope(uid: string | null): void {
    if (uid === this.owner) return;
    this.owner = uid;
    this.reset();
  }

  /** Forget the question: the next ping starts a new one. */
  reset(): void {
    if (this.expiry) clearTimeout(this.expiry);
    this.expiry = null;
    this.check.set(null);
    this.error.set(null);
  }

  /**
   * Record the question and close its window later. One timer per question,
   * never repeating: the page does not re-ask on its own.
   */
  private ask(check: PresenceCheck): void {
    if (this.expiry) clearTimeout(this.expiry);
    this.check.set(check);
    this.expiry = setTimeout(() => {
      this.expiry = null;
      this.check.update((held) =>
        held && held.token === check.token && held.askedAt === check.askedAt
          ? { ...held, expired: true }
          : held,
      );
    }, PING_ANSWER_MS);
  }
}
