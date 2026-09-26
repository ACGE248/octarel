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

/* The panel re-fetches on every poll, so a route handler that called
   route.fetch() per invocation raced with context teardown ("Response has been
   disposed"). Capture the real payload once through the request fixture, mutate
   it, and serve that fixed body instead — no live fetch inside the handler. */
async function serveMutatedUsage(page, request, baseURL, mutate) {
  const real = await (await request.get(`${baseURL}/api/usage-telemetry`)).json();
  mutate(real);
  const body = JSON.stringify(real);
  await page.route('**/api/usage-telemetry', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body }),
  );
  await page.reload();
  await navTo(page, 'view-providers');
  await expect(page.locator('.usage-row').first()).toBeVisible();
}

test('an unestablished metric never renders as a figure, even with a stray value', async ({ page, request, baseURL }) => {
  /* The class decides, not the presence of a value. A NOT_EXPOSED cell that
     somehow carried a number must still read NOT_EXPOSED -- rendering it would
     be exactly the fabrication this panel exists to prevent. */
  await serveMutatedUsage(page, request, baseURL, (body) => {
    body.rows[0].metrics.cache_hit_rate = {
      value: 93.5, class: 'NOT_EXPOSED', reason: 'not recorded', unit: 'percent',
    };
  });

  const cell = page.locator('.usage-row').first().locator('.usage-metric', { hasText: 'Cache hit rate' });
  await expect(cell).toContainText('NOT_EXPOSED');
  await expect(cell).not.toContainText('93.5');
});

