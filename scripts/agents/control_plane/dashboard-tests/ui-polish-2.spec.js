// @ts-check
// OCTAREL-UI-03: polish phase 2. Scannable task/run cards, direct "View Active Run",
// stacked mobile table, truthful log console, measured mobile chrome insets, wrapping
// workflow layout, and the Terminal / Settings / Worktrees / System hierarchy.

import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

const TASK_REF = 'ENG-AGENT-02';
const WORKER = 'grok-build';

async function navTo(page, viewId) {
  const direct = page.locator(`[data-view="${viewId}"]:visible`);
  const deadline = Date.now() + 3000;
  while (Date.now() < deadline) {
    if (await direct.count()) {
      try { await direct.first().click({ timeout: 1000 }); return; } catch { /* re-resolve */ }
    }
    await page.waitForTimeout(100);
  }
  await page.locator('#more-tab').click();
  await expect(page.locator('#more-sheet')).toBeVisible();
  await page.locator(`.more-item[data-view="${viewId}"]`).click();
}
const width = (page) => page.viewportSize()?.width || 0;
const overflows = (page) => page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);

test.beforeEach(async ({ page, request }) => {
  expect((await request.post('/__fixture__/reset')).ok()).toBeTruthy();
  await page.goto('/');
  await expect(page.locator('h1')).toHaveText('Control Center');
  await expect(page.locator('html')).toHaveAttribute('data-initial-refresh-complete', 'true', { timeout: 15000 });
});

// 1 -------------------------------------------------------------------- task + run cards

test('task cards lead with ID, title, state, stage, agent and elapsed; scheduling detail is disclosed', async ({ page }) => {
  await navTo(page, 'view-tasks');
  const card = page.locator('#tasks-cards .task-card').first();
  await expect(card).toBeVisible({ timeout: 15000 });
  await expect(card.locator('h3')).not.toBeEmpty();
  await expect(card.locator('.entity-id')).not.toBeEmpty();
  await expect(card.locator('.status-pill')).not.toBeEmpty();
  for (const label of ['Stage', 'Agent']) await expect(card.locator('.entity-fact-k', { hasText: label })).toBeVisible();
  await expect(card.locator('.entity-more')).not.toHaveAttribute('open', '');
  await expect(card.locator('.entity-more .entity-meta').first()).toBeHidden();
  await card.locator('.entity-more summary').click();
  await expect(card.locator('.entity-more')).toContainText(/Wave/);
  // The open disclosure survives the 2s poll.
  await page.waitForTimeout(4500);
  await expect(card.locator('.entity-more')).toHaveAttribute('open', '');
});

test('blocked tasks state their reason on the card without opening anything', async ({ page }) => {
  // The Tasks view lists ACTIVE-projection tasks; present one that is waiting on a dependency.
  await page.route('**/api/tasks', async (route) => {
    const tasks = await (await route.fetch()).json();
    tasks[0] = { ...tasks[0], state: 'BLOCKED', projection: 'ACTIVE', dependencies: ['fx-upstream-1'], last_error: null, admission_reason: null, failure_reason_sanitized: null };
    await route.fulfill({ json: tasks });
  });
  await navTo(page, 'view-tasks');
  const blocked = page.locator('#tasks-cards .task-card[data-task-state="BLOCKED"]').first();
  await expect(blocked).toBeVisible({ timeout: 15000 });
  await expect(blocked.locator('.entity-reason')).toContainText('Waiting on fx-upstream-1');
  await expect(blocked.locator('.entity-reason-glyph')).toHaveAttribute('aria-hidden', 'true');
});

test('run cards show stage, agent and elapsed first; objective and budget are under Run details', async ({ page }) => {
  await navTo(page, 'view-runs');
  const card = page.locator('[data-runbook-id="fx-rb-running"]');
  await expect(card).toBeVisible({ timeout: 15000 });
  await expect(card.locator('.entity-fact-k', { hasText: 'Stage' })).toBeVisible();
  await expect(card.locator('.entity-fact-k', { hasText: 'Agent' })).toBeVisible();
  await expect(card.locator('.entity-more')).not.toHaveAttribute('open', '');
  await expect(card.locator('.entity-more')).toContainText(/budget/);
  await expect(card.getByRole('button', { name: 'Pause' })).toBeVisible();
});

// 2 ------------------------------------------------------------------ View Active Run

