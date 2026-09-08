import { describe, expect, it } from 'vitest';

import type { Job, JobStatus, Stage, WorkerHeartbeat } from '@clipforge/contracts';

/**
 * The PWA half of the contracts check.
 *
 * Types are erased at runtime, so the real assertion here is that this file
 * *compiles*: it proves the `@clipforge/contracts` path mapping resolves and
 * that the generated TypeScript matches the shape the worker writes. If a
 * schema field is renamed and the generated output regenerated, this file stops
 * compiling — which is the point.
 *
 * The Python half lives in apps/worker/tests/unit/test_contracts.py.
 */
describe('@clipforge/contracts', () => {
  it('describes a job the worker could actually write', () => {
    const stage: Stage = {
      name: 'ECHO_ONE',
      lane: 'CPU',
      status: 'PENDING',
    };

    const job: Job = {
      id: 'job-1',
      uid: 'user-1',
      type: 'ECHO',
      status: 'QUEUED',
      stages: [stage],
      attempts: 0,
      maxAttempts: 3,
      createdAt: '2026-09-08T12:00:00Z',
      updatedAt: '2026-09-08T12:00:00Z',
    };

    expect(job.stages).toHaveLength(1);
    expect(job.stages[0].name).toBe('ECHO_ONE');
  });

  it('constrains job status to the documented state machine', () => {
    const terminal: JobStatus[] = ['COMPLETED', 'FAILED', 'CANCELLED'];
    expect(terminal).toHaveLength(3);
  });

  it('describes a worker heartbeat', () => {
    const heartbeat: WorkerHeartbeat = {
      workerId: 'worker-1',
      uid: 'user-1',
      status: 'ONLINE',
      capabilities: { whisper: true, llm: true, render: true, publish: false },
      version: '0.0.1',
      lastSeenAt: '2026-09-08T12:00:00Z',
    };

    expect(heartbeat.capabilities.publish).toBe(false);
  });
});