test('a metric missing from the payload is shown as unknown, not omitted', async ({ page, request, baseURL }) => {
  await serveMutatedUsage(page, request, baseURL, (body) => {
    delete body.rows[0].metrics.duration_seconds;
  });

  // Dropping it silently would hide that it was never established.
  const cell = page.locator('.usage-row').first().locator('.usage-metric', { hasText: 'Duration' });
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

/* --------------------------------------------------------------------------
   OCTAREL-UI-07 (issue #42): approximate API-equivalent value, kept visibly
   and semantically apart from what was actually charged.

   The fixture seeds one run per billing class and pins its own price list
   (see serve_fixture's _fixture_pricing_book), so nothing here depends on the
   host's cached catalog and no provider is ever contacted. */

const SUBSCRIPTION_ROW = { hasText: 'fx-rb-fallback' };
const API_ROW = { hasText: 'fx-usage-api-billed' };
const FREE_ROW = { hasText: 'fx-usage-free-route' };
const UNCLASSIFIED_ROW = { hasText: 'fx-usage-unclassified' };

test('subscription usage shows an equivalent value, included billing, and zero cost', async ({ page }) => {
  /* The headline requirement from issue #42, and the one most likely to be
     got wrong: the value is worth showing, and it is not spend. */
  const row = page.locator('.usage-row', SUBSCRIPTION_ROW);
  const money = row.locator('.usage-money');

  const estimate = money.locator('.usage-money-cell.is-estimate');
  await expect(estimate).toContainText('Estimated API-equivalent value');
  // The tilde is what makes it unreadable as an exact charge.
  await expect(estimate.locator('.usage-money-value')).toContainText('~$');
  await expect(estimate).toContainText('Not a charge');

  await expect(money.locator('.billing-chip')).toHaveText('Included with subscription');

  const actual = money.locator('.usage-money-cell.is-actual');
  await expect(actual).toContainText('Actual incremental API cost');
  await expect(actual.locator('.usage-money-value')).toHaveText('$0.00');
});

test('the estimate and the actual charge are different elements, never one figure', async ({ page }) => {
  /* Issue #42 requires two fields, not one number formatted twice. If a
     redesign ever collapsed them, this fails. */
  const money = page.locator('.usage-row', API_ROW).locator('.usage-money');
  await expect(money.locator('.usage-money-cell.is-estimate')).toHaveCount(1);
  await expect(money.locator('.usage-money-cell.is-actual')).toHaveCount(1);

  // On an API row the arithmetic matches, so only the presentation separates
  // them -- exactly the case where confusion would be easiest.
  await expect(money.locator('.usage-money-cell.is-estimate .usage-money-value')).toContainText('~$0.30');
  await expect(money.locator('.usage-money-cell.is-actual .usage-money-value')).toHaveText('$0.30');
});

test('an API-billed run is labelled as API-billed, not as subscription-included', async ({ page }) => {
  const chip = page.locator('.usage-row', API_ROW).locator('.billing-chip');
  await expect(chip).toHaveText('API-billed');
  await expect(chip).toHaveClass(/is-api/);
});

test('a free route is called free, never included with a subscription', async ({ page }) => {
  /* Both cost nothing. Only one of them is covered by something the operator
     is paying for, and saying otherwise misdescribes the account. */
  const row = page.locator('.usage-row', FREE_ROW);
  await expect(row.locator('.billing-chip')).toHaveText('Free tier');
  await expect(row.locator('.billing-chip')).not.toContainText(/subscription/i);
  await expect(row.locator('.usage-money-cell.is-actual .usage-money-value')).toHaveText('$0.00');
});

test('a run whose billing cannot be established never shows zero cost', async ({ page }) => {
  /* The most dangerous default in the whole panel: "we do not know" must not
     render as "it was free". */
  const row = page.locator('.usage-row', UNCLASSIFIED_ROW);
  await expect(row.locator('.billing-chip')).toHaveText('Billing not established');

  const actual = row.locator('.usage-money-cell.is-actual');
  await expect(actual).toContainText('UNKNOWN');
  await expect(actual.locator('.usage-money-value')).not.toContainText('$');
});

test('a model with no catalog price reports the estimate as unavailable', async ({ page }) => {
  // Anthropic is not carried by the OpenCode catalog, so claude-code runs
  // cannot be priced -- and say so rather than guessing.
  const row = page.locator('.usage-row', { hasText: 'fx-rb-running' });
  const estimate = row.locator('.usage-money-cell.is-estimate');
  await expect(estimate).toContainText('UNKNOWN');
  await expect(estimate.locator('.usage-money-value')).not.toContainText('$');
  await expect(row.locator('.usage-pricing-source')).toContainText('Rates unavailable');
  // Pricing failing does not make the billing fact unknown: they are separate.
  await expect(row.locator('.billing-chip')).toHaveText('Included with subscription');
});

test('each row says where its rates came from, separately from how it was billed', async ({ page }) => {
  const row = page.locator('.usage-row', API_ROW);
  await expect(row.locator('.usage-pricing-source')).toContainText('OpenCode model catalog');
  await expect(row.locator('.usage-pricing-source')).toContainText('xai/grok-4.6');
  // A pricing snapshot makes no claim about who paid.
  await expect(row.locator('.usage-pricing-source')).not.toContainText(/subscription|API-billed/);
});

test('the aggregates keep value and spend in separate rows for every window', async ({ page }) => {
  const windows = page.locator('#usage-aggregates .usage-window');
  await expect(windows).toHaveCount(3);

  for (const label of ['Today', 'This week', 'This month']) {
    const window = page.locator('.usage-window', { hasText: label });
    await expect(window.locator('.usage-agg.is-estimate')).toHaveCount(3);
    await expect(window.locator('.usage-agg.is-actual')).toHaveCount(1);
    // Every estimated total is marked approximate; spend never is.
    await expect(window.locator('.usage-agg.is-estimate dd').first()).toContainText('~');
    await expect(window.locator('.usage-agg.is-actual dd')).not.toContainText('~');
  }
});

test('subscription value is not added into actual API spend', async ({ page }) => {
  /* The fixture has one API-billed run at $0.30 and subscription/free runs
     worth roughly another $0.28 of equivalent value. If the two totals were
     ever combined, spend would read about $0.58. */
  const today = page.locator('.usage-window', { hasText: 'Today' });
  await expect(today.locator('.usage-agg.is-actual dd')).toHaveText('$0.30');
  await expect(today.locator('.usage-agg', { hasText: 'Included value' }).locator('dd')).toContainText('~$0.27');
});

test('the panel states the separation rule it is enforcing', async ({ page }) => {
  await expect(page.locator('#usage-aggregate-invariant')).toContainText('never added to actual API spend');
  await expect(page.locator('#usage-pricing-provenance')).toContainText('OpenCode model catalog');
});

test('a failed read clears the totals rather than leaving stale money on screen', async ({ page }) => {
  await page.route('**/api/usage-telemetry', (route) => route.fulfill({ status: 503, body: '{}' }));
  await page.reload();
  await navTo(page, 'view-providers');

  await expect(page.locator('#usage-telemetry-rows')).toContainText('could not be read');
  // Stale figures beside a failure message would be worse than no figures.
  await expect(page.locator('#usage-aggregates')).toBeEmpty();
  await expect(page.locator('#usage-aggregate-invariant')).toBeEmpty();
});

test('an unestablished money cell never renders as an amount', async ({ page, request, baseURL }) => {
  /* Same rule as the metric grid, applied where it matters most: a cell
     carrying a stray number with an UNKNOWN class must not become a figure. */
  await serveMutatedUsage(page, request, baseURL, (body) => {
    body.rows[0].metrics.actual_cost_usd = {
      value: 42.5, class: 'UNKNOWN', reason: 'billing not established', unit: 'usd',
    };
  });

  const actual = page.locator('.usage-row').first().locator('.usage-money-cell.is-actual');
  await expect(actual).toContainText('UNKNOWN');
  await expect(actual).not.toContainText('42.5');
});

