import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import type { CompileItem, CompileOptions, CompileTransition } from '@clipforge/contracts';

import { BASKET_LIMIT, CompileBasketService } from '../../core/compile-basket';
import { ResearchRepository } from '../../core/data/research';
import { SessionService } from '../../core/session';

/**
 * Several videos, one theme, one clip.
 *
 * The basket is filled on the Trends page or by pasting links here; this page
 * is where it becomes a job. The job is a COMPILE, which ingests every item
 * exactly as a CLIP job would, picks one moment from each for the theme, and
 * stitches them with a card in front — and what comes out is a clip in the
 * review queue like any other, with the list of pieces on its page.
 */
@Component({
  selector: 'app-compile-page',
  imports: [FormsModule, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './compile-page.html',
})
export class CompilePage {
  protected readonly basket = inject(CompileBasketService);
  private readonly research = inject(ResearchRepository);
  private readonly session = inject(SessionService);
  private readonly router = inject(Router);

  protected readonly limit = BASKET_LIMIT;
  protected readonly pasted = signal('');
  protected readonly length = signal(60);
  protected readonly maxSegment = signal(20);
  protected readonly captions = signal(true);
  protected readonly titleCard = signal(true);
  protected readonly transition = signal<CompileTransition>('FADE');
  protected readonly making = signal(false);
  protected readonly error = signal<string | null>(null);

  /** Roughly how the target divides up, so the numbers mean something before the render. */
  protected readonly perSegment = computed(() => {
    const count = this.basket.count();
    if (!count) return null;
    return Math.max(5, Math.min(this.maxSegment(), Math.round(this.length() / count)));
  });

  protected readonly canMake = computed(
    () => this.basket.ready() && !this.making() && this.length() >= 20 && this.length() <= 180,
  );

  /** A pasted link, or several on separate lines. */
  protected addPasted(): void {
    this.error.set(null);
    const urls = this.pasted()
      .split(/\s+/)
      .map((url) => url.trim())
      .filter(Boolean);
    let refused = 0;
    for (const url of urls) {
      if (!this.basket.add({ url, title: null, channel: null })) refused += 1;
    }
    if (refused) {
      this.error.set(
        refused === urls.length
          ? `Nothing was added: already in the list, or the list is full at ${BASKET_LIMIT}.`
          : `${refused} of those were already in the list or would not fit.`,
      );
    }
    this.pasted.set('');
  }

  protected remove(url: string): void {
    this.basket.remove(url);
  }

  protected async make(): Promise<void> {
    const uid = this.session.uid;
    if (!uid || !this.canMake()) return;
    const options: CompileOptions = {
      theme: this.basket.theme().trim(),
      title: this.basket.title().trim() || null,
      // The contract types a bounded list as a union of tuples; the basket
      // enforces the bound (two to twelve) and `ready()` gates this call.
      items: this.basket.items().map((item): CompileItem => ({
        submission: item.url,
        startSec: null,
        endSec: null,
        note: item.title ? item.title.slice(0, 300) : null,
      })) as CompileOptions['items'],
      targetDurationSec: Math.round(this.length()),
      maxSegmentSec: Math.round(this.maxSegment()),
      captions: this.captions(),
      titleCard: this.titleCard(),
      transition: this.transition(),
      trendId: this.basket.trendId(),
    };
    this.making.set(true);
    this.error.set(null);
    try {
      const id = await this.research.startCompile(uid, options);
      this.basket.clear();
      await this.router.navigate(['/jobs', id]);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.making.set(false);
    }
  }
}