test('View Active Run lands on the run: selected, focused and its details open', async ({ page }) => {
  const btn = page.locator('#overview-continue-btn');
  await expect(btn).toHaveText('View Active Run', { timeout: 15000 });
  await btn.click();
  await expect(page.locator('#view-runs')).toBeVisible();
  const card = page.locator('.run-card.is-focused');
  await expect(card).toHaveCount(1);
  await expect(card).toHaveAttribute('aria-current', 'true');
  await expect(card.locator('.entity-more')).toHaveAttribute('open', '');
  await expect(card).toBeInViewport();
  await expect.poll(() => page.evaluate(() => document.activeElement && document.activeElement.classList.contains('run-card'))).toBe(true);
  await expect(page.locator('#runs-focus-status')).toContainText('Showing run');
  // Leaving Runs clears the selection.
  await navTo(page, 'view-tasks');
  await navTo(page, 'view-runs');
  await expect(page.locator('.run-card.is-focused')).toHaveCount(0);
});

// 3 ------------------------------------------------------------------ responsive table

test('telemetry table stacks into labelled rows on narrow screens and stays a table on desktop', async ({ page }) => {
  await navTo(page, 'view-providers');
  const row = page.locator('#telemetry-table tbody tr').first();
  await expect(row).toBeVisible({ timeout: 15000 });
  const cell = row.locator('td').first();
  await expect(cell).toHaveAttribute('data-label', 'Provider');
  const display = await cell.evaluate((n) => getComputedStyle(n).display);
  if (width(page) <= 767) {
    expect(display).toBe('flex');
    const before = await cell.evaluate((n) => getComputedStyle(n, '::before').content);
    expect(before).toContain('Provider');
    // Every value is still present: all three labelled fields per row.
    expect(await row.locator('td[data-label]').count()).toBe(3);
  } else {
    expect(display).toBe('table-cell');
  }
  expect(await overflows(page)).toBe(false);
});

// 4 ----------------------------------------------------------------------- log console

test('log console: honest about combined output, exact raw text, flag filter and plain mode', async ({ page, request, baseURL }) => {
  const root = (await (await request.get(`${baseURL}/test/repo-root`)).json()).root;
  const runDir = path.join(root, '.agent-output', TASK_REF, WORKER, 'run-truth-1');
  fs.mkdirSync(path.join(runDir, 'logs'), { recursive: true });
  const raw = ['STATUS: FAIL', 'collected 3 items', 'WARNING: slow fixture', 'Traceback (most recent call last):', 'AssertionError: boom', 'plain output   with  spaces'].join('\n') + '\n';
  fs.writeFileSync(path.join(runDir, 'logs', 'run.log'), raw);
  fs.writeFileSync(path.join(runDir, 'manifest.json'), JSON.stringify({
    manifest_version: 1, task: TASK_REF, role: 'focused-tests', worker: WORKER, result: 'FAIL', exit_status: 1,
    started_at: '2026-09-18T10:00:00+00:00', finished_at: '2026-09-18T10:04:00+00:00',
    planned: { execution_system: 'cli', provider: 'grok', model: 'grok-4.6' }, actual: { execution_system: 'cli', provider: 'grok', model: 'grok-4.6' },
    files_changed: [], tests_or_checks: [], notes: [], paths: { log: path.relative(root, path.join(runDir, 'logs', 'run.log')) },
  }));
  try {
    const stage = page.locator('.workflow-stage-card[data-stage-id="fx-running-1"]');
    await expect(stage).toBeVisible({ timeout: 15000 });
    await stage.click();
    const log = page.locator('.agent-activity-log');
    await expect(log).toContainText('collected 3 items');
    await expect(page.locator('.agent-activity-note')).toContainText('stdout and stderr are recorded together');
    await expect(page.locator('.agent-activity-note')).toContainText('not stream markers');
    // Nothing claims a stream: no ERR/OUT/stdout/stderr labels anywhere on lines.
    const flags = await log.locator('[data-flag]').evaluateAll((nodes) => nodes.map((n) => n.getAttribute('data-flag')));
    expect([...flags].sort()).toEqual(['error keyword', 'error keyword', 'warning keyword', 'wrapper']);
    expect(flags.join(' ')).not.toMatch(/stderr|stdout|ERR\b/);
    // Raw content is preserved exactly (whitespace included).
    expect(await log.locator('code').evaluate((n) => n.textContent)).toBe(raw);
    // Flagged-only filter and its count.
    await page.locator('.agent-activity-toggle', { hasText: /flagged only/i }).click();
    await expect(page.locator('.agent-activity-meta')).toContainText('3 of 6 lines match');
    await expect(log).not.toContainText('collected 3 items');
    await page.locator('.agent-activity-toggle', { hasText: /flagged only/i }).click();
    // Highlights off = plain raw text with no decorations.
    await page.locator('.agent-activity-toggle', { hasText: /highlights/i }).click();
    await expect(log).toHaveClass(/plain/);
    // Existing controls remain.
    for (const name of ['Jump to bottom', 'Copy output']) await expect(page.getByRole('button', { name })).toBeVisible();
    await expect(page.locator('.agent-activity-btn', { hasText: /follow/i })).toBeVisible();
    await expect(page.locator('.agent-activity-search')).toBeVisible();
    // Facts derived from the evidence, not invented.
    await expect(page.locator('.agent-activity-meta')).toContainText('exit 1');
  } finally {
    fs.rmSync(path.join(root, '.agent-output', TASK_REF), { recursive: true, force: true });
  }
});

