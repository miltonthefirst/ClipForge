import { expect, test } from '@playwright/test';

import { candidate, clip, preview, signIn, wipe, write } from './helpers';

/**
 * Light and dark.
 *
 * Every colour resolves from one CSS custom property pair via `light-dark()`,
 * which `color-scheme` selects between — so what these assert is that
 * `color-scheme` ends up right, in each of the three states the toggle has, and
 * that it is right *before the first paint* rather than a frame later.
 *
 * The last one is the reason the inline script in index.html duplicates a few
 * lines of theme.ts. Two copies of a contract need a test that they still agree.
 */

const LIGHT_CANVAS = 'rgb(248, 250, 252)';
const DARK_CANVAS = 'rgb(2, 6, 23)';

// styles.css paints `body` from --cf-canvas directly, so this is the element
// that proves the token resolved — not a wrapper that happens to repeat it.
const canvasOf = 'body';

/**
 * Open the account menu and hand back the theme control inside it.
 *
 * The toggle lives in the avatar dropdown now: the top bar carries identity and
 * the account, and navigation moved to the sidebar. Clicking the control does
 * not close the menu, so repeated cycles need only one open.
 */
async function themeToggle(page: import('@playwright/test').Page) {
  // `menuitem`, not `button`. The element carries role="menuitem" for the
  // dropdown, and an explicit role replaces the implicit one — so a
  // getByRole('button') lookup matches nothing at all, however well-labelled it
  // is. Worth stating: the symptom was an empty locator, not a wrong name.
  const item = page.getByRole('menuitem', { name: /^Theme:/ });
  if (!(await item.isVisible().catch(() => false))) {
    await page.getByRole('button', { name: /Account menu/ }).click();
  }
  return item;
}

test.beforeEach(async () => {
  await wipe();
});

test.describe('following the system', () => {
  test.use({ colorScheme: 'dark' });

  test('uses the dark palette when the OS asks for dark', async ({ page }) => {
    await signIn(page);
    await expect(page.locator(canvasOf)).toHaveCSS('background-color', DARK_CANVAS);
    // `system` is the absence of an override, not a third value written down.
    await expect(page.locator('html')).not.toHaveAttribute('data-theme');
  });
});

test.describe('following the system, the other way', () => {
  test.use({ colorScheme: 'light' });

  test('uses the light palette when the OS asks for light', async ({ page }) => {
    await signIn(page);
    await expect(page.locator(canvasOf)).toHaveCSS('background-color', LIGHT_CANVAS);
    await expect(page.locator('html')).not.toHaveAttribute('data-theme');
  });

  test('an explicit dark choice overrides a light system', async ({ page }) => {
    await signIn(page);
    const toggle = await themeToggle(page);
    await toggle.click(); // system -> light
    await toggle.click(); // light  -> dark

    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
    await expect(page.locator(canvasOf)).toHaveCSS('background-color', DARK_CANVAS);
  });
});

test('the toggle cycles system, light, dark and back', async ({ page }) => {
  await signIn(page);
  const toggle = await themeToggle(page);

  await expect(toggle).toHaveAttribute('aria-label', /following your system/);

  await toggle.click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await expect(toggle).toHaveAttribute('aria-label', /Theme: light/);

  await toggle.click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await expect(toggle).toHaveAttribute('aria-label', /Theme: dark/);

  await toggle.click();
  await expect(page.locator('html')).not.toHaveAttribute('data-theme');
});

test('a chosen theme survives a reload, and is applied before first paint', async ({ page }) => {
  await signIn(page);
  await (await themeToggle(page)).click(); // -> light
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');

  // A fresh navigation: the attribute must be set by the inline script in
  // index.html, before Angular has bootstrapped. Reading it immediately after
  // `domcontentloaded` — rather than after the app renders — is what makes this
  // a test of the no-flash path and not merely of persistence.
  await page.goto('/review', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
});

test('the stored value is the one index.html and theme.ts both agree on', async ({ page }) => {
  // They cannot share code — the inline script runs before the bundle exists —
  // so the key and its accepted values are the contract between them.
  await signIn(page);
  await (await themeToggle(page)).click();

  const stored = await page.evaluate(() => localStorage.getItem('clipforge.theme'));
  expect(stored).toBe('light');
});

test('choosing system clears the stored value rather than recording a third one', async ({
  page,
}) => {
  await signIn(page);
  const toggle = await themeToggle(page);
  await toggle.click();
  await toggle.click();
  await toggle.click(); // back to system

  const stored = await page.evaluate(() => localStorage.getItem('clipforge.theme'));
  expect(stored).toBeNull();
});

test.describe('the browser chrome follows a forced theme', () => {
  test.use({ colorScheme: 'dark' });

  test('a forced light theme puts a matching theme-color ahead of the media ones', async ({
    page,
  }) => {
    // Browsers take the FIRST theme-color whose media matches, so an override
    // has to be prepended. Appended, the dark media rule would still win and the
    // status bar would stay dark behind a light page.
    await signIn(page);
    await (await themeToggle(page)).click(); // -> light

    const first = await page.evaluate(
      () => document.querySelector('meta[name="theme-color"]')?.getAttribute('content'),
    );
    expect(first).toBe('#f8fafc');
  });

  test('returning to system removes the override', async ({ page }) => {
    await signIn(page);
    const toggle = await themeToggle(page);
    await toggle.click();
    await toggle.click();
    await toggle.click(); // back to system

    const overrides = await page.evaluate(() => document.querySelectorAll('#cf-theme-color').length);
    expect(overrides).toBe(0);
  });
});

test('the logo mark is present and decorative in both themes', async ({ page }) => {
  await signIn(page);
  const mark = page.locator('header img');

  await expect(mark).toBeVisible();
  // Decorative: the wordmark beside it already names the app, so announcing the
  // image too would just repeat it.
  await expect(mark).toHaveAttribute('alt', '');
  await expect(mark).toHaveJSProperty('naturalWidth', 256);
});

test('a review card is legible in whichever theme is active', async ({ page }) => {
  const uid = await signIn(page);
  await write('candidates/cand-1', candidate(uid));
  await write('clips/clip-1', clip(uid));
  await write('clips/clip-1/preview/poster', preview());

  const toggle = await themeToggle(page);
  const card = page.locator('main li').first();

  for (const expected of [LIGHT_CANVAS, DARK_CANVAS]) {
    await toggle.click();
    await expect(page.locator(canvasOf)).toHaveCSS('background-color', expected);
    // The card has to stay distinguishable from the page behind it in both.
    const [cardBg, pageBg] = await Promise.all([
      card.evaluate((el) => getComputedStyle(el).backgroundColor),
      page.locator(canvasOf).evaluate((el) => getComputedStyle(el).backgroundColor),
    ]);
    expect(cardBg).not.toBe(pageBg);
    await expect(page.getByText('Most developers never realise this')).toBeVisible();
  }
});
