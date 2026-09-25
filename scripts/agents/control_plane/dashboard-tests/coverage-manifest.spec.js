// @ts-check
// ENG-AGENT-14 (issue #140): a data-driven coverage manifest, additive to
// control-center.spec.js's existing per-feature assertions.
//
// Rather than a hand-maintained list of every button that must be clicked
// (brittle, and a list nobody updates when a new control is added), this
// test discovers every currently-rendered *actionable* control on every
// Control Center page/view and compares its accessible name against a
// committed baseline (coverage-manifest.json, itself generated from a real
// fixture-driven DOM query, never hand-typed). A control whose accessible
// name is not already in the baseline for its view, and is not covered by
// an EXEMPTIONS entry, fails the test -- exactly the drift-detection the
// issue asks for: a newly introduced actionable control with no audit rule
// or explicit exemption breaks this test until one is added.
//
// This does not replace control-center.spec.js's existing per-control
// interaction/effect assertions; it only proves *coverage accounting* stays
// honest as the UI grows. Regenerate the baseline (after reviewing the diff
// for anything unexpected) with:
//   UPDATE_COVERAGE_MANIFEST=1 npx playwright test coverage-manifest.spec.js \
//     --config scripts/agents/control_plane/playwright.config.js

import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const manifestPath = path.join(here, 'coverage-manifest.json');

const ALL_SECTIONS = [
  'view-overview', 'view-runs', 'view-flow', 'view-tasks', 'view-agents',
  'view-providers', 'view-steering', 'view-history', 'view-worktrees',
  'view-system', 'view-terminal', 'view-settings',
];

// OCTAREL-UI-04: Manager (view-steering) is a mobile dock item; History moved
// into the "More" sheet alongside the other secondary surfaces.
const VIEWS_IN_MORE_SHEET = new Set([
  'view-runs', 'view-flow', 'view-providers', 'view-history', 'view-worktrees',
  'view-system', 'view-terminal', 'view-settings', 'view-roadmap',
]);

// Narrow, documented, safety-based exemptions only -- never a catch-all.
// Each entry names the view, the exact accessible name, and why it is
// exempt from baseline tracking.
const EXEMPTIONS = [
  {
    view: 'view-terminal',
    // xterm.js renders its own internal canvas/textarea/helper elements for
    // keyboard capture and text rendering; these are third-party terminal
    // emulator internals, not Control Center controls this audit owns, and
    // they carry no stable accessible name of our own choosing.
    match: (name, control) => typeof control.className === 'string' && control.className.includes('xterm'),
    reason: 'xterm.js internal rendering/input-capture nodes, not an OctAges control',
  },
];

/** @param {import('@playwright/test').Locator} root */
async function discoverActionableControls(root) {
  return root.evaluate((container) => {
    const selector = [
      'button', 'a[href]', 'select', 'summary',
      'input[type="checkbox"]', 'input[type="radio"]', 'input[type="text"]',
      'input[type="search"]', 'input[type="number"]', 'textarea',
      '[role="button"]', '[role="tab"]',
    ].join(', ');
    const seen = [];
    for (const el of container.querySelectorAll(selector)) {
      const style = window.getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden') continue;
      // Mirrors real accessible-name precedence closely enough for coverage
      // purposes: an explicit aria-label wins outright; otherwise an
      // element's own rendered text is its name (never its `title`, which
      // several controls here set to a volatile, path-bearing string for a
      // native OS tooltip only -- using title as the primary source made
      // e.g. every worktree action button's "name" embed that worktree's
      // full, per-run-random temp-directory path, defeating any stable
      // baseline). `title`/`placeholder`/`value` are only a fallback for a
      // control with no rendered text of its own (e.g. an icon-only button).
      const rawName = (
        el.getAttribute('aria-label')
        || el.textContent
        || el.getAttribute('placeholder')
        || el.getAttribute('title')
        || el.value
        || ''
      ).trim().replace(/\s+/g, ' ');
      // Truncate rather than encode an arbitrarily deep/volatile nested-card
      // textContent (e.g. an unlabeled container button wrapping a whole
      // detail card) into the tracked name -- coverage tracks *that* a
      // control exists and is reachable, not its full dynamic content.
      const truncated = rawName.length > 60 ? `${rawName.slice(0, 60)}…` : rawName;
      // Several cards fold live, run-dependent facts into their accessible
      // name (counts, task/runbook status words, empty-vs-populated result
      // labels). Those facts drift as the shared Playwright fixture mutates
      // and across viewports. Collapse them so the baseline tracks that the
      // control exists; a genuinely new control still produces a new name.
      const name = truncated
        .replace(/\d+/g, '#')
        .replace(
          /\b(BLOCKED|RUNNING|SUCCEEDED|FAILED|PENDING|QUEUED|DRAFT|PAUSED|READY|UNKNOWN|CANCELLED)\b/g,
          '#',
        )
        .replace(/No artifacts reported/g, '# reported item');
      seen.push({ name, tag: el.tagName.toLowerCase(), className: el.className || '' });
    }
    return seen;
  });
}

