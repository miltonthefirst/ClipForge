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
import { RouterLink } from '@angular/router';
import type { Source, SourceKind, SourcePreview } from '@clipforge/contracts';

import { SourcesRepository } from '../../core/data/sources';
import type { Live } from '../../core/firestore/gateway';
import { SessionService } from '../../core/session';
import {
  SOURCE_TABS,
  byUse,
  describeUse,
  initial,
  isKept,
  readableLength,
  readableSize,
  sourceTab,
  type SourceTab,
} from '../../core/source-list';

/** One row, decorated once rather than three times from the template. */
interface SourceRow {
  readonly source: Source;
  readonly poster: string | null;
  readonly kept: boolean;
  readonly use: string;
  readonly size: string | null;
  readonly length: string | null;
  readonly letter: string;
}

/**
 * What is on this machine, and what has earned its place.
 *
 * ## Why the library is worth a screen at all
 *
 * Because sources are the expensive thing and the irreplaceable one. A download
 * is bandwidth and minutes; a video that has since been taken down is gone for
 * good. The workspace has a 60 GB budget and a collector that reclaims the
 * least-recently-used, so something is always on its way out — and until this
 * page there was no way to see what, or to say "not that one".
 *
 * ## Why the web can look and not touch
 *
 * The files are on the worker. A browser on a phone can read the records and
 * open the original on YouTube, and that is the whole of what it can honestly
 * offer: removing a file means deleting bytes it cannot reach. So Remove is
 * offered where it works — the desktop app, which talks to the worker over
 * loopback — and elsewhere the row says where to do it rather than presenting a
 * button that fails.
 */
@Component({
  selector: 'app-sources-page',
  imports: [RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './sources-page.html',
})
export class SourcesPage implements OnDestroy {
  private readonly data = inject(SourcesRepository);
  private readonly session = inject(SessionService);

  /** From the URL, so a tab survives a reload and a link can name one. */
  readonly tab = input<string | undefined>(undefined);
  protected readonly selected = computed<SourceTab>(() => sourceTab(this.tab()));
  protected readonly tabs = SOURCE_TABS;

  protected readonly sources = signal<Source[] | null>(null);
  protected readonly loadFailure = signal<string | null>(null);

  /**
   * Posters, fetched per row after the list arrives.
   *
   * Keyed by source id rather than held on the row, so a list that re-delivers
   * — a use counted, a source pinned — does not re-fetch every image it already
   * has. ~20 KB each; forty of them on every snapshot would be the reason this
   * page felt slow.
   */
  private readonly posters = signal<Record<string, string | null>>({});

  private readonly held = signal<Live<Source[]> | null>(null);

  constructor() {
    effect(() => {
      const uid = this.session.uid;
      const kind = this.selected();
      this.loadFailure.set(null);

      untracked(() => this.held())?.release();
      this.held.set(null);

      if (!uid) {
        this.sources.set(null);
        return;
      }
      this.sources.set(null);
      this.held.set(this.data.listSources(kind === 'all' ? undefined : (kind as SourceKind)));
    });

    effect(() => {
      const held = this.held();
      if (!held) return;
      const failure = held.error();
      if (failure) {
        this.loadFailure.set(failure.message);
        return;
      }
      const sources = held.data();
      // Not loaded is not the same as an empty library, and only the second is
      // something to tell the reviewer about.
      if (sources === null) return;
      this.sources.set(sources);
      void this.loadPosters(sources);
    });
  }

  ngOnDestroy(): void {
    this.held()?.release();
  }

  protected readonly rows = computed<SourceRow[] | null>(() => {
    const sources = this.sources();
    if (sources === null) return null;
    const posters = this.posters();
    return byUse(sources).map((source) => ({
      source,
      poster: posters[source.id] ?? null,
      kept: isKept(source),
      use: describeUse(source),
      size: readableSize(source.sizeBytes),
      length: readableLength(source.durationSec),
      letter: initial(source),
    }));
  });

  protected readonly empty = computed(() => this.rows()?.length === 0);

  /** The total on disk, so the 60 GB budget is a number and not a rumour. */
  protected readonly footprint = computed(() => {
    const rows = this.rows();
    if (!rows?.length) return null;
    const bytes = rows.reduce((total, row) => total + (row.source.sizeBytes ?? 0), 0);
    return readableSize(bytes);
  });

  private async loadPosters(sources: readonly Source[]): Promise<void> {
    const known = this.posters();
    const wanted = sources.filter((source) => !(source.id in known));
    if (wanted.length === 0) return;

    const found = await Promise.all(
      wanted.map(async (source) => {
        const preview = await this.data.loadPreview(source.id).catch(() => null);
        return [source.id, posterOf(preview)] as const;
      }),
    );
    this.posters.set({ ...this.posters(), ...Object.fromEntries(found) });
  }
}

function posterOf(preview: SourcePreview | null): string | null {
  return preview ? `data:image/jpeg;base64,${preview.posterBase64}` : null;
}
