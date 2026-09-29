// @ts-check
// OCTAREL-UI-04 (issue #23): Priority & Fallback Matrix.
//
// The routing order this screen shows has always existed in the registry; the
// screen is new. These tests pin the two properties that matter: it shows the
// *real* configured route, and it stays honest about candidates the router
// cannot currently use rather than hiding them.

import { test, expect } from '@playwright/test';
import { navTo } from './nav-helper.js';

test.beforeEach(async ({ page }) => {
  await page.goto('/');
  await navTo(page, 'view-priority');
  await expect(page.locator('#view-priority')).toBeVisible();
  await expect(page.locator('#priority-columns .priority-column').first()).toBeVisible();
});

test('renders a column per configured role with real P-ordered candidates', async ({ page }) => {
  const api = await page.evaluate(() => fetch('/api/priority-matrix').then((r) => r.json()));
  expect(api.roles.length).toBeGreaterThan(0);

  await expect(page.locator('#priority-columns .priority-column')).toHaveCount(api.roles.length);

  // Priority is the candidate's position in the configured route, so the tier
  // flags must read P1, P2, P3... in order within each column.
  for (const role of api.roles) {
    const column = page.locator('.priority-column', { hasText: role.role }).first();
    const tiers = await column.locator('.priority-tier').allTextContents();
    expect(tiers).toEqual(role.candidates.map((c) => `P${c.priority}`));
  }
});

test('the implementation route shows its real provider/agent/model chain', async ({ page }) => {
  const column = page.locator('.priority-column', { hasText: 'primary-implementation' }).first();
  await expect(column).toBeVisible();

  // A provider and an agent are distinct concepts; both are shown, with the
  // effective model. These are fixture-registry facts, not invented values.
  const first = column.locator('.priority-card').first();
  await expect(first).toContainText('P1');
  await expect(first).toContainText('Provider');
  await expect(first).toContainText('Agent');
  await expect(first).toContainText('Model');
});

test('an unusable candidate stays visible with its recorded reason', async ({ page }) => {
  const api = await page.evaluate(() => fetch('/api/priority-matrix').then((r) => r.json()));
  const excluded = api.roles
    .flatMap((role) => role.candidates.map((c) => ({ role: role.role, ...c })))
    .filter((c) => !c.routable);

  // The fixture deliberately seeds a DISABLED and a COST_BLOCKED provider.
  expect(excluded.length).toBeGreaterThan(0);

  for (const candidate of excluded) {
    const card = page
      .locator('.priority-column', { hasText: candidate.role })
      .first()
      .locator('.priority-card', { hasText: candidate.display_name })
      .first();
    await expect(card).toBeVisible();
    await expect(card).toContainText('Excluded');
    // The reason comes from provider state; it is never blank or guessed.
    await expect(card.locator('.priority-reason')).toContainText(/provider state|not the selected candidate|no provider state/);
  }
});

test('exactly one candidate per role carries traffic in single-primary mode', async ({ page }) => {
  const api = await page.evaluate(() => fetch('/api/priority-matrix?mode=A').then((r) => r.json()));
  for (const role of api.roles) {
    const withShare = role.candidates.filter((c) => c.share_percent != null);
    // A role whose candidates are all unusable correctly carries no traffic.
    expect(withShare.length).toBeLessThanOrEqual(1);
    if (role.routable_count > 0) {
      expect(withShare).toHaveLength(1);
      expect(withShare[0].share_percent).toBe(100);
    }
  }
});

test('changing routing mode re-reads the matrix from the server', async ({ page }) => {
  const select = page.locator('#priority-mode');
  await expect(select).toHaveValue('A');

  const [request] = await Promise.all([
    page.waitForRequest((r) => r.url().includes('/api/priority-matrix') && r.url().includes('mode=B')),
    select.selectOption('B'),
  ]);
  expect(request.url()).toContain('mode=B');

  // Even split gives every routable candidate in a role a share, so more than
  // one card can be active — proving the mode actually reached the server.
  await expect(page.locator('#priority-columns .priority-column').first()).toBeVisible();
});

test('no drag-to-reorder affordance is offered', async ({ page }) => {
  /* There is no backend contract for mutating route order, and the design
     specification only permits reordering "where the actual product supports
     configuration". A draggable card that silently did nothing would be a
     control that appears functional but is not. */
  await expect(page.locator('#priority-columns [draggable="true"]')).toHaveCount(0);
});
