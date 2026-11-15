import { DatePipe } from '@angular/common';
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
import type { LogGroup, LogOccurrence } from '@clipforge/contracts';

import { LogsRepository } from '../../core/data/logs';
import type { Live } from '../../core/firestore/gateway';
import { SessionService } from '../../core/session';
import { buildLogExportMarkdown } from './log-export.util';

/** One error group: what it says, where, how often, and what to do about it. */
@Component({
  selector: 'app-log-page',
  imports: [DatePipe, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './log-page.html',
})
export class LogPage implements OnDestroy {
  /** The fingerprint, from the route (`logs/:id`). */
  readonly id = input.required<string>();

  private readonly repository = inject(LogsRepository);
  private readonly session = inject(SessionService);
  private readonly router = inject(Router);

  private readonly heldGroup = signal<Live<LogGroup> | null>(null);
  private readonly heldOccurrences = signal<Live<LogOccurrence[]> | null>(null);
  protected readonly group = computed(() => this.heldGroup()?.data() ?? null);
  protected readonly loading = computed(() => this.heldGroup()?.loading() ?? true);
  protected readonly occurrences = computed(() => this.heldOccurrences()?.data() ?? []);
  protected readonly busy = signal(false);
  protected readonly confirmDelete = signal(false);
  protected readonly copied = signal(false);

  constructor() {
    effect(() => {
      const id = this.id();
      untracked(() => {
        this.release();
        this.heldGroup.set(this.repository.group(id));
        this.heldOccurrences.set(this.repository.occurrences(id));
      });
    });
  }

  ngOnDestroy(): void {
    this.release();
  }

  protected extraOf(occurrence: LogOccurrence): [string, string][] {
    return Object.entries(occurrence.extra ?? {});
  }

  protected async resolve(): Promise<void> {
    const group = this.group();
    const uid = this.session.uid;
    if (!group || !uid) return;
    await this.run(() =>
      group.status === 'resolved'
        ? this.repository.reopen(group.fingerprint)
        : this.repository.resolve([group.fingerprint], uid),
    );
  }

  protected async toggleWatch(): Promise<void> {
    const group = this.group();
    if (group) await this.run(() => this.repository.watch(group.fingerprint, !group.watched));
  }

  protected async remove(): Promise<void> {
    const group = this.group();
    if (!group) return;
    if (!this.confirmDelete()) {
      this.confirmDelete.set(true);
      return;
    }
    const ids = this.occurrences()
      .map((occurrence) => occurrence.id)
      .filter((id): id is string => !!id);
    await this.run(() => this.repository.remove(group.fingerprint, ids));
    await this.router.navigateByUrl('/logs');
  }

  protected async copy(): Promise<void> {
    const group = this.group();
    if (!group) return;
    const text = buildLogExportMarkdown(
      [{ ...group, occurrences: this.occurrences() }],
      null,
      new Date(),
    );
    try {
      await navigator.clipboard.writeText(text);
      this.copied.set(true);
      setTimeout(() => this.copied.set(false), 2_000);
    } catch {
      // Refused: nothing else to do.
    }
  }

  private async run(action: () => Promise<void>): Promise<void> {
    this.busy.set(true);
    try {
      await action();
    } finally {
      this.busy.set(false);
    }
  }

  private release(): void {
    this.heldGroup()?.release();
    this.heldOccurrences()?.release();
  }
}
