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
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import type { Job } from '@clipforge/contracts';

import {
  JOB_TABS,
  isTerminal,
  jobNote,
  jobTab,
  jobTabSpec,
  queueFailure,
  stageProgress,
  type JobTab,
  type StageProgress,
} from '../../core/job-list';
import { SessionService } from '../../core/session';
import { JOB_PAGE, JobsRepository } from '../../core/data/jobs';
import type { Live } from '../../core/firestore/gateway';
import { WorkerPanel } from './worker-panel';

/** One card, with everything the template needs already worked out. */
export interface JobRow {
  readonly job: Job;
  readonly progress: StageProgress;
  /** What the running stage — or the one that died — last said it was doing. */
  readonly note: string | null;
  readonly cancellable: boolean;
  readonly deletable: boolean;
  readonly tone: string;
}

@Component({
  selector: 'app-jobs-page',
  imports: [FormsModule, RouterLink, WorkerPanel],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './jobs-page.html',
})
export class JobsPage implements OnDestroy {
  private readonly data = inject(JobsRepository);
  private readonly session = inject(SessionService);

  /**
   * The selected tab, from `?tab=` — bound by `withComponentInputBinding`, the
   * same mechanism that feeds `:id` to the detail page.
   *
   * In the URL rather than in a signal so that reloading, sharing or coming
   * back from a job lands on the list the operator was actually looking at. It
   * is typed as a bare string because the router sets every declared input on
   * every navigation, including to `undefined` when the parameter is absent, so
   * an `input('active')` default would be overwritten rather than honoured.
   */
  readonly tab = input<string>();

  protected readonly tabs = JOB_TABS;
  protected readonly selected = computed(() => jobTab(this.tab()));

  protected readonly jobs = signal<Job[] | null>(null);
  protected readonly counts = signal<Record<JobTab, number> | null>(null);
  protected readonly submission = signal('');
  protected readonly error = signal<string | null>(null);
  /**
   * Why the list is not here, when it is never going to arrive.
   *
   * A third state, distinct from "still loading" and from "loaded and empty",
   * because the page could previously only express two. A failed query delivers
   * nothing at all, so `jobs` stayed null and the page went on saying "Loading
   * the queue…" for as long as anyone left it open.
   *
   * Separate from {@link error}, which is the banner for something the operator
   * just pressed. This one replaces the list, because it is about the list.
   */
  protected readonly loadFailure = signal<string | null>(null);
  protected readonly submitting = signal(false);
  /**
   * The job an action is in flight for, so only its own button goes quiet.
   *
   * Per row, and the handlers below are guarded the same way — by the disabled
   * attribute and nothing else, as the admin and review pages are. They used to
   * open with a global `if (busy()) return`, which disagreed with the template:
   * while one job was cancelling every *other* row's Cancel and Delete stayed
   * enabled, and pressing one did nothing at all — no write, no error, no
   * change on screen.
   */
  protected readonly busy = signal<string | null>(null);

  /**
   * How many jobs are waiting, so the worker panel can say what that means.
   *
   * Counted across the whole collection rather than taken from the list in
   * front of you. It drives "N jobs waiting with nothing to run them", and
   * Completed and Failed deliver no queued job at all — a banner fed from the
   * visible rows would read zero on two tabs out of four. Counting it
   * separately also fixes what was already wrong: the old count only saw queued
   * jobs inside the newest 25.
   *
   * It is as live as {@link refreshCounts} is, which is not the same on every
   * tab. Active and All deliver a newly queued job to this page's listener, so
   * there the banner appears by itself; Completed and Failed do not, so there
   * it is right as of the moment the tab was opened and moves again the next
   * time this page submits, cancels or deletes something — or the next time the
   * operator changes tab. Closing that gap means a second listener over QUEUED
   * running on every screen, and ADR-0004 makes a narrow `onSnapshot` a
   * requirement rather than a preference — every delivered document bills a
   * read. A count that is one tab-switch out of date is the cheaper half of
   * that trade, and the case the banner exists for — a queue with no worker —
   * does not clear itself while nobody is looking.
   */
  protected readonly queuedCount = signal(0);

