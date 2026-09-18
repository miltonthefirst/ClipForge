import { Injectable, computed, signal } from '@angular/core';

/**
 * The videos somebody is gathering into one compilation.
 *
 * Held in memory and mirrored to this browser's storage, so a basket survives
 * a reload and a trip to the Jobs page — but never reaches another device.
 * That is the right scope: a basket is a half-made decision, and a half-made
 * decision showing up on the phone is confusing rather than helpful. The
 * finished decision is a COMPILE job, and that goes everywhere.
 *
 * Storage can be absent, full or refused, so every touch of it is wrapped and
 * the basket works from memory alone when it is.
 */

export interface BasketItem {
  readonly url: string;
  readonly title: string | null;
  readonly channel: string | null;
}

/** The contract's ceiling on items. Twelve segments of five seconds is a minute. */
export const BASKET_LIMIT = 12;

const STORAGE_KEY = 'clipforge.compile-basket';

interface Stored {
  readonly items: BasketItem[];
  readonly theme: string;
  readonly title: string;
  readonly trendId: string | null;
}

@Injectable({ providedIn: 'root' })
export class CompileBasketService {
  readonly items = signal<BasketItem[]>([]);
  readonly theme = signal('');
  readonly title = signal('');
  /** The trend this basket was started from, when it was. Provenance only. */
  readonly trendId = signal<string | null>(null);

  readonly count = computed(() => this.items().length);
  readonly full = computed(() => this.items().length >= BASKET_LIMIT);
  /** Two is the contract's minimum: one video is a clip, not a compilation. */
  readonly ready = computed(() => this.items().length >= 2 && this.theme().trim().length > 0);

  constructor() {
    this.restore();
  }

  has(url: string): boolean {
    return this.items().some((item) => item.url === url);
  }

  /**
   * Add one video. Returns false when it was already there or the basket is
   * full, so the caller can say which rather than silently doing nothing.
   */
  add(item: BasketItem): boolean {
    if (this.has(item.url) || this.full()) return false;
    this.items.update((items) => [...items, item]);
    this.persist();
    return true;
  }

  remove(url: string): void {
    this.items.update((items) => items.filter((item) => item.url !== url));
    this.persist();
  }

  toggle(item: BasketItem): boolean {
    if (this.has(item.url)) {
      this.remove(item.url);
      return false;
    }
    return this.add(item);
  }

  /**
   * Seed the basket from a trend: its best videos, its title, its id.
   *
   * Replaces what was there. Starting a compilation from a trend is a fresh
   * intention, and merging it into a basket left over from yesterday would
   * produce a video about two things.
   */
  startFrom(trendId: string, theme: string, title: string | null, items: BasketItem[]): void {
    this.items.set(items.slice(0, BASKET_LIMIT));
    this.theme.set(theme);
    this.title.set(title ?? '');
    this.trendId.set(trendId);
    this.persist();
  }

  setTheme(theme: string): void {
    this.theme.set(theme);
    this.persist();
  }

  setTitle(title: string): void {
    this.title.set(title);
    this.persist();
  }

  clear(): void {
    this.items.set([]);
    this.theme.set('');
    this.title.set('');
    this.trendId.set(null);
    this.persist();
  }

  private persist(): void {
    const stored: Stored = {
      items: this.items(),
      theme: this.theme(),
      title: this.title(),
      trendId: this.trendId(),
    };
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(stored));
    } catch {
      // Private mode, a full quota, or no storage at all: the basket still
      // works for this page load, which is most of what it is for.
    }
  }

  private restore(): void {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      const parsed = JSON.parse(raw) as Partial<Stored>;
      const items = Array.isArray(parsed.items)
        ? parsed.items
            .filter(
              (item): item is BasketItem => typeof item?.url === 'string' && item.url.length > 0,
            )
            .map((item) => ({
              url: item.url,
              title: typeof item.title === 'string' ? item.title : null,
              channel: typeof item.channel === 'string' ? item.channel : null,
            }))
        : [];
      this.items.set(items.slice(0, BASKET_LIMIT));
      this.theme.set(typeof parsed.theme === 'string' ? parsed.theme : '');
      this.title.set(typeof parsed.title === 'string' ? parsed.title : '');
      this.trendId.set(typeof parsed.trendId === 'string' ? parsed.trendId : null);
    } catch {
      // Unreadable is the same as absent.
    }
  }
}
