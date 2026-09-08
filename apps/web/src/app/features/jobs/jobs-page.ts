import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  effect,
  inject,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import type { Job, Stage } from '@clipforge/contracts';

import { SessionService } from '../../core/session';
import { ClipForgeStore } from '../../core/store';

@Component({
  selector: 'app-jobs-page',
  imports: [FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './jobs-page.html',
})
export class JobsPage implements OnDestroy {
  private readonly store = inject(ClipForgeStore);
  private readonly session = inject(SessionService);
  private stop: (() => void) | null = null;

  protected readonly jobs = signal<Job[] | null>(null);
  protected readonly submission = signal('');
  protected readonly error = signal<string | null>(null);
  protected readonly submitting = signal(false);

  constructor() {
    effect((onCleanup) => {
      const uid = this.session.uid;
      if (!uid) {
        this.jobs.set(null);
        return;
      }
      const watch = this.store.watchJobs(uid, (err) => this.error.set(err.message));
      this.stop = watch.stop;
      onCleanup(() => watch.stop());
      effect(() => this.jobs.set(watch.jobs()));
    });
  }

  ngOnDestroy(): void {
    this.stop?.();
  }

  protected async submit(): Promise<void> {
    const uid = this.session.uid;
    const value = this.submission().trim();
    if (!uid || !value) return;

    this.submitting.set(true);
    this.error.set(null);
    try {
      await this.store.submit(uid, value);
      this.submission.set('');
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.submitting.set(false);
    }
  }

  protected async cancel(job: Job): Promise<void> {
    try {
      await this.store.cancel(job.id);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    }
  }

  protected cancellable(job: Job): boolean {
    // Mirrors firestore.rules: cancellation is the only transition a client may
    // drive, and only from these two states.
    return job.status === 'QUEUED' || job.status === 'RUNNING';
  }

  /**
   * Per-stage progress, which is what the checkpointed stage model buys the UI:
   * "TRANSCRIBE, 2 of 4" rather than an indeterminate spinner.
   */
  protected progress(job: Job): { done: number; total: number; current: string | null } {
    const settled = job.stages.filter(
      (s: Stage) => s.status === 'DONE' || s.status === 'SKIPPED',
    ).length;
    const running = job.stages.find((s: Stage) => s.status === 'RUNNING');
    return { done: settled, total: job.stages.length, current: running?.name ?? null };
  }

  protected statusTone(job: Job): string {
    switch (job.status) {
      case 'COMPLETED':
        return 'bg-emerald-500/15 text-emerald-400';
      case 'FAILED':
        return 'bg-red-500/15 text-red-400';
      case 'RUNNING':
        return 'bg-forge-500/15 text-forge-400';
      case 'CANCELLED':
        return 'bg-slate-700/40 text-slate-400';
      default:
        return 'bg-slate-700/40 text-slate-300';
    }
  }
}