// 5 ---------------------------------------------------------------- mobile sticky action bar

test('mobile: the session bar and bottom nav never cover content on any view', async ({ page }) => {
  test.skip(width(page) > 767, 'fixed bars exist on phone/tablet-portrait layouts only');
  /* This walks every view and measures real layout geometry on each, so it is
     inherently slower than a single-surface test. On mobile most views are
     reached through the "More" sheet, and OCTAREL-UI-04 both added views and
     moved History into that sheet, which pushed the walk past the 30s default.
     The assertions below are unchanged; only the wall-clock budget is raised. */
  test.setTimeout(90_000);
  const views = ['overview', 'runs', 'flow', 'priority', 'tasks', 'agents', 'providers', 'steering', 'history', 'worktrees', 'system', 'terminal', 'settings', 'roadmap'];
  for (const view of views) {
    await navTo(page, `view-${view}`);
    /* Some views fetch their content when they become visible, so scrolling
       immediately would scroll a short page and then measure a tall one.
       Wait for the document height to stop changing before scrolling. */
    await expect
      .poll(async () => {
        const h = await page.evaluate(() => document.documentElement.scrollHeight);
        await page.waitForTimeout(120);
        const again = await page.evaluate(() => document.documentElement.scrollHeight);
        return h === again;
      }, { timeout: 10_000 })
      .toBe(true);
    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    await page.waitForTimeout(150);
    const m = await page.evaluate(() => {
      const bar = document.getElementById('sticky-controls');
      const nav = document.getElementById('bottom-nav');
      const main = document.querySelector('.view.active');
      const barBox = bar.getBoundingClientRect();
      const navBox = nav.getBoundingClientRect();
      const mainBox = main.getBoundingClientRect();
      return {
        barTop: barBox.top, barHeight: barBox.height, barBottom: barBox.bottom, navTop: navBox.top,
        navHeight: navBox.height, mainBottom: mainBox.bottom,
        cssBar: parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--sticky-h')),
        cssNav: parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--bottom-nav-h')),
        viewport: window.innerHeight,
      };
    });
    expect(m.mainBottom, `${view}: last content must end above the session bar`).toBeLessThanOrEqual(m.barTop + 1);
    expect(Math.abs(m.barBottom - m.navTop), `${view}: bar sits flush on the nav`).toBeLessThanOrEqual(1);
    expect(Math.abs(m.cssBar - m.barHeight), `${view}: published bar height is the measured height`).toBeLessThanOrEqual(1);
    expect(Math.abs(m.cssNav - m.navHeight)).toBeLessThanOrEqual(1);
    expect(m.navTop + m.navHeight).toBeLessThanOrEqual(m.viewport + 1);
  }
});

test('mobile: Pause/Stop stay reachable and the terminal viewport clears the bars', async ({ page }) => {
  test.skip(width(page) > 767, 'phone/tablet-portrait layouts only');
  await navTo(page, 'view-terminal');
  const term = page.locator('#terminal-frame');
  await expect(term).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  const box = await term.boundingBox();
  const bar = await page.locator('#sticky-controls').boundingBox();
  expect(box.y + box.height).toBeLessThanOrEqual(bar.y + 1);
  for (const id of ['#ctl-pause', '#ctl-stop']) {
    const b = await page.locator(id).boundingBox();
    expect(b.height).toBeGreaterThanOrEqual(40);
    expect(b.y + b.height).toBeLessThanOrEqual(await page.evaluate(() => window.innerHeight));
  }
});