  /**
   * The listener currently held, released when the tab changes or the page goes.
   *
   * A signal rather than a field because the effect below *reads* it: a plain
   * field is not tracked, so that effect would run once against no listener and
   * never again, and the tab counts would sit at zero for ever.
   */
  private readonly held = signal<Live<Job[]> | null>(null);

  constructor() {
    effect(() => {
      const uid = this.session.uid;
      // Cleared first, and unconditionally: a failure belongs to the listener
      // that produced it, and this effect is about to replace that listener —
      // including with no listener at all, on the way out of the app.
      this.loadFailure.set(null);

      // Let go of the previous tab's listener before taking the next one. The
      // gate keeps it warm for fifteen minutes, so coming back to this tab
      // re-attaches to the same listener and costs nothing — which is the whole
      // reason releasing is not the same as unsubscribing.
      // `untracked`, or this effect depends on the signal it is about to
      // write and re-runs itself for ever — releasing and re-opening a listener
      // on every pass, which is a hang rather than a leak.
      untracked(() => this.held())?.release();
      this.held.set(null);

      if (!uid) {
        this.jobs.set(null);
        return;
      }

      // Reading the tab here is what re-subscribes: switching tabs releases
      // this listener and takes the next one's query.
      const statuses = jobTabSpec(this.selected()).statuses;
      this.jobs.set(null);
      this.delivered = null;
      this.held.set(this.data.watchJobs(statuses));
    });

    // Separate from the subscription above so that a delivery does not re-open
    // a listener: this one reads the held signals and nothing else, and reading
    // `held` inside the effect that assigns it would make every snapshot a
    // reason to re-subscribe.
    effect(() => {
      const held = this.held();
      if (!held) return;
      const failure = held.error();
      if (failure) {
        this.onQueryFailed(failure);
        return;
      }
      const jobs = held.data();
      // Still loading is not the same as delivered-and-empty, and only the
      // second is news. Acting on the first is what put "Loading the queue…"
      // on screen for ever behind a query that had already failed.
      if (jobs === null) return;
      this.onJobs(jobs);
    });
  }

  ngOnDestroy(): void {
    this.held()?.release();
  }

  /**
   * What the last delivery held, as id:status pairs.
   *
   * A snapshot fires for every field the worker touches — a lease extended, an
   * attempt counted — and none of that moves a job between tabs. Comparing the
   * signature is what stops a 30-second heartbeat costing four aggregation
   * queries. `null` rather than `''` so the first delivery always counts, even
   * when it is empty.
   */
  private delivered: string | null = null;

  /**
   * A listener that is not going to deliver anything.
   *
   * `onSnapshot` does not retry after an error, so this is the end of the road
   * for this tab until a navigation re-subscribes it.
   *
   * The counts are asked for here, and that is the whole reason this is a method
   * rather than one line in the effect. They are aggregations with no ordering
   * to serve, so the missing composite index that stops the list does not stop
   * them — and one of them is the queued total under the worker panel. Refreshed
   * only from a successful delivery, it would sit at its initial zero and take
   * "N jobs waiting with nothing to run them" off the screen at exactly the
   * moment the screen has already failed to show the queue.
   */
  private onQueryFailed(error: Error): void {
    this.loadFailure.set(queueFailure(error));
    void this.refreshCounts();
  }

  private onJobs(jobs: Job[]): void {
    this.jobs.set(jobs);
    const signature = jobs
      .map((job) => `${job.id}:${job.status}`)
      .sort()
      .join(',');
    if (signature === this.delivered) return;
    this.delivered = signature;
    void this.refreshCounts();
  }

