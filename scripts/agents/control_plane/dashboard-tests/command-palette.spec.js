// @ts-check
// OCTAREL-UI-04 (issue #23): the cross-entity command palette.
//
// The important properties are that it genuinely spans entity types (rather
// than filtering the active view) and that it cannot become a way around a
// confirmation gate.

import { test, expect } from '@playwright/test';

/** Click whichever palette trigger this viewport shows. */
async function paletteTrigger(page) {
  const desktop = page.locator('#palette-open');
  return (await desktop.isVisible()) ? desktop : page.locator('#palette-open-mobile');
}

async function openPalette(page) {
  await (await paletteTrigger(page)).click();
  await expect(page.locator('#palette')).toBeVisible();
  // The index is read when the palette opens.
  await expect(page.locator('.palette-row').first()).toBeVisible();
}

test.beforeEach(async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('html')).toHaveAttribute('data-initial-refresh-complete', 'true', { timeout: 15000 });
});

test('indexes real entities across every kind, not just the active view', async ({ page }) => {
  await openPalette(page);

  /* Each kind is searched for by a term that only its own group matches, so a
     result proves that group was indexed. Views and Commands are static;
     Task/Agent/Provider/Run/Worktree/Project come from live endpoints. */
  const probes = [
    ['Worktrees', 'View'],
    ['fx-', 'Task'],
    ['Grok', 'Agent'],
    ['/stop', 'Command'],
  ];

  for (const [query, kind] of probes) {
    await page.fill('#palette-input', query);
    await expect(page.locator(`.palette-row:has(.palette-kind:text-is("${kind}"))`).first()).toBeVisible();
  }

  // Entities come from more than one source: the unfiltered index spans kinds.
  await page.fill('#palette-input', '');
  const kinds = new Set(await page.locator('.palette-kind').allTextContents());
  expect(kinds.size).toBeGreaterThan(2);
});

test('navigates to a view chosen from the palette', async ({ page }) => {
  await openPalette(page);
  await page.fill('#palette-input', 'Terminal');
  await page.locator('.palette-row').first().click();

  await expect(page.locator('#palette')).toBeHidden();
  await expect(page.locator('#view-terminal')).toBeVisible();
});

test('keyboard drives the palette end to end', async ({ page }) => {
  // The accelerator opens the palette rather than focusing the view filter.
  await page.keyboard.press('ControlOrMeta+k');
  await expect(page.locator('#palette')).toBeVisible();
  await expect(page.locator('.palette-row').first()).toBeVisible();

  await page.fill('#palette-input', 'Roadmap');
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowUp');
  await expect(page.locator('.palette-row.is-active')).toHaveCount(1);

  await page.keyboard.press('Enter');
  await expect(page.locator('#palette')).toBeHidden();
  await expect(page.locator('#view-roadmap')).toBeVisible();
});

test('escape closes the palette and restores focus', async ({ page }) => {
  const trigger = await paletteTrigger(page);
  await trigger.click();
  await expect(page.locator('#palette')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.locator('#palette')).toBeHidden();
  await expect(trigger).toBeFocused();
});

test('a destructive command is handed to the Manager, never executed', async ({ page }) => {
  /* This is the palette's central safety property. Choosing /stop must not
     issue a command: it must land in the Manager input so execution still goes
     through the existing parse/confirm path and the server's own
     destructiveness check. */
  const commandRequests = [];
  page.on('request', (request) => {
    const url = request.url();
    if (url.includes('/api/commands/') || url.includes('/api/steering/execute')) {
      commandRequests.push(`${request.method()} ${url}`);
    }
  });

  await openPalette(page);
  await page.fill('#palette-input', '/stop TASK_ID');
  const row = page.locator('.palette-row').first();
  // The confirmation requirement is stated up front.
  await expect(row).toContainText('requires confirmation');
  await row.click();

  await expect(page.locator('#view-steering')).toBeVisible();
  await expect(page.locator('#steering-input')).toHaveValue('/stop TASK_ID');

  // Nothing was executed on the way there.
  expect(commandRequests).toEqual([]);
});

test('the view filter and the palette stay separate tools', async ({ page, viewport }) => {
  // The view-filter field lives in the desktop topbar; mobile reaches the
  // palette from the header button instead and has no inline filter.
  test.skip(!viewport || viewport.width < 768, 'desktop topbar only');
  // The topbar field filters the current view; it does not open the palette.
  await page.fill('#global-search', 'zzz-no-such-entity');
  await expect(page.locator('#palette')).toBeHidden();
  await expect(page.locator('#global-search')).toHaveAttribute('placeholder', /filter this view/i);
});
