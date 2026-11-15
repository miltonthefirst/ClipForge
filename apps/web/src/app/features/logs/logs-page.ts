import { DatePipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  computed,
  inject,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import type { LogGroup } from '@clipforge/contracts';

import { LogsRepository } from '../../core/data/logs';
import { SessionService } from '../../core/session';
import { buildLogExportMarkdown } from './log-export.util';

type Show = 'unresolved' | 'resolved' | 'all';
type Sort = 'recent' | 'count';

/**
 * Errors from the worker and the agent, grouped (the logging blueprint, §7).
 * Admins only, in the rules as well as the menu.
 */
@Component({
  selector: 'app-logs-page',
  imports: [DatePipe, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './logs-page.html',
})
export class LogsPage implements OnDestroy {
  private readonly repository = inject(LogsRepository);
  private readonly session = inject(SessionService);

  private readonly live = this.repository.groups();
  protected readonly loadError = this.live.error;
  protected readonly show = signal<Show>('unresolved');
  protected readonly app = signal<'all' | 'worker' | 'agent'>('all');
  protected readonly sort = signal<Sort>('recent');
  protected readonly busy = signal(false);
  protected readonly copied = signal(false);

  protected readonly groups = computed<LogGroup[] | null>(() => {
    const all = this.live.data();
    if (all === null) return null;
    const show = this.show();
    const app = this.app();
    const shown = all.filter(
      (group) => (show === 'all' || group.status === show) && (app === 'all' || group.app === app),
    );
    return this.sort() === 'count' ? [...shown].sort((a, b) => b.count - a.count) : shown;
  });

  protected readonly unresolvedShown = computed(
    () => (this.groups() ?? []).filter((group) => group.status === 'unresolved').length,
  );

  ngOnDestroy(): void {
    this.live.release();
  }

  protected async resolveShown(): Promise<void> {
    const uid = this.session.uid;
    const open = (this.groups() ?? []).filter((group) => group.status === 'unresolved');
    if (!uid || open.length === 0) return;
    this.busy.set(true);
    try {
      await this.repository.resolve(
        open.map((group) => group.fingerprint),
        uid,
      );
    } finally {
      this.busy.set(false);
    }
  }

  /** Every group shown, as Markdown, ready to paste into a chat for triage. */
  protected async copyShown(): Promise<void> {
    const groups = (this.groups() ?? []).map((group) => ({ ...group, occurrences: [] }));
    const text = buildLogExportMarkdown(
      groups,
      { app: this.app(), unresolvedOnly: this.show() === 'unresolved' },
      new Date(),
    );
    try {
      await navigator.clipboard.writeText(text);
      this.copied.set(true);
      setTimeout(() => this.copied.set(false), 2_000);
    } catch {
      // The clipboard can refuse after a slow fetch; nothing else to do here.
    }
  }
}
