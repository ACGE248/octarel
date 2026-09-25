// @ts-check
// Shared Control Center navigation helper.
//
// Navigation is viewport-dependent: desktop shows the full sidebar rail, while
// mobile shows the Home | Manager | Tasks | Agents dock plus a "More" sheet for
// the remaining sections. Every spec that navigates needs the same knowledge of
// which surfaces live behind "More", so it is defined once here rather than
// copied per spec — a navigation change then updates one list.

import { expect } from '@playwright/test';

/** Views reachable on mobile only through the "More" sheet. */
export const VIEWS_IN_MORE_SHEET = new Set([
  'view-runs',
  'view-flow',
  'view-providers',
  'view-history',
  'view-worktrees',
  'view-system',
  'view-terminal',
  'view-settings',
  'view-roadmap',
]);

/** Every top-level view, in navigation order. */
export const ALL_VIEWS = [
  'view-overview', 'view-runs', 'view-flow', 'view-tasks', 'view-agents',
  'view-providers', 'view-steering', 'view-history', 'view-worktrees',
  'view-system', 'view-terminal', 'view-settings', 'view-roadmap',
];

/**
 * Click through to a view at whatever viewport the test is running.
 * @param {import('@playwright/test').Page} page
 * @param {string} viewId
 */
export async function navTo(page, viewId) {
  // The sidebar/bottom-nav swap on a CSS transition (transform + visibility),
  // so a resize can leave the target control mid-transition for a moment;
  // retry briefly instead of failing on a single instantaneous count() check.
  const direct = page.locator(`[data-view="${viewId}"]:visible`);
  const deadline = Date.now() + 3000;
  while (Date.now() < deadline) {
    if (await direct.count()) {
      try {
        await direct.first().click({ timeout: 1000 });
        return;
      } catch {
        // Layout/poll-cycle churn made the click target unstable; re-resolve
        // the locator and retry rather than failing on one bad attempt.
      }
    }
    await page.waitForTimeout(100);
  }
  if (!VIEWS_IN_MORE_SHEET.has(viewId)) {
    throw new Error(`no visible nav control for ${viewId}`);
  }
  await page.locator('#more-tab').click();
  await expect(page.locator('#more-sheet')).toBeVisible();
  await page.locator(`.more-item[data-view="${viewId}"]`).click();
}