  /**
   * The numbers on the tabs, and the queued total under them.
   *
   * Refreshed when the visible list changes, and after anything this page does
   * that Firestore will not tell it about: a job submitted or deleted while a
   * different tab is open never reaches that tab's listener, so nothing else
   * would notice the count had moved.
   *
   * Failure is deliberately silent and leaves the previous numbers in place. A
   * count is a label on a tab; replacing the queue with an error because one
   * did not arrive would be the wrong trade, and a stale number is closer to
   * the truth than none.
   */
  private async refreshCounts(): Promise<void> {
    try {
      const [totals, queued] = await Promise.all([
        Promise.all(
          JOB_TABS.map(async (tab) => [tab.key, await this.data.countJobs(tab.statuses)] as const),
        ),
        this.data.countJobs(['QUEUED']),
      ]);
      this.counts.set(Object.fromEntries(totals) as Record<JobTab, number>);
      this.queuedCount.set(queued);
    } catch {
      // Left as they were, on purpose — see above.
    }
  }

  /**
   * The cards, in the order the listener delivered them.
   *
   * One computed rather than a handful of helpers the template calls per row:
   * the progress line alone used to work the stage list out three times a card
   * to read three fields off the same answer.
   */
  protected readonly rows = computed<JobRow[] | null>(() => {
    const jobs = this.jobs();
    if (jobs === null) return null;
    return jobs.map((job) => {
      const progress = stageProgress(job);
      return {
        job,
        progress,
        note: jobNote(job),
        // Mirrors firestore.rules: cancellation is the only transition a client
        // may drive, and only from these two states.
        cancellable: job.status === 'QUEUED' || job.status === 'RUNNING',
        // Terminal only. Deleting a job a worker still holds does not stop it —
        // the stage finishes and writes the document back, and the job returns
        // marked COMPLETED. See `isTerminal`.
        deletable: isTerminal(job),
        tone: statusTone(job),
      };
    });
  });

  /**
   * Whether there is genuinely nothing, or merely nothing on this tab.
   *
   * The counts land a moment after the list, and an unknown total reads as
   * "nothing at all" — which is what an empty first screen said before there
   * were tabs, and is corrected the instant the counts arrive.
   */
  protected readonly nothingAnywhere = computed(() => (this.counts()?.['all'] ?? 0) === 0);

  protected readonly emptyHere = computed(() => jobTabSpec(this.selected()).empty);

  /**
   * Said out loud, so a capped list is never mistaken for the whole of it.
   *
   * The count makes this better than the usual "50+": the page knows exactly
   * how many it is not showing, and can say so.
   */
  protected readonly truncation = computed<string | null>(() => {
    const rows = this.rows();
    if (!rows || rows.length < JOB_PAGE) return null;
    const total = this.counts()?.[this.selected()];
    if (total !== undefined && total <= rows.length) return null;
    return total === undefined
      ? `Showing the newest ${rows.length}; there are more.`
      : `Showing the newest ${rows.length} of ${total}.`;
  });

  protected async submit(): Promise<void> {
    const uid = this.session.uid;
    const value = this.submission().trim();
    if (!uid || !value) return;

    this.submitting.set(true);
    this.error.set(null);
    try {
      await this.data.submit(uid, value);
      this.submission.set('');
      void this.refreshCounts();
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.submitting.set(false);
    }
  }

  protected async cancel(row: JobRow): Promise<void> {
    this.busy.set(row.job.id);
    this.error.set(null);
    try {
      await this.data.cancel(row.job.id);
      void this.refreshCounts();
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.busy.set(null);
    }
  }

  /**
   * Forget a job from the list it is sitting in.
   *
   * Confirmed with the browser's own dialog, as the detail page does. There is
   * no toast and no modal in this app; what an error becomes is the banner at
   * the top of this page.
   */
  protected async remove(row: JobRow): Promise<void> {
    // Not an affordance check. The rules allow any approved user to delete any
    // job, so terminal-only is this side's rule and has to be enforced as well
    // as rendered: a row claimed by a worker between the render and the click
    // would otherwise delete a document the worker recreates, marked COMPLETED.
    // See `isTerminal`.
    if (!row.deletable) return;
    if (
      !confirm(
        `Delete job ${row.job.id}? Its event log is left behind in the database, and the ` +
          `files it produced stay on disk.`,
      )
    ) {
      return;
    }
    this.busy.set(row.job.id);
    this.error.set(null);
    try {
      await this.data.deleteJob(row.job.id);
      void this.refreshCounts();
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.busy.set(null);
    }
  }
}

function statusTone(job: Job): string {
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