// 6 ---------------------------------------------------------------------- workflow reflow

test('workflow reflows before it overflows and keeps stage text readable', async ({ page }) => {
  const pipeline = page.locator('#overview-pipeline');
  await expect(pipeline.locator('.workflow-stage-card').first()).toBeVisible({ timeout: 15000 });
  const m = await pipeline.evaluate((n) => ({
    cls: n.className, sw: n.scrollWidth, cw: n.clientWidth,
    widths: [...n.querySelectorAll('.workflow-stage-card:not(.orchestrator)')].map((c) => c.getBoundingClientRect().width),
    fonts: [...n.querySelectorAll('.workflow-stage-title strong, .workflow-stage-title span, .workflow-stage-agent')].map((c) => parseFloat(getComputedStyle(c).fontSize)),
  }));
  expect(m.sw, 'pipeline must not scroll sideways').toBeLessThanOrEqual(m.cw + 1);
  expect(Math.min(...m.fonts), 'no tiny workflow text').toBeGreaterThanOrEqual(11.5);
  if (width(page) > 640) expect(Math.min(...m.widths), `stage cards stay readable (${m.cls})`).toBeGreaterThanOrEqual(170);
  if (width(page) === 1280 || width(page) === 768) expect(m.cls).toContain('workflow-medium');
  if (width(page) <= 640) expect(m.cls).toContain('workflow-mobile');
  expect(await overflows(page)).toBe(false);
});

// 7 --------------------------------------------------------------------------- terminal

test('terminal shows connection state in its header and an explicit disconnected state', async ({ page }) => {
  await page.routeWebSocket('**/api/terminal/ws', (ws) => ws.close());
  await page.goto('/'); // WebSocket routes apply to pages navigated after registration
  await expect(page.locator('h1')).toHaveText('Control Center');
  await navTo(page, 'view-terminal');
  await expect(page.locator('.terminal-header #terminal-state')).toHaveText('Disconnected', { timeout: 10000 });
  await expect(page.locator('#terminal-frame')).toHaveAttribute('data-connection', 'disconnected');
  await expect(page.locator('#terminal-overlay')).toBeVisible();
  await expect(page.locator('#terminal-overlay')).toContainText('Terminal disconnected');
  await expect(page.locator('#terminal-reconnect')).toHaveClass(/btn-primary/);
  await expect(page.locator('#terminal-meta')).toContainText('Branch');
  expect(await overflows(page)).toBe(false);
});

test('terminal connected state hides the overlay and shows when it connected', async ({ page }) => {
  await navTo(page, 'view-terminal');
  await expect(page.locator('#terminal-state')).toHaveText('Connected', { timeout: 10000 });
  await expect(page.locator('#terminal-overlay')).toBeHidden();
  await expect(page.locator('#terminal-state-detail')).toContainText('since');
});

// 8 --------------------------------------------------------------------------- settings

test('settings are grouped with headings, descriptions and save/apply feedback', async ({ page }) => {
  await navTo(page, 'view-settings');
  for (const heading of ['Appearance', 'Notifications', 'Orchestration']) {
    await expect(page.locator('.settings-section-head h3', { hasText: heading })).toBeVisible();
  }
  await expect(page.locator('.setting-row .hint').first()).toBeVisible();
  await page.locator('#theme-select').selectOption('light');
  await expect(page.locator('#theme-feedback')).toHaveText('Saved');
  await page.locator('#theme-select').selectOption('dark');
  await expect(page.locator('#notif-feedback')).not.toBeEmpty();
  // Validation is explicit, not silent.
  await page.locator('#max-writers-input').fill('0');
  await page.locator('#max-writers-save').click();
  await expect(page.locator('#max-writers-feedback')).toContainText('whole number from 1 to 8');
  await expect(page.locator('#max-writers-input')).toHaveAttribute('aria-invalid', 'true');
  await page.locator('#max-writers-input').fill('4');
  await page.locator('#max-writers-save').click();
  await expect(page.locator('#max-writers-feedback')).toContainText('Applied', { timeout: 10000 });
  expect(await overflows(page)).toBe(false);
});

