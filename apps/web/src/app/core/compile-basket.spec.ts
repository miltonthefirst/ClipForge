import { beforeEach, describe, expect, it } from 'vitest';

import { BASKET_LIMIT, CompileBasketService } from './compile-basket';

/**
 * The basket: what it holds, what it refuses, and what survives a reload.
 *
 * Storage is exercised for real — jsdom has a localStorage — because the
 * failure worth catching is a basket that forgets on the way to the Compile
 * page, which is exactly the trip it exists to survive.
 */

const STORAGE_KEY = 'clipforge.compile-basket';

/** A new service instance, as a fresh page load would construct one. */
function fresh(): CompileBasketService {
  return new CompileBasketService();
}

const video = (n: number) => ({
  url: `https://youtu.be/v${n}`,
  title: `Video ${n}`,
  channel: null,
});

describe('CompileBasketService', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('starts empty and not ready', () => {
    const basket = fresh();
    expect(basket.count()).toBe(0);
    expect(basket.ready()).toBe(false);
  });

  it('is ready with two videos and a theme, and not before', () => {
    const basket = fresh();
    basket.add(video(1));
    basket.setTheme('goals');
    expect(basket.ready()).toBe(false);
    basket.add(video(2));
    expect(basket.ready()).toBe(true);
  });

  it('refuses a duplicate and says so', () => {
    const basket = fresh();
    expect(basket.add(video(1))).toBe(true);
    expect(basket.add(video(1))).toBe(false);
    expect(basket.count()).toBe(1);
  });

  it('refuses a thirteenth video, which the contract would refuse too', () => {
    const basket = fresh();
    for (let n = 0; n < BASKET_LIMIT; n += 1) basket.add(video(n));
    expect(basket.full()).toBe(true);
    expect(basket.add(video(99))).toBe(false);
  });

  it('toggles in and out', () => {
    const basket = fresh();
    expect(basket.toggle(video(1))).toBe(true);
    expect(basket.has(video(1).url)).toBe(true);
    expect(basket.toggle(video(1))).toBe(false);
    expect(basket.has(video(1).url)).toBe(false);
  });

  it('survives a reload', () => {
    const basket = fresh();
    basket.add(video(1));
    basket.setTheme('goals');
    basket.setTitle('Goals!');
    const again = fresh();
    expect(again.items()).toEqual([video(1)]);
    expect(again.theme()).toBe('goals');
    expect(again.title()).toBe('Goals!');
  });

  it('starting from a trend replaces what was there', () => {
    const basket = fresh();
    basket.add(video(1));
    basket.setTheme('old');
    basket.startFrom('trend-1', 'new theme', 'New title', [video(2), video(3)]);
    expect(basket.items().map((item) => item.url)).toEqual([video(2).url, video(3).url]);
    expect(basket.theme()).toBe('new theme');
    expect(basket.title()).toBe('New title');
    expect(basket.trendId()).toBe('trend-1');
  });

  it('ignores storage it cannot read rather than failing to construct', () => {
    localStorage.setItem(STORAGE_KEY, '{not json');
    const basket = fresh();
    expect(basket.count()).toBe(0);
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ items: [{ url: '' }, { nope: 1 }, 7] }));
    expect(fresh().count()).toBe(0);
  });

  it('clears everything, including the trend it came from', () => {
    const basket = fresh();
    basket.startFrom('trend-1', 'theme', null, [video(1), video(2)]);
    basket.clear();
    expect(basket.count()).toBe(0);
    expect(basket.theme()).toBe('');
    expect(basket.trendId()).toBeNull();
    expect(fresh().count()).toBe(0);
  });
});
