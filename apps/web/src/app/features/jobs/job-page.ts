import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  computed,
  effect,
  inject,
  input,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import type { Clip, Job, JobEvent, Source, Stage, StageStatus } from '@clipforge/contracts';

import { SessionService } from '../../core/session';
import { ClipForgeStore } from '../../core/store';

/**
 * One job, in enough detail to answer "why is it not moving?"
 *
 * The queue card can only say "1 of 4 stages". That is the right amount for a
 * list and nowhere near enough when the answer is wrong — a job stops for
 * reasons the card cannot express: a stage failed non-retryably, a worker took
 * it and died so the lease lapsed, the video had no speech in it and the
 * transcript came back empty. Each of those looks like "1 of 4" from outside.
 *
 * Three sources, layered from summary to raw:
 *
 * 1. **Health** — the diagnosis, computed. Whether the lease is dead, whether
 *    attempts are exhausted, whether anything is actually holding this job.
 * 2. **Stages** — where it got to, with per-stage duration, attempts, VRAM and
 *    the error verbatim.
 * 3. **The event log** — what happened, in order, straight from the worker.
 *
 * The layering matters: the log is authoritative but unreadable at a glance,
 * and the health line is readable but derived. Showing only one of them means
 * either guessing or scrolling.
 */
@Component({
  selector: 'app-job-page',
  imports: [RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './job-page.html',
})
export class JobPage implements OnDestroy {
  private readonly store = inject(ClipForgeStore);
  private readonly session = inject(SessionService);

  /** From the route: `jobs/:id`. */
  readonly id = input.required<string>();

  private stopJob: (() => void) | null = null;
  private stopEvents: (() => void) | null = null;
  private ticker: ReturnType<typeof setInterval> | null = null;

  protected readonly job = signal<Job | null | undefined>(undefined);
  protected readonly events = signal<JobEvent[] | null>(null);
  protected readonly source = signal<Source | null>(null);
  protected readonly results = signal<{ candidates: number; clips: Clip[] } | null>(null);
  protected readonly error = signal<string | null>(null);
  protected readonly cancelling = signal(false);
  protected readonly showLog = signal(true);

  /** Ticks so "held for 3m" keeps counting rather than freezing at load. */
  private readonly now = signal(Date.now());

  constructor() {
    effect((onCleanup) => {
      const id = this.id();
      const stopJob = this.store.watchJob(
        id,
        (job) => {
          this.job.set(job);
          // Resolved lazily and only once: the source is written by DOWNLOAD,
          // so it does not exist for a job that has not got that far, and it
          // never changes once it does.
          const sourceId = job?.sourceId;
          if (sourceId && !this.source()) {
            void this.store
              .loadSource(sourceId)
              .then((found) => this.source.set(found))
              .catch(() => this.source.set(null));
          }

          // Only once nothing more can be produced. Polled on every job update
          // it would run two queries per heartbeat for an answer that is still
          // being written; a finished job's output does not change.
          const uid = this.session.uid;
          if (uid && job && this.terminal(job) && !this.results()) {
            void this.store
              .loadJobResults(uid, job.id)
              .then((found) => this.results.set(found))
              .catch((err: unknown) =>
                this.error.set(err instanceof Error ? err.message : String(err)),
              );
          }
        },
        (err) => this.error.set(err.message),
      );
      const stopEvents = this.store.watchJobEvents(
        id,
        (events) => this.events.set(events),
        (err) => this.error.set(err.message),
      );

      this.stopJob = stopJob;
      this.stopEvents = stopEvents;
      onCleanup(() => {
        stopJob();
        stopEvents();
      });
    });

    this.ticker = setInterval(() => this.now.set(Date.now()), 5_000);
  }

  ngOnDestroy(): void {
    this.stopJob?.();
    this.stopEvents?.();
    if (this.ticker) clearInterval(this.ticker);
  }

  // ── Diagnosis ──────────────────────────────────────────────────────────────

  /**
   * Whether this job is RUNNING behind a lease nobody is renewing.
   *
   * The failure that has no other symptom. A worker that stops without
   * releasing its jobs leaves them RUNNING, and the reaper that would recover
   * them lives *inside a worker* (docs/adr/0006-lease-based-job-claiming.md) —
   * so on a single-worker deployment nothing reclaims the job until a worker
   * runs again. From the queue it looks merely slow, indefinitely.
   */
  protected readonly leaseExpired = computed(() => {
    const job = this.job();
    if (!job || job.status !== 'RUNNING' || !job.leaseExpiresAt) return false;
    return Date.parse(job.leaseExpiresAt) < this.now();
  });

  /** The one-line explanation, or null when the job needs no explaining. */
  protected readonly diagnosis = computed<string | null>(() => {
    const job = this.job();
    if (!job) return null;

    if (this.leaseExpired()) {
      return (
        `Stranded. ${job.workerId ?? 'A worker'} took this job and stopped without ` +
        `releasing it, and its lease ran out ${this.ago(Date.parse(job.leaseExpiresAt!))} ago. ` +
        `Nothing recovers it until a worker runs again — starting one reclaims it automatically.`
      );
    }

    if (job.status === 'QUEUED') {
      return 'Waiting for a worker to pick it up.';
    }

    if (job.status === 'FAILED') {
      const failed = job.stages.find((stage: Stage) => stage.status === 'FAILED');
      if (failed?.error && failed.error.retryable === false) {
        return `${failed.name} failed in a way that retrying cannot fix, so the job stopped rather than spending its remaining attempts.`;
      }
      return `Gave up after ${job.attempts + 1} attempt${job.attempts ? 's' : ''}.`;
    }

    return null;
  });

  protected readonly current = computed(
    () => this.job()?.stages.find((stage: Stage) => stage.status === 'RUNNING') ?? null,
  );

  protected readonly settled = computed(
    () =>
      this.job()?.stages.filter(
        (stage: Stage) => stage.status === 'DONE' || stage.status === 'SKIPPED',
      ).length ?? 0,
  );

  /**
   * How long the job has taken end to end.
   *
   * From `startedAt`, not `createdAt`: time spent queued is not time spent
   * working, and adding the two together would make an overnight queue look
   * like an overnight render.
   */
  protected readonly elapsed = computed(() => {
    const job = this.job();
    if (!job?.startedAt) return null;
    const end = job.endedAt ? Date.parse(job.endedAt) : this.now();
    return this.duration(end - Date.parse(job.startedAt));
  });

  protected cancellable(job: Job): boolean {
    return job.status === 'QUEUED' || job.status === 'RUNNING';
  }

  protected terminal(job: Job): boolean {
    return job.status === 'COMPLETED' || job.status === 'FAILED' || job.status === 'CANCELLED';
  }

  /**
   * A job that ran to completion and produced nothing.
   *
   * Not a failure and not a bug — most often a video with no speech in it, so
   * there were no words to judge and nothing to cut. It needs saying out loud
   * because the alternative is an empty review queue and no explanation.
   */
  protected readonly finishedEmpty = computed(() => {
    const job = this.job();
    const results = this.results();
    return !!job && job.status === 'COMPLETED' && !!results && results.clips.length === 0;
  });

  protected async cancel(): Promise<void> {
    const job = this.job();
    if (!job) return;
    this.cancelling.set(true);
    this.error.set(null);
    try {
      await this.store.cancel(job.id);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.cancelling.set(false);
    }
  }

  // ── Formatting ─────────────────────────────────────────────────────────────

  protected stageTone(status: StageStatus): string {
    switch (status) {
      case 'DONE':
        return 'bg-ok-bg text-ok-ink-soft';
      case 'RUNNING':
        return 'bg-accent-soft text-accent-ink';
      case 'FAILED':
        return 'bg-danger-bg text-danger-ink';
      case 'SKIPPED':
        return 'bg-line-strong/40 text-ink-subtle';
      default:
        return 'bg-line-strong/40 text-ink-muted';
    }
  }

  protected statusTone(job: Job): string {
    switch (job.status) {
      case 'COMPLETED':
        return 'bg-ok-bg text-ok-ink-soft';
      case 'FAILED':
        return 'bg-danger-bg text-danger-ink';
      case 'RUNNING':
        return 'bg-accent-soft text-accent-ink';
      case 'CANCELLED':
        return 'bg-line-strong/40 text-ink-muted';
      default:
        return 'bg-line-strong/40 text-ink-muted';
    }
  }

  /**
   * How long a stage took — its recorded duration, or how long it has been
   * going. The second half is what makes a wedged stage visible: "TRANSCRIBE,
   * 40m" says something "TRANSCRIBE, running" does not.
   */
  protected stageTime(stage: Stage): string | null {
    if (stage.durationMs != null) return this.duration(stage.durationMs);
    if (stage.status === 'RUNNING' && stage.startedAt) {
      return this.duration(this.now() - Date.parse(stage.startedAt));
    }
    return null;
  }

  protected duration(ms: number): string {
    const seconds = Math.max(0, Math.round(ms / 1000));
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
    return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
  }

  protected at(iso: string | null | undefined): string {
    if (!iso) return '—';
    return new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'medium' });
  }

  protected clock(iso: string | null | undefined): string {
    if (!iso) return '';
    return new Date(iso).toLocaleTimeString(undefined, { timeStyle: 'medium' });
  }

  protected ago(at: number): string {
    return this.duration(this.now() - at);
  }

  /** Events read better as sentences than as enum names. */
  protected describe(event: JobEvent): string {
    switch (event.kind) {
      case 'CREATED':
        return 'Job created';
      case 'CLAIMED':
        return `Claimed by ${event.workerId ?? 'a worker'}`;
      case 'STAGE_STARTED':
        return `${event.stage} started`;
      case 'STAGE_COMPLETED':
        return `${event.stage} finished`;
      case 'STAGE_FAILED':
        return `${event.stage} failed`;
      case 'COMPLETED':
        return 'Job completed';
      case 'FAILED':
        return 'Job failed';
      case 'CANCELLED':
        return 'Cancelled';
      case 'LEASE_EXPIRED':
        return 'Lease expired — the worker stopped renewing it';
      case 'REQUEUED':
        return 'Returned to the queue';
      default:
        return event.kind;
    }
  }

  protected eventTone(event: JobEvent): string {
    switch (event.kind) {
      case 'STAGE_FAILED':
      case 'FAILED':
        return 'bg-danger-solid';
      case 'LEASE_EXPIRED':
        return 'bg-warn-ink';
      case 'COMPLETED':
        return 'bg-ok-ink-soft';
      case 'STAGE_STARTED':
      case 'CLAIMED':
        return 'bg-accent-ink';
      default:
        return 'bg-line-strong';
    }
  }
}