// 9 -------------------------------------------------------------------------- worktrees

test('worktrees lead with branch, path, condition and owner; git detail is disclosed', async ({ page }) => {
  await navTo(page, 'view-worktrees');
  const card = page.locator('.worktree-card').filter({ hasText: 'fixture-main' }).first();
  await expect(card).toBeVisible({ timeout: 15000 });
  await expect(card.locator('h3')).toHaveText(/fixture-main/);
  await expect(card.locator('.entity-id')).not.toBeEmpty();
  await expect(card.locator('.chip-row .chip').first()).toBeVisible();
  await expect(card.locator('.entity-fact-k', { hasText: 'Owner' })).toBeVisible();
  await expect(card.locator('.entity-more')).not.toHaveAttribute('open', '');
  await expect(card.locator('.entity-more')).toContainText('Upstream');
  // The card and its status pill stay inside the viewport (no page overflow from long paths).
  const right = await card.locator('.status-pill').evaluate((n) => n.getBoundingClientRect().right);
  expect(right).toBeLessThanOrEqual(await page.evaluate(() => window.innerWidth));
  expect(await overflows(page)).toBe(false);
  // Stale/dirty checkouts are called out with words, not only a border.
  await expect(page.locator('.worktree-card.needs-attention .chip-warn').first()).toBeVisible();
});

// 10 ----------------------------------------------------------------------------- system

test('system page separates runtime, repository, environment, services and diagnostics', async ({ page }) => {
  await navTo(page, 'view-system');
  for (const heading of ['Runtime health', 'Repository state', 'Environment', 'Services & processes', 'Diagnostics']) {
    await expect(page.locator('.system-group-head h2', { hasText: heading })).toBeVisible();
  }
  const summary = page.locator('#system-summary');
  await expect(summary).toContainText(/warning|No warnings/, { timeout: 15000 });
  // Warnings are named with words and a group chip; the local gate keeps status/reason up front.
  await expect(summary.locator('li .chip-warn').first()).toBeVisible();
  const gate = page.locator('#tests-body');
  await expect(gate).toContainText('NOT READY');
  await expect(gate.locator('.kv-more')).not.toHaveAttribute('open', '');
  await expect(gate.locator('.kv-warn').first()).toContainText('NOT READY');
  await expect(page.locator('#card-app-lifecycle #app-start')).toBeVisible();
  expect(await overflows(page)).toBe(false);
});

// 11 ------------------------------------------------------------- accepted task is not startable

test('Quick Start shows why an already-accepted task cannot be started again', async ({ page }) => {
  const reason = 'V1-08 was already implemented and accepted by run RB-x (every acceptance stage passed). Octarel will not start it again.';
  await page.route('**/api/quickstart', async (route) => {
    const options = await (await route.fetch()).json();
    options[0] = { ...options[0], ready: false, unavailable_reason: reason, dependency_state: 'Blocked — already accepted; awaiting merge/reconciliation' };
    await route.fulfill({ json: options });
  });
  await page.goto('/');
  await navTo(page, 'view-runs');
  const first = page.locator('#quickstart-row .quickstart-btn').first();
  await expect(first.locator('.status-pill')).toHaveText('Needs setup', { timeout: 15000 });
  await expect(first.locator('.quickstart-reason')).toContainText('already implemented and accepted');
  await first.click();
  await expect(page.locator('#prepared-run-start')).toBeDisabled();
  await expect(page.locator('#prepared-run-feedback')).toContainText('will not start it again');
});

test('Active Work does not present a finished task as the current one', async ({ page }) => {
  await page.route('**/api/workflow', async (route) => {
    const wf = await (await route.fetch()).json();
    wf.task = { ...wf.task, state: 'SUCCEEDED' };
    wf.stages = wf.stages.map((stage) => ({ ...stage, state: 'SUCCEEDED' }));
    wf.orchestrator = { ...wf.orchestrator, state: 'SUCCEEDED' };
    await route.fulfill({ json: wf });
  });
  await page.goto('/');
  const active = page.locator('#overview-active-work');
  await expect(active).toContainText('Nothing is running', { timeout: 15000 });
  await expect(active).toContainText('Last activity:');
  await expect(active.locator('.active-work-facts')).toHaveCount(0);
  await expect(page.locator('#active-work-state')).toHaveText('Idle');
});
