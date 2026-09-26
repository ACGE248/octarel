// @ts-check
// OCTAREL-UI-06 (issue #25): model & session usage panel.
//
// The fixture seeds one runbook with exact CLI-reported token counts and one
// with none, so both the measured and the unknown paths render. What these
// tests pin is that the panel never turns an unavailable metric into a
// plausible-looking number.

import { test, expect } from '@playwright/test';
import { navTo } from './nav-helper.js';

test.beforeEach(async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('html')).toHaveAttribute('data-initial-refresh-complete', 'true', { timeout: 15000 });
  await navTo(page, 'view-providers');
  await expect(page.locator('#card-usage-telemetry')).toBeVisible();
  await expect(page.locator('.usage-row').first()).toBeVisible();
});

test('a run with real counts shows them as measured, with a derived total', async ({ page }) => {
  const row = page.locator('.usage-row', { hasText: 'fx-rb-fallback' });
  await expect(row).toBeVisible();

  const input = row.locator('.usage-metric', { hasText: 'Input' }).first();
  await expect(input).toContainText('142,800');
  await expect(input).toContainText('MEASURED');

  const total = row.locator('.usage-metric', { hasText: 'Tokens processed' });
  await expect(total).toContainText('152,110');
  await expect(total).toContainText('DERIVED');
});

test('usage is attributed to the worker that actually ran, not a default', async ({ page }) => {
  // This fixture run fell back from claude-code to codex-build.
  const row = page.locator('.usage-row', { hasText: 'fx-rb-fallback' });
  await expect(row.locator('.usage-row-identity')).toContainText('codex-build');
  await expect(row.locator('.usage-row-identity')).not.toContainText('claude-code');
});

test('cache and context metrics are labelled unavailable with a reason', async ({ page }) => {
  const row = page.locator('.usage-row', { hasText: 'fx-rb-fallback' });

  for (const label of ['Fresh input', 'Cache reads', 'Cache hit rate', 'Context used']) {
    const cell = row.locator('.usage-metric', { hasText: label }).first();
    await expect(cell).toContainText('NOT_EXPOSED');
    // A number is never shown in place of an unavailable metric.
    await expect(cell).not.toContainText(/\d/);
    await expect(cell.locator('.usage-metric-reason')).not.toBeEmpty();
  }
});

test('a subscription route shows no dollar figure', async ({ page }) => {
  /* Subscription usage is not API billing; $0.00 would imply pricing that
     does not apply to a subscription-backed CLI invocation. */
  const cost = page.locator('.usage-row').first().locator('.usage-metric', { hasText: 'Cost' });
  await expect(cost).toContainText('NOT_APPLICABLE');
  await expect(cost).not.toContainText('$');
});

test('a run with no recorded counts says unknown rather than zero', async ({ page }) => {
  const row = page.locator('.usage-row', { hasText: 'fx-rb-running' });
  const input = row.locator('.usage-metric', { hasText: 'Input' }).first();
  await expect(input).toContainText('UNKNOWN');
  await expect(input).not.toContainText('0');
});

test('the panel states why the unavailable metrics are unavailable', async ({ page }) => {
  await expect(page.locator('#usage-telemetry-note')).toContainText('NOT_EXPOSED');
});

/* Independent review follow-ups (Grok Build, issue #25). */

test('an unestablished metric never renders as a figure, even with a stray value', async ({ page }) => {
  /* The class decides, not the presence of a value. A NOT_EXPOSED cell that
     somehow carried a number must still read NOT_EXPOSED -- rendering it would
     be exactly the fabrication this panel exists to prevent. */
  await page.route('**/api/usage-telemetry', async (route) => {
    const body = await (await route.fetch()).json();
    body.rows[0].metrics.cache_hit_rate = {
      value: 93.5, class: 'NOT_EXPOSED', reason: 'not recorded', unit: 'percent',
    };
    await route.fulfill({ json: body });
  });
  await page.reload();
  await navTo(page, 'view-providers');

  const cell = page.locator('.usage-row').first().locator('.usage-metric', { hasText: 'Cache hit rate' });
  await expect(cell).toContainText('NOT_EXPOSED');
  await expect(cell).not.toContainText('93.5');
});

test('a metric missing from the payload is shown as unknown, not omitted', async ({ page }) => {
  await page.route('**/api/usage-telemetry', async (route) => {
    const body = await (await route.fetch()).json();
    delete body.rows[0].metrics.cost_usd;
    await route.fulfill({ json: body });
  });
  await page.reload();
  await navTo(page, 'view-providers');

  // Dropping it silently would hide that it was never established.
  const cell = page.locator('.usage-row').first().locator('.usage-metric', { hasText: 'Cost' });
  await expect(cell).toBeVisible();
  await expect(cell).toContainText('UNKNOWN');
});

test('the route pill does not borrow run-state styling', async ({ page }) => {
  await navTo(page, 'view-providers');
  const pill = page.locator('.usage-row').first().locator('.usage-route-pill');
  await expect(pill).toBeVisible();
  // Billable vs subscription is a routing fact, not "running"/"idle".
  await expect(pill).not.toHaveClass(/st-running|st-available/);
});

