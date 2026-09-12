import { Injectable, signal } from '@angular/core';

/**
 * Three states, not two.
 *
 * "System" is a real choice and the default one: it means *keep following the
 * OS*, so a phone that turns dark at sunset takes the app with it. Collapsing it
 * into a boolean would make the first tap a permanent opt-out of that, which is
 * not what tapping a toggle once is meant to mean.
 */
export type ThemeChoice = 'system' | 'light' | 'dark';

export const THEME_STORAGE_KEY = 'clipforge.theme';

/** The order the toggle cycles through. */
export const THEME_ORDER: readonly ThemeChoice[] = ['system', 'light', 'dark'];

/**
 * `--cf-canvas` in each theme, duplicated here for the `theme-color` meta.
 *
 * The meta needs a literal colour, and the token it must match lives in CSS. A
 * `getComputedStyle` read would avoid the duplication but forces a layout on
 * every toggle to fetch a value that changes about twice a year;
 * `theme.spec.ts` asserts these two still match the stylesheet.
 */
export const LIGHT_CANVAS = '#f8fafc';
export const DARK_CANVAS = '#020617';

/**
 * Reads and writes the one thing that decides the palette: `color-scheme`.
 *
 * Every colour token in styles.css is a `light-dark()` pair, so the entire theme
 * follows from `data-theme` on the root element. This service owns that
 * attribute and nothing else — there is no per-component theme state to keep in
 * step, and no second place a colour could be decided.
 */
@Injectable({ providedIn: 'root' })
export class ThemeService {
  private readonly current = signal<ThemeChoice>('system');

  /** The user's choice — `system` included, which is not the same as its result. */
  readonly choice = this.current.asReadonly();

  constructor() {
    this.current.set(readStoredChoice());
    this.apply(this.current());

    // Enable transitions only now. Before this the page may not have painted,
    // and animating from an unstyled document looks like a bug.
    document.documentElement.setAttribute('data-theme-ready', '');
  }

  set(choice: ThemeChoice): void {
    this.current.set(choice);
    this.apply(choice);

    try {
      if (choice === 'system') {
        localStorage.removeItem(THEME_STORAGE_KEY);
      } else {
        localStorage.setItem(THEME_STORAGE_KEY, choice);
      }
    } catch {
      // Private browsing, or storage disabled. The theme still applies for this
      // session; only remembering it is lost, which is not worth failing over.
    }
  }

  /** Advance to the next choice. What the header button does. */
  cycle(): ThemeChoice {
    const next = THEME_ORDER[(THEME_ORDER.indexOf(this.current()) + 1) % THEME_ORDER.length];
    this.set(next);
    return next;
  }

  /** What the choice currently resolves to, following the OS when it is `system`. */
  resolved(): 'light' | 'dark' {
    const choice = this.current();
    if (choice !== 'system') return choice;
    return matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  }

  private apply(choice: ThemeChoice): void {
    const root = document.documentElement;
    if (choice === 'system') {
      root.removeAttribute('data-theme');
    } else {
      root.setAttribute('data-theme', choice);
    }
    this.syncBrowserChrome(choice);
  }

  /**
   * Keep the browser's own chrome in step — the Android address bar, and the
   * status bar area of the installed PWA.
   *
   * index.html carries two `theme-color` metas scoped by `prefers-color-scheme`,
   * which is right for `system` and right before this code has run. They are
   * wrong the moment someone forces a theme the OS disagrees with: the page goes
   * light while the status bar stays dark.
   *
   * The spec says the browser takes the FIRST `theme-color` whose media matches,
   * so an override has to be inserted ahead of them rather than appended, and
   * removed again when the choice goes back to `system`.
   */
  private syncBrowserChrome(choice: ThemeChoice): void {
    const id = 'cf-theme-color';
    document.getElementById(id)?.remove();
    if (choice === 'system') return;

    const meta = document.createElement('meta');
    meta.id = id;
    meta.name = 'theme-color';
    meta.content = choice === 'light' ? LIGHT_CANVAS : DARK_CANVAS;
    document.head.prepend(meta);
  }
}

/**
 * The stored choice, or `system`.
 *
 * Shared with the inline pre-paint script in index.html, which cannot import
 * from here — it has to run before the bundle exists. The key and the accepted
 * values are the contract between the two, and `apps/web/e2e/theme.spec.ts`
 * asserts they still agree.
 */
export function readStoredChoice(): ThemeChoice {
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    return stored === 'light' || stored === 'dark' ? stored : 'system';
  } catch {
    return 'system';
  }
}
