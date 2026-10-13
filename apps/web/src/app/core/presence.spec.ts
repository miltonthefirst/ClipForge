import { TestBed } from '@angular/core/testing';
import type { AgentReport, WorkerHeartbeat } from '@clipforge/contracts';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { PING_ANSWER_MS, WorkersRepository } from './data/workers';
import { PresenceService, agentPresence, workerPresence, type PresenceCheck } from './presence';

/**
 * "Is it there?" — asked, never assumed from a document's age.
 *
 * The agent and the worker no longer write on a timer, so a document that has
 * not moved for an hour says nothing about whether its machine is on. These pin
 * the reading of a ping's answer, which is the only thing that does.
 */

const ASKED_AT = '2026-10-13T09:00:00.000Z';

function agent(over: Partial<AgentReport> = {}): AgentReport {
  return {
    agentId: 'tower',
    version: '0.0.2',
    desired: 'RUNNING',
    state: 'RUNNING',
    lastSeenAt: ASKED_AT,
    ...over,
  } as AgentReport;
}

function beat(over: Partial<WorkerHeartbeat> = {}): WorkerHeartbeat {
  return {
    workerId: 'tower',
    uid: 'local',
    status: 'ONLINE',
    capabilities: { whisper: true, llm: true, render: true, publish: false },
    version: '0.0.2',
    lastSeenAt: ASKED_AT,
    ...over,
  } as WorkerHeartbeat;
}

function check(over: Partial<PresenceCheck> = {}): PresenceCheck {
  return {
    agentId: 'tower',
    token: 'tok-1',
    askedAt: 0,
    agentSeenAt: ASKED_AT,
    workerSeenAt: ASKED_AT,
    expired: false,
    ...over,
  };
}

describe('agentPresence', () => {
  it('is unknown until somebody asks', () => {
    expect(agentPresence(null, agent())).toBe('unknown');
  });

  it('is checking while the window is open and nothing has come back', () => {
    expect(agentPresence(check(), agent())).toBe('checking');
  });

  it('is online when the token comes back', () => {
    expect(agentPresence(check(), agent({ answeredPing: 'tok-1' }))).toBe('online');
  });

  it('is online when the document moved after the question, token or not', () => {
    // A state change written a moment after the ping is as good an answer.
    expect(agentPresence(check(), agent({ lastSeenAt: '2026-10-13T09:00:03.000Z' }))).toBe(
      'online',
    );
  });

  it('is offline when the window closes unanswered', () => {
    // However recent the document looks: an old answer is not this answer.
    expect(agentPresence(check({ expired: true }), agent({ answeredPing: 'older' }))).toBe(
      'offline',
    );
  });

  it('does not read one machine’s answer as another’s', () => {
    expect(agentPresence(check({ agentId: 'laptop' }), agent({ answeredPing: 'tok-1' }))).toBe(
      'unknown',
    );
  });
});

describe('workerPresence', () => {
  it('is online when the worker echoes the token', () => {
    expect(workerPresence(check(), agent(), beat({ answeredPing: 'tok-1' }))).toBe('online');
  });

  it('is online when the worker wrote after the question — it started, say', () => {
    expect(workerPresence(check(), agent(), beat({ lastSeenAt: '2026-10-13T09:00:05.000Z' }))).toBe(
      'online',
    );
  });

  it('is offline at once when the agent answers that no worker is running', () => {
    // No need to wait out the window for a worker the machine says is not there.
    const stopped = agent({ answeredPing: 'tok-1', state: 'STOPPED' });
    expect(workerPresence(check(), stopped, beat())).toBe('offline');
  });

  it('is offline when the worker wrote OFFLINE on its way out', () => {
    const stopping = beat({ status: 'OFFLINE', lastSeenAt: '2026-10-13T09:00:04.000Z' });
    expect(workerPresence(check(), agent(), stopping)).toBe('offline');
  });

  it('believes the agent over a worker that died without saying so', () => {
    // Both stamps come from the machine's own clock. The worker answered, then
    // died; the agent noticed and said so afterwards.
    const answered = beat({ answeredPing: 'tok-1', lastSeenAt: '2026-10-13T09:00:01.000Z' });
    const noticed = agent({
      answeredPing: 'tok-1',
      state: 'STOPPED',
      lastSeenAt: '2026-10-13T09:00:09.000Z',
    });
    expect(workerPresence(check(), noticed, answered)).toBe('offline');
  });

  it('keeps checking while the agent says a worker is running and the worker has not answered', () => {
    expect(workerPresence(check(), agent({ answeredPing: 'tok-1' }), beat())).toBe('checking');
  });

  it('is offline when the window closes with no word from the worker', () => {
    expect(workerPresence(check({ expired: true }), agent({ answeredPing: 'tok-1' }), beat())).toBe(
      'offline',
    );
  });

  it('is unknown with nobody asking', () => {
    expect(workerPresence(null, agent(), beat())).toBe('unknown');
  });
});

describe('PresenceService', () => {
  let ping: ReturnType<typeof vi.fn>;
  let service: PresenceService;

  beforeEach(() => {
    vi.useFakeTimers();
    ping = vi.fn().mockResolvedValue(undefined);
    TestBed.configureTestingModule({
      providers: [{ provide: WorkersRepository, useValue: { ping } }],
    });
    service = TestBed.inject(PresenceService);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('writes a fresh token and records what the documents said when it asked', async () => {
    await service.ping(agent(), beat(), 'uid-1', 1_000);

    expect(ping).toHaveBeenCalledOnce();
    const [agentId, uid, token] = ping.mock.calls[0] as [string, string, string];
    expect(agentId).toBe('tower');
    expect(uid).toBe('uid-1');
    expect(service.check()).toMatchObject({ token, askedAt: 1_000, agentSeenAt: ASKED_AT });
  });

  it('closes the window once, after the answer time, and never asks again on its own', async () => {
    await service.ping(agent(), beat(), 'uid-1');

    vi.advanceTimersByTime(PING_ANSWER_MS - 1);
    expect(service.check()?.expired).toBe(false);
    vi.advanceTimersByTime(1);
    expect(service.check()?.expired).toBe(true);

    vi.advanceTimersByTime(10 * 60_000);
    expect(ping).toHaveBeenCalledOnce();
  });

  it('does not write again while its own question is inside the throttle', async () => {
    await service.ping(agent(), beat(), 'uid-1', 1_000);
    await service.ping(agent(), beat(), 'uid-1', 10_000);

    expect(ping).toHaveBeenCalledOnce();
  });

  it('waits on somebody else’s token when the rules refuse the write', async () => {
    // Another device asked moments ago; its question is this question.
    ping.mockRejectedValueOnce(Object.assign(new Error('denied'), { code: 'permission-denied' }));

    await service.ping(agent({ pingToken: 'theirs', answeredPing: 'theirs' }), beat(), 'uid-1');

    expect(service.check()?.token).toBe('theirs');
    expect(service.error()).toBeNull();
    expect(agentPresence(service.check(), agent({ answeredPing: 'theirs' }))).toBe('online');
  });

  it('says why when the write fails for another reason', async () => {
    ping.mockRejectedValueOnce(new Error('offline'));

    await service.ping(agent(), beat(), 'uid-1');

    expect(service.error()).toBe('offline');
  });

  it('forgets the question when somebody else signs in', async () => {
    service.scope('uid-1');
    await service.ping(agent(), beat(), 'uid-1');
    service.scope('uid-1');
    expect(service.check()).not.toBeNull();

    service.scope('uid-2');
    expect(service.check()).toBeNull();
  });
});