async function navTo(page, viewId) {
  const direct = page.locator(`[data-view="${viewId}"]:visible`);
  const deadline = Date.now() + 3000;
  while (Date.now() < deadline) {
    if (await direct.count()) {
      try {
        await direct.first().click({ timeout: 1000 });
        return;
      } catch {
        // retry
      }
    }
    await page.waitForTimeout(100);
  }
  if (!VIEWS_IN_MORE_SHEET.has(viewId)) throw new Error(`no visible nav control for ${viewId}`);
  await page.locator('#more-tab').click();
  await expect(page.locator('#more-sheet')).toBeVisible();
  await page.locator(`.more-item[data-view="${viewId}"]`).click();
}

test.beforeEach(async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('h1')).toHaveText('Control Center');
});

test('every actionable Control Center control is either baselined or explicitly exempt', async ({ page, request }) => {
  // This test walks every view (mobile also opens the More sheet for each), which
  // takes ~25-30s on a quiet machine. The default 30s test timeout made it fail
  // spuriously under bounded-parallel matrix load; the assertions are unchanged.
  test.setTimeout(120_000);
  // Discover against the seeded fixture, not leftover mutations from earlier
  // tests in this shared backend process (start/pause/stop runbooks).
  const reset = await request.post('/__fixture__/reset');
  expect(reset.ok()).toBeTruthy();
  await page.goto('/');
  await expect(page.locator('h1')).toHaveText('Control Center');

  /** @type {Record<string, string[]>} */
  const discovered = {};

  for (const viewId of ALL_SECTIONS) {
    await navTo(page, viewId);
    // Some views (e.g. Flow) populate their cards from a fetch issued only
    // once their tab becomes active, racing an immediate DOM query.
    await page.waitForLoadState('networkidle');
    const controls = await discoverActionableControls(page.locator(`#${viewId}`));
    const names = new Set();
    for (const control of controls) {
      const exempt = EXEMPTIONS.some((rule) => rule.view === viewId && rule.match(control.name, control));
      if (exempt) continue;
      if (control.name) names.add(control.name);
    }
    discovered[viewId] = [...names].sort();
  }

  if (process.env.UPDATE_COVERAGE_MANIFEST) {
    fs.writeFileSync(manifestPath, JSON.stringify(discovered, null, 2) + '\n', 'utf-8');
    test.skip(true, 'coverage-manifest.json regenerated; review the diff, then rerun without UPDATE_COVERAGE_MANIFEST');
    return;
  }

  if (!fs.existsSync(manifestPath)) {
    throw new Error(
      `${manifestPath} does not exist. Generate it once with UPDATE_COVERAGE_MANIFEST=1, review the diff, and commit it.`
    );
  }
  const baseline = JSON.parse(fs.readFileSync(manifestPath, 'utf-8'));

  const uncovered = [];
  for (const viewId of ALL_SECTIONS) {
    const known = new Set(baseline[viewId] || []);
    for (const name of discovered[viewId] || []) {
      if (!known.has(name)) uncovered.push(`${viewId}: ${JSON.stringify(name)}`);
    }
  }

  expect(
    uncovered,
    uncovered.length
      ? `New actionable control(s) with no coverage-manifest.json entry or EXEMPTIONS rule:\n  ${uncovered.join('\n  ')}\n` +
        'Add an audit rule (regenerate coverage-manifest.json after covering it in control-center.spec.js) or a narrow, ' +
        'documented EXEMPTIONS entry in coverage-manifest.spec.js.'
      : ''
  ).toEqual([]);
});
