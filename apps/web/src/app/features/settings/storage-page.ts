import { DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import {
  LocalApiService,
  type StorageFile,
  type StorageReport,
  type TrashItem,
} from '../../core/local-api';

/**
 * What is actually on this machine, and how to get rid of it.
 *
 * **Deliberately not built from Firestore.** Deleting a clip's record and
 * removing the 400 MB behind it are separate acts, so by the time somebody
 * wants to reclaim disk the record may be long gone — and a storage screen
 * built from Firestore could never show the file it left behind. This walks the
 * worker's disk instead, so what it lists is what is there.
 *
 * It follows that this only works where the files are. The local API answers on
 * loopback and hands out its token through Tauri, so this page is functional in
 * the desktop app and inert in a browser on a phone. That is the correct shape
 * rather than a limitation: a phone cannot free space on a computer it is not.
 */
@Component({
  selector: 'app-storage-page',
  imports: [DecimalPipe, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './storage-page.html',
})
export class StoragePage {
  private readonly local = inject(LocalApiService);

  protected readonly report = signal<StorageReport | null>(null);
  protected readonly error = signal<string | null>(null);
  protected readonly busy = signal(false);
  protected readonly tab = signal<'files' | 'trash'>('files');

  /** Paths ticked for removal, and trash ids ticked for restore or purge. */
  protected readonly picked = signal<ReadonlySet<string>>(new Set());

  protected readonly files = computed<StorageFile[]>(() => {
    const report = this.report();
    if (!report) return [];
    // Biggest first. The screen exists to reclaim space, and a 400 MB source is
    // worth more attention than sixty 6 MB clips however many of them there are.
    return [...report.sources, ...report.clips].sort((a, b) => b.sizeBytes - a.sizeBytes);
  });

  protected readonly trash = computed<TrashItem[]>(() => this.report()?.trash ?? []);

  protected readonly pickedBytes = computed(() => {
    const chosen = this.picked();
    const rows = this.tab() === 'files' ? this.files() : this.trash();
    return rows
      .filter((row) => chosen.has(this.keyOf(row)))
      .reduce((total, row) => total + row.sizeBytes, 0);
  });

  constructor() {
    void this.refresh();
  }

  protected keyOf(row: StorageFile | TrashItem): string {
    return 'path' in row ? row.path : row.id;
  }

  protected isPicked(row: StorageFile | TrashItem): boolean {
    return this.picked().has(this.keyOf(row));
  }

  protected toggle(row: StorageFile | TrashItem): void {
    const key = this.keyOf(row);
    const next = new Set(this.picked());
    if (!next.delete(key)) next.add(key);
    this.picked.set(next);
  }

  protected pickAll(): void {
    const rows = this.tab() === 'files' ? this.files() : this.trash();
    const all = this.picked().size === rows.length;
    this.picked.set(all ? new Set() : new Set(rows.map((row) => this.keyOf(row))));
  }

  protected show(tab: 'files' | 'trash'): void {
    this.tab.set(tab);
    // Cleared on every switch: the two tabs key on different things — a path
    // and a trash id — and carrying a selection across would silently act on
    // rows the operator cannot see.
    this.picked.set(new Set());
  }

  protected async refresh(): Promise<void> {
    await this.run(async () => {
      this.report.set(await this.local.storage());
      this.picked.set(new Set());
    });
  }

  /** Move what is ticked into the bin. Reversible, and nothing is deleted. */
  protected async remove(): Promise<void> {
    const paths = [...this.picked()];
    if (!paths.length) return;
    await this.run(async () => {
      this.report.set(await this.local.moveToTrash(paths));
      this.picked.set(new Set());
    });
  }

  protected async restore(): Promise<void> {
    const ids = [...this.picked()];
    if (!ids.length) return;
    await this.run(async () => {
      this.report.set(await this.local.restoreFromTrash(ids));
      this.picked.set(new Set());
    });
  }

  /**
   * Delete for good.
   *
   * The only irreversible thing on this page, so it is the only one that asks —
   * and it asks with the number of files and the space involved, because
   * "delete 34 items" and "delete 12 GB" are answered differently.
   */
  protected async purge(all = false): Promise<void> {
    const ids = all ? [] : [...this.picked()];
    if (!all && !ids.length) return;

    const count = all ? this.trash().length : ids.length;
    const bytes = all ? (this.report()?.trashBytes ?? 0) : this.pickedBytes();
    const size = (bytes / 1024 / 1024 / 1024).toFixed(2);
    if (
      !confirm(
        `Permanently delete ${count} item(s), freeing about ${size} GB? This cannot be undone.`,
      )
    ) {
      return;
    }

    await this.run(async () => {
      this.report.set(await this.local.purgeTrash(ids, all));
      this.picked.set(new Set());
    });
  }

  private async run(action: () => Promise<void>): Promise<void> {
    this.busy.set(true);
    this.error.set(null);
    try {
      await action();
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.busy.set(false);
    }
  }

  protected gb(bytes: number): number {
    return bytes / 1024 / 1024 / 1024;
  }

  protected mb(bytes: number): number {
    return bytes / 1024 / 1024;
  }
}
