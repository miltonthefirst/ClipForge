import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  computed,
  effect,
  inject,
  input,
  signal,
  untracked,
} from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import type { Clip, Job, JobEvent, Source, Stage, StageStatus } from '@clipforge/contracts';

import { JobsRepository } from '../../core/data/jobs';
import { SourcesRepository } from '../../core/data/sources';
import type { Live } from '../../core/firestore/gateway';
import { isTerminal, jobTitle, stageNote } from '../../core/job-list';

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
  private readonly router = inject(Router);
  private readonly jobs = inject(JobsRepository);
  private readonly sources = inject(SourcesRepository);

  /** From the route: `jobs/:id`. */
  readonly id = input.required<string>();

  /**
   * The two listeners this page holds, released when the route id changes and
   * when the page goes.
   *
   * Signals rather than plain fields because the delivering effects below
   * *read* them, and a plain field is not tracked: each of those effects would
   * run once against no listener and never again, leaving the page on
   * "Loading…" for ever with nothing anywhere saying why.
   */
  private readonly heldJob = signal<Live<Job> | null>(null);
  private readonly heldEvents = signal<Live<JobEvent[]> | null>(null);

  private ticker: ReturnType<typeof setInterval> | null = null;

  /**
   * The job, in three states.
   *
   * `undefined` is "has not arrived"; `null` is "there is no such job", which
   * is what a deleted job looks like to a page still open on it. The gateway
   * draws the same line — `data()` null while `loading()` is true, against
   * `data()` null once it is false — and collapsing the two here is what would
   * leave a removed job reading "Loading…" for as long as the tab stayed open.
   */
  protected readonly job = signal<Job | null | undefined>(undefined);
  /**
   * The event log: null until it arrives, and empty for a job with nothing
   * recorded against it. Those are opposite conclusions about a stalled job,
   * so the template renders them as separate sentences.
   */
  protected readonly events = signal<JobEvent[] | null>(null);
  protected readonly source = signal<Source | null>(null);
  protected readonly results = signal<{ candidates: number; clips: Clip[] } | null>(null);
  protected readonly error = signal<string | null>(null);
  /**
   * Why the job is not on screen, when it is not coming.
   *
   * {@link error} is a banner *inside* the loaded page, so a listener that fails
   * before its first delivery had nowhere to put itself: `job()` stayed
   * `undefined` and the page read "Loading…" for as long as it was open, with
   * nothing anywhere saying the read had been refused.
   */
  protected readonly loadFailure = signal<string | null>(null);
  /** The same, for the log — which loads separately and can fail on its own. */
  protected readonly eventsFailure = signal<string | null>(null);
  protected readonly cancelling = signal(false);
  protected readonly busy = signal(false);
  protected readonly showLog = signal(true);

  /** Ticks so "held for 3m" keeps counting rather than freezing at load. */
  private readonly now = signal(Date.now());

  constructor() {
    effect(() => {
      const id = this.id();
      // Cleared first, and unconditionally: a failure belongs to the listener
      // that produced it, and this effect is about to replace both listeners.
      this.loadFailure.set(null);
      this.eventsFailure.set(null);

      // Let go of the previous job's listeners before taking the next one's.
      // The gate holds a listener warm for fifteen minutes after that, so
      // stepping back to a job re-attaches to the same one and costs nothing —
      // which is the whole reason releasing is not unsubscribing.
      // `untracked`, or this effect depends on the signals it is about to write
      // and re-runs itself for ever, releasing and re-opening a listener on
      // every pass — a hang rather than a leak.
      untracked(() => {
        this.heldJob()?.release();
        this.heldEvents()?.release();
      });
      this.heldJob.set(null);
      this.heldEvents.set(null);

      // Everything on screen describes the job that was here a moment ago. The
      // source and the results are each resolved once and only once, so left in
      // place they would be inherited by the next job, which would then never
      // ask for its own.
      this.job.set(undefined);
      this.events.set(null);
      this.source.set(null);
      this.results.set(null);

      this.heldJob.set(this.jobs.watchJob(id));
      this.heldEvents.set(this.jobs.watchJobEvents(id));
    });

    // Separate from the subscription above so that a delivery is not a reason
    // to re-subscribe: these read the held signals and nothing else, and
    // reading `heldJob` inside the effect that assigns it would make every
    // snapshot re-open the listener it arrived on.
    effect(() => {
      const held = this.heldJob();
      if (!held) return;
      const failure = held.error();
      if (failure) {
        this.loadFailure.set(failure.message);
        return;
      }
      const job = held.data();
      // Null while the listener is still loading means the answer has not come
      // back; null once it has loaded means there is no such job. Only the
      // second is news, and telling them apart is what this page exists to do.
      if (job === null && held.loading()) return;
      // `untracked` because settling the job reads the two signals it is about
      // to fill, to ask each question exactly once. Tracked, this effect would
      // depend on its own answers and re-run on each of them.
      untracked(() => this.onJob(job));
    });

    effect(() => {
      const held = this.heldEvents();
      if (!held) return;
      const failure = held.error();
      if (failure) {
        this.eventsFailure.set(failure.message);
        return;
      }
      const events = held.data();
      // Not yet delivered, which is not the same as delivered and empty. The
      // log is read to find out why a job stopped, and "nothing recorded yet"
      // is an answer where "still loading" is not.
      if (events === null) return;
      this.events.set(events);
    });

    this.ticker = setInterval(() => this.now.set(Date.now()), 5_000);
  }

  ngOnDestroy(): void {
    this.heldJob()?.release();
    this.heldEvents()?.release();
    if (this.ticker) clearInterval(this.ticker);
  }

  /**
   * One delivery of the job document, or null for a job that is not there.
   *
   * The source is resolved lazily and only once: it is written by DOWNLOAD, so
   * it does not exist for a job that has not got that far, and it never changes
   * once it does.
   *
   * The results are asked for only once nothing more can be produced. Polled on
   * every job update they would run two queries per heartbeat for an answer
   * that is still being written; a finished job's output does not change.
   */
  private onJob(job: Job | null): void {
    this.job.set(job);
    if (!job) return;

    const sourceId = job.sourceId;
    if (sourceId && !this.source()) {
      void this.sources
        .loadSource(sourceId)
        .then((found) => this.source.set(found))
        .catch(() => this.source.set(null));
    }

    if (this.terminal(job) && !this.results()) {
      void this.jobs
        .loadJobResults(job.id)
        .then((found) => this.results.set(found))
        .catch((err: unknown) => this.error.set(err instanceof Error ? err.message : String(err)));
    }
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

  /** Shared with the queue card, which gates Delete on the same answer. */
  protected terminal(job: Job): boolean {
    return isTerminal(job);
  }

  /** What a stage says it is doing, when that is still true of it. */
  protected note(stage: Stage): string | null {
    return stageNote(stage);
  }

  /** The submission, the question, or the theme — whichever this job carries. */
  protected title(job: Job): string {
    return jobTitle(job);
  }

  /** Whether a research run has written its list yet. */
  protected researched(job: Job): boolean {
    return job.stages.some((stage: Stage) => stage.name === 'RESEARCH' && stage.status === 'DONE');
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
      await this.jobs.cancel(job.id);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.cancelling.set(false);
    }
  }

  /**
   * Forget this job. Its event log is left behind.
   *
   * Records only, and not even all of the records: `events` is a subcollection,
   * which a document delete does not touch and the rules will not let a client
   * clear. Whatever the job downloaded or rendered stays on the machine that
   * holds it — see Settings ▸ Storage, which is the only surface that can reach
   * a particular disk.
   *
   * Offered only on a job that has stopped. See `isTerminal`.
   */
  protected async removeJob(): Promise<void> {
    const job = this.job();
    if (!job || !this.terminal(job)) return;
    if (
      !confirm(
        `Delete job ${job.id}? Its event log is left behind in the database, and the files ` +
          `it produced stay on disk.`,
      )
    ) {
      return;
    }
    this.busy.set(true);
    this.error.set(null);
    try {
      await this.jobs.deleteJob(job.id);
      await this.router.navigate(['/jobs']);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.busy.set(false);
    }
  }

  /**
   * Forget the source and everything cut from it.
   *
   * Counted before it is offered, because "delete this source" and "delete this
   * source, eleven clips and forty candidates" are different decisions and only
   * one of them was on the button.
   */
  protected async removeSource(): Promise<void> {
    const source = this.source();
    if (!source) return;
    this.busy.set(true);
    this.error.set(null);
    try {
      const { clips, candidates } = await this.sources.sourceFootprint(source.id);
      const what = [
        `the source "${source.title ?? source.id}"`,
        clips ? `${clips} clip(s)` : '',
        candidates ? `${candidates} candidate(s)` : '',
      ]
        .filter(Boolean)
        .join(', ');
      if (!confirm(`Delete ${what}? The media stays on disk until you remove it in Storage.`)) {
        return;
      }
      await this.sources.deleteSource(source.id);
      this.source.set(null);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.busy.set(false);
    }
  }

  /**
   * Which attempt this is, out of how many it may have.
   *
   * `attempts` counts the ones already SPENT, so a running job is on the next
   * one and a settled job is on its last. Rendering `attempts + 1`
   * unconditionally was right for the first case and overshot the second — a
   * job that had used its single attempt displayed "2 of 1", which reads like a
   * bug in the scheduler rather than in this line.
   *
   * There was a real bug underneath it, which is why the wrong label was worth
   * following: reclaiming an expired lease costs an attempt and was not
   * checking the budget, so a job created with `maxAttempts: 1` genuinely did
   * run twice. Fixed in `lease.is_claimable`; this now cannot exceed the max
   * because nothing can.
   */
  protected attemptLabel(job: Job): string {
    const spent = job.attempts ?? 0;
    const max = job.maxAttempts ?? 1;
    const running = job.status === 'RUNNING';
    return `${Math.min(running ? spent + 1 : Math.max(spent, 1), max)} of ${max}`;
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
