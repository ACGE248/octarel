// @ts-check
// OCTAREL-UI-04 (issue #23): the Glass Orchestration Studio application shell.
//
// Covers the parts of the redesign that are behaviour rather than decoration:
// the collapsible navigation rail, the navigation information architecture
// (Manager label, Roadmap promoted to a real destination), and the structural
// light/dark parity the design specification requires. Visual treatment is not
// asserted here — only things that can break for a user.

import { test, expect } from '@playwright/test';
import { navTo, ALL_VIEWS } from './nav-helper.js';

test.beforeEach(async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#view-overview')).toBeVisible();
});

test.describe('navigation rail', () => {
  test.skip(({ viewport }) => !viewport || viewport.width < 768, 'rail collapse is a desktop affordance');

  test('collapses to an icon rail and restores, keeping every accessible name', async ({ page }) => {
    const sidebar = page.locator('#sidebar');
    const toggle = page.locator('#rail-toggle');
    const expandedWidth = (await sidebar.boundingBox()).width;

    await expect(toggle).toHaveAttribute('aria-pressed', 'false');
    await toggle.click();

    await expect(page.locator('html')).toHaveAttribute('data-rail', 'collapsed');
    await expect(toggle).toHaveAttribute('aria-pressed', 'true');
    // The rail animates its width, so poll rather than measuring the first
    // frame after the click.
    await expect
      .poll(async () => (await sidebar.boundingBox()).width)
      .toBeLessThan(expandedWidth);

    // The labels are visually collapsed but must remain in the accessibility
    // tree, otherwise the rail stops being navigable by name.
    for (const view of ALL_VIEWS) {
      const tab = page.locator(`#tabbar .tab[data-view="${view}"]`);
      await expect(tab).toBeVisible();
      expect((await tab.textContent()).trim()).not.toBe('');
    }

    await toggle.click();
    await expect(page.locator('html')).not.toHaveAttribute('data-rail', 'collapsed');
    await expect(toggle).toHaveAttribute('aria-pressed', 'false');
  });

  test('a collapsed rail still navigates and survives a reload', async ({ page }) => {
    await page.locator('#rail-toggle').click();
    await expect(page.locator('html')).toHaveAttribute('data-rail', 'collapsed');

    await page.locator('#tabbar .tab[data-view="view-terminal"]').click();
    await expect(page.locator('#view-terminal')).toBeVisible();

    // The preference is stored in this browser only, like the theme.
    await page.reload();
    await expect(page.locator('html')).toHaveAttribute('data-rail', 'collapsed');
  });
});

test('Manager is the user-facing label for the steering surface', async ({ page }) => {
  // Whichever navigation this viewport shows — the desktop rail or the mobile
  // dock — the surface is labelled "Manager", never "Steering".
  const control = page.locator('[data-view="view-steering"]:visible').first();
  await expect(control).toContainText('Manager');
  await expect(page.locator('nav:visible')).not.toContainText('Steering');

  // The route/binding is deliberately unchanged underneath the new label.
  await navTo(page, 'view-steering');
  await expect(page.locator('#view-steering')).toBeVisible();
  await expect(page.locator('#steering-input')).toBeVisible();
});

test('Roadmap is a real destination that owns the live table', async ({ page }) => {
  await navTo(page, 'view-roadmap');
  await expect(page.locator('#view-roadmap')).toBeVisible();
  await expect(page.locator('#roadmap-cards')).toBeVisible();
  await expect(page.locator('#roadmap-summary')).toBeVisible();

  // It is no longer duplicated inside Settings: exactly one live table exists.
  await expect(page.locator('#roadmap-cards')).toHaveCount(1);
  await navTo(page, 'view-settings');
  await expect(page.locator('#view-settings')).toBeVisible();
  await expect(page.locator('#view-settings #roadmap-cards')).toHaveCount(0);
});

test('light and dark expose exactly the same structure on every view', async ({ page }) => {
  /* The specification requires strict 1:1 structural parity — a theme change
     may alter tokens, never information architecture. Compare the rendered
     control/heading inventory of each view across both themes.

     The dashboard polls every 2s, so wait for the poll-driven surfaces to be
     populated before snapshotting; otherwise the first inventory captures an
     empty list and the second captures a filled one, and the comparison
     measures load timing instead of theming. */
  await expect(page.locator('#overview-attention-list li').first()).toBeVisible();
  await expect(page.locator('#active-work-body')).not.toBeEmpty();

  async function setTheme(theme) {
    await page.evaluate((t) => { document.documentElement.setAttribute('data-theme', t); }, theme);
  }

  async function snapshot(view) {
    return page.locator(`#${view}`).evaluate((root) => {
      const nodes = root.querySelectorAll('button, a, select, input, textarea, summary, h2, h3');
      return Array.from(nodes)
        .filter((el) => el.offsetParent !== null || el.getClientRects().length)
        .map((el) => `${el.tagName}:${(el.id || el.className || '').toString().trim()}`)
        .sort();
    });
  }

  for (const view of ALL_VIEWS) {
    await navTo(page, view);
    await expect(page.locator(`#${view}`)).toBeVisible();

    /* Compare the two themes back to back on one view, and allow a retry.
       The dashboard polls every 2s against state other tests mutate, so a
       single mismatch can mean "the data changed between snapshots". A real
       structural difference between themes reproduces on every attempt; a
       data refresh does not. */
    let dark = [];
    let light = [];
    for (let attempt = 0; attempt < 3; attempt += 1) {
      await setTheme('dark');
      dark = await snapshot(view);
      await setTheme('light');
      light = await snapshot(view);
      if (JSON.stringify(dark) === JSON.stringify(light)) break;
    }
    expect(light, `${view} structure differs between themes`).toEqual(dark);
  }
});
