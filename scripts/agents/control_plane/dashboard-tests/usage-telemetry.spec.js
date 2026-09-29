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

  // On a derived API row the arithmetic matches, so only the presentation
  // separates them -- exactly the case where confusion would be easiest.
  await expect(money.locator('.usage-money-cell.is-estimate .usage-money-value')).toContainText('~$0.30');
  await expect(money.locator('.usage-money-cell.is-actual .usage-money-value')).toHaveText('~$0.30');
  // They remain different claims: only the actual cell states how it was
  // established.
  await expect(money.locator('.usage-money-cell.is-actual .usage-money-provenance')).toHaveCount(1);
  await expect(money.locator('.usage-money-cell.is-estimate .usage-money-provenance')).toHaveCount(0);
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
  /* Spend counts the two API-billed runs only: $0.30 derived plus $0.69791188
     reported = $1.00. The subscription and free rows contribute roughly $0.28
     of equivalent *value*; if the totals were ever combined, spend would read
     about $1.28 instead. */
  const today = page.locator('.usage-window', { hasText: 'Today' });
  await expect(today.locator('.usage-agg.is-actual dd')).toHaveText('$1.00');
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

/* Independent review follow-ups (Grok Build, issue #42). */

test('money is no longer one of the interchangeable metric cells', async ({ page }) => {
  /* It moved out of the grid deliberately. If a later change put a money
     figure back among cells that all look alike, the distinction this panel
     exists to make would quietly erode. */
  const row = page.locator('.usage-row', API_ROW);
  await expect(row.locator('.usage-metric', { hasText: 'Cost' })).toHaveCount(0);
  await expect(row.locator('.usage-money')).toHaveCount(1);
});

test('a money field missing from the payload is shown as unknown, not omitted', async ({ page, request, baseURL }) => {
  await serveMutatedUsage(page, request, baseURL, (body) => {
    delete body.rows[0].metrics.actual_cost_usd;
  });

  const actual = page.locator('.usage-row').first().locator('.usage-money-cell.is-actual');
  await expect(actual).toBeVisible();
  await expect(actual).toContainText('UNKNOWN');
  await expect(actual.locator('.usage-money-value')).not.toContainText('$');
});

test('a spend total nothing could supply reads unknown, not zero', async ({ page, request, baseURL }) => {
  /* The aggregate form of "we do not know is not free". Beside a count of
     API-billed runs, a $0.00 total would read as "they cost nothing" when it
     means "none of them could be priced". */
  await serveMutatedUsage(page, request, baseURL, (body) => {
    Object.values(body.aggregates.windows).forEach((window) => {
      window.actual_api_spend_usd = {
        value: null,
        class: 'UNKNOWN',
        reason: 'none of the 2 row(s) in this window could supply this figure',
        unit: 'usd',
        coverage: { contributed: 0, rows: 2, complete: false },
      };
    });
  });

  const spend = page.locator('.usage-window', { hasText: 'Today' }).locator('.usage-agg.is-actual');
  await expect(spend).toContainText('UNKNOWN');
  await expect(spend.locator('dd')).not.toContainText('$');
});

test('a partial total states its shortfall on screen, not only on hover', async ({ page, request, baseURL }) => {
  await serveMutatedUsage(page, request, baseURL, (body) => {
    Object.values(body.aggregates.windows).forEach((window) => {
      window.actual_api_spend_usd = {
        ...window.actual_api_spend_usd,
        value: 0.3,
        class: 'DERIVED',
        coverage: { contributed: 1, rows: 4, complete: false },
      };
    });
  });

  const spend = page.locator('.usage-window', { hasText: 'Today' }).locator('.usage-agg.is-actual');
  // Visible text, so a total that is small only because most rows could not
  // be priced cannot be mistaken for a total that is small because little was
  // spent.
  await expect(spend.locator('.usage-agg-shortfall')).toHaveText('1 of 4 priced');
});

test('a complete total shows no shortfall note', async ({ page }) => {
  const today = page.locator('.usage-window', { hasText: 'Today' });
  await expect(today.locator('.usage-agg.is-actual .usage-agg-shortfall')).toHaveCount(0);
});

test('an API-billed row is not told it was never billed', async ({ page }) => {
  // The caveat on the estimate has to be true of the run it sits on.
  const estimate = page.locator('.usage-row', API_ROW).locator('.usage-money-cell.is-estimate');
  await expect(estimate).toContainText('reported separately');
  await expect(estimate).not.toContainText('Not a charge and not billed');

  const included = page.locator('.usage-row', SUBSCRIPTION_ROW).locator('.usage-money-cell.is-estimate');
  await expect(included).toContainText('Not a charge and not billed');
});

test('the unestablished billing chip stays visible against its own panel', async ({ page }) => {
  /* It used to take --surface-2 on a --surface-2 parent, so the one billing
     state an operator most needs to notice was the least visible. */
  const chip = page.locator('.usage-row', UNCLASSIFIED_ROW).locator('.billing-chip');
  await expect(chip).toBeVisible();
  const outlined = await chip.evaluate((node) => getComputedStyle(node).boxShadow);
  expect(outlined).not.toBe('none');
});

/* Round-2 review follow-ups (Grok Build, issue #42): three strings that were
   changed without any assertion holding them in place. */

test('a priced run whose billing is unknown is not told it was never billed', async ({ page, request, baseURL }) => {
  /* The fixture's unclassified row uses a model the catalog cannot price, so
     its estimate is unavailable and the note never renders. The dangerous
     combination is an unclassified row that *can* be priced: it would have
     claimed "Not a charge and not billed" about a run nobody classified. */
  await serveMutatedUsage(page, request, baseURL, (body) => {
    const row = body.rows.find((candidate) => candidate.billing.class === 'UNKNOWN');
    row.metrics.estimated_api_equivalent_usd = {
      value: 0.042, class: 'DERIVED', formula: 'x', source: null, reason: null, unit: 'usd',
    };
  });

  const estimate = page
    .locator('.usage-row', UNCLASSIFIED_ROW)
    .locator('.usage-money-cell.is-estimate');
  await expect(estimate.locator('.usage-money-value')).toContainText('~$0.04');
  await expect(estimate).toContainText('not established');
  // The claim it must never make about a run nobody could classify.
  await expect(estimate).not.toContainText('not billed');
});

test('the window breakdown counts unclassified runs instead of dropping them', async ({ page }) => {
  /* The parts used to sum to less than the stated total with nothing saying
     why. The fixture seeds one run whose billing cannot be established. */
  const count = page.locator('.usage-window', { hasText: 'Today' }).locator('.usage-window-count');
  await expect(count).toContainText('billing unknown');
  await expect(count).toContainText('7 run(s)');
});

test('runs counted in no window at all are stated, not silently dropped', async ({ page, request, baseURL }) => {
  await serveMutatedUsage(page, request, baseURL, (body) => {
    body.aggregates.excluded_undated_rows = 2;
    body.aggregates.excluded_future_rows = 1;
    body.aggregates.excluded_reason = 'no recorded end time';
  });

  const note = page.locator('#usage-aggregate-invariant');
  await expect(note).toContainText('3 run(s) are counted in no window');
  await expect(note).toContainText('no recorded end time');
  // The separation rule is still stated alongside it.
  await expect(note).toContainText('never added to actual API spend');
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


/* --------------------------------------------------------------------------
   OCTAREL-UI-08 (issue #44): a cost the worker CLI reported about itself.

   The fixture seeds an API-billed run whose CLI reported $0.69791188, and a
   subscription-backed run that also reports a figure -- the case where a
   monetary field must NOT become API spend. */

const REPORTED_ROW = { hasText: 'fx-usage-reported-cost' };
const SUB_REPORTED_ROW = { hasText: 'fx-usage-subscription-reported' };

test('a provider-reported charge is shown as reported, at its reported precision', async ({ page }) => {
  const actual = page.locator('.usage-row', REPORTED_ROW).locator('.usage-money-cell.is-actual');

  // Not rounded into a tidier number than the provider gave.
  await expect(actual.locator('.usage-money-value')).toHaveText('$0.6979');
  // ...and never marked approximate, because it is not.
  await expect(actual.locator('.usage-money-value')).not.toContainText('~');
  await expect(actual.locator('.usage-money-provenance')).toHaveText('Provider reported');
});

test('a reconstructed charge says so and stays marked approximate', async ({ page }) => {
  const actual = page.locator('.usage-row', API_ROW).locator('.usage-money-cell.is-actual');
  await expect(actual.locator('.usage-money-value')).toContainText('~$');
  await expect(actual.locator('.usage-money-provenance')).toHaveText('Derived from usage + pricing');
});

test('the two provenances are visibly different, not just differently worded', async ({ page }) => {
  const reported = page.locator('.usage-row', REPORTED_ROW).locator('.usage-money-provenance');
  const derived = page.locator('.usage-row', API_ROW).locator('.usage-money-provenance');
  await expect(reported).toHaveClass(/is-reported/);
  await expect(derived).toHaveClass(/is-derived/);
  const [a, b] = await Promise.all([
    reported.evaluate((n) => getComputedStyle(n).color),
    derived.evaluate((n) => getComputedStyle(n).color),
  ]);
  expect(a).not.toBe(b);
});

test('a subscription run reporting a cost still shows zero incremental spend', async ({ page }) => {
  /* The safety property. The Claude CLI's total_cost_usd means "what this
     would have cost on the API", not "what you were charged", so a
     subscription session must never present it as spend. */
  const row = page.locator('.usage-row', SUB_REPORTED_ROW);
  await expect(row.locator('.billing-chip')).toHaveText('Included with subscription');

  const actual = row.locator('.usage-money-cell.is-actual');
  await expect(actual.locator('.usage-money-value')).toHaveText('$0.00');
  // No reported figure leaks in, and the zero is not dressed up as an estimate.
  await expect(actual).not.toContainText('0.51');
  await expect(actual.locator('.usage-money-value')).not.toContainText('~');
});

test('a billing-class zero claims no reconstruction it did not perform', async ({ page }) => {
  // Its $0.00 comes from the route, not from usage and pricing, so it carries
  // no provenance chip -- its own note already states where it came from.
  const actual = page.locator('.usage-row', FREE_ROW).locator('.usage-money-cell.is-actual');
  await expect(actual.locator('.usage-money-provenance')).toHaveCount(0);
  await expect(actual).toContainText('free route');
});

test('a reported charge never displaces the API-equivalent value', async ({ page }) => {
  const row = page.locator('.usage-row', REPORTED_ROW);
  const estimate = row.locator('.usage-money-cell.is-estimate');
  // Still the public-rate approximation, still marked approximate, and not
  // the same figure as the reported charge.
  await expect(estimate.locator('.usage-money-value')).toContainText('~$');
  await expect(estimate.locator('.usage-money-value')).not.toContainText('0.6979');
});
