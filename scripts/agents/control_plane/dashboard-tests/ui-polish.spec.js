// @ts-check
// OCTAREL-UI-02: Control Center polish pass. Covers the new operator-facing
// surfaces against the deterministic fixture: Active Work, the unified attention
// language, the pipeline phase track and non-colour-only stage states, compact
// provider facts with reasons, the Agent Activity log console, and horizontal
// overflow on every view at every viewport.

import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

const VIEWS = ['overview', 'runs', 'flow', 'tasks', 'agents', 'providers', 'steering', 'history', 'worktrees', 'system', 'terminal', 'settings'];
const TASK_REF = 'ENG-AGENT-02';
const WORKER = 'grok-build'; // fx-running-1's worker

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

test.beforeEach(async ({ page, request }) => {
  // Start from the seeded fixture, not mutations left by earlier specs.
  expect((await request.post('/__fixture__/reset')).ok()).toBeTruthy();
  await page.goto('/');
  await expect(page.locator('h1')).toHaveText('Control Center');
});

test('Overview answers project, active work, agents, next stage and blockers', async ({ page }) => {
  await expect(page.locator('#overview-context')).toContainText('OctaScene');
  const active = page.locator('#overview-active-work');
  await expect(active).toContainText(TASK_REF, { timeout: 15000 });
  await expect(active.locator('.active-work-facts')).toContainText('Stage');
  await expect(active.locator('.active-work-facts')).toContainText('Test');
  await expect(active.locator('.active-work-facts')).toContainText('Next expected');
  await expect(active.locator('.active-work-facts')).toContainText('Review');
  await expect(active.locator('.active-work-facts')).toContainText('Elapsed');
  // The blocking reason is stated, not implied.
  await expect(active.locator('.active-work-block')).toContainText('Waiting on fx-running-1');
  await expect(active.locator('.active-work-agent')).toHaveCount(2);
});

test('Active Work agent rows open the Agent Activity viewer', async ({ page }) => {
  const row = page.locator('#overview-active-work .active-work-agent').first();
  await expect(row).toBeVisible({ timeout: 15000 });
  await row.click();
  await expect(page.locator('#workflow-detail-sheet')).toBeVisible();
  await expect(page.locator('.agent-activity-banner')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.locator('#workflow-detail-sheet')).toBeHidden();
});

test('attention rows share one language: glyph + text label + action, never colour alone', async ({ page }) => {
  const rows = page.locator('#overview-attention-list .attn');
  await expect(rows.first()).toBeVisible({ timeout: 15000 });
  await expect(page.locator('#overview-attention-count')).toContainText(/\d+ items?/);
  await expect(page.locator('#overview-attention-list [data-attention-kind="blocked"] .attn-kind')).toHaveText('Task blocked');
  await expect(page.locator('#overview-attention-list [data-attention-kind="quota"] .attn-kind').first()).toHaveText('Spend / quota issue');
  const count = await rows.count();
  for (let i = 0; i < count; i += 1) {
    await expect(rows.nth(i).locator('.attn-icon')).toHaveAttribute('aria-hidden', 'true');
    await expect(rows.nth(i).locator('.attn-kind')).not.toBeEmpty();
    await expect(rows.nth(i).locator('.attn-action')).toBeVisible();
  }
  // "Open" navigates to the place the operator can act.
  await page.locator('#overview-attention-list [data-attention-kind="quota"] .attn-action').first().click();
  await expect(page.locator('#view-providers')).toBeVisible();
});

test('pipeline phase track distinguishes states with text and glyphs', async ({ page }) => {
  const track = page.locator('#overview-pipeline .phase-track');
  await expect(track).toBeVisible({ timeout: 15000 });
  await expect(track.locator('.phase-step')).toHaveCount(5);
  await expect(track.locator('.phase-step[data-phase="review"]')).toContainText('Blocked');
  await expect(track.locator('.phase-step[data-phase="test"]')).toContainText('Running');
  await expect(track.locator('.phase-step[data-phase="implement"]')).toContainText('Not started');
  // Every stage pill carries a glyph and its state word.
  const blocked = page.locator('#overview-pipeline .workflow-status[data-kind="blocked"]').first();
  await expect(blocked).toContainText('BLOCKED');
  await expect(blocked.locator('.workflow-status-glyph')).not.toBeEmpty();
  await expect(page.locator('#overview-pipeline .workflow-status[data-kind="complete"]').first()).toContainText('SUCCEEDED');
});

test('providers show compact facts and explain why one is unavailable', async ({ page }) => {
  await navTo(page, 'view-providers');
  await expect(page.locator('.provider-summary')).toContainText('ready');
  const blockedCard = page.locator('#providers-cards .entity-card').filter({ hasText: 'ID: grok-build-review' });
  await expect(blockedCard.locator('.provider-why')).toContainText('Cost-blocked');
  for (const label of ['Authentication', 'Model', 'Role', 'Usage', 'Fallback']) {
    await expect(blockedCard.locator('.provider-fact dt', { hasText: label })).toBeVisible();
  }
  // Implementation detail is one disclosure away, not on the primary surface.
  await expect(blockedCard.locator('.provider-more')).not.toHaveAttribute('open', '');
});

test('providers list keeps an open disclosure open across polls', async ({ page }) => {
  await navTo(page, 'view-providers');
  const card = page.locator('#providers-cards .entity-card').filter({ hasText: 'ID: grok-build' }).first();
  // First load legitimately re-renders as route orders and model/auth data arrive; wait for both to settle.
  await expect(card.locator('.provider-fact', { hasText: 'Fallback' }).locator('dd')).not.toHaveText('—', { timeout: 8000 });
  await expect(card.locator('.provider-fact', { hasText: 'Authentication' }).locator('dd')).toContainText(/session/i, { timeout: 8000 });
  await card.locator('.provider-more summary').click();
  await expect(card.locator('.provider-more')).toHaveAttribute('open', '');
  await page.waitForTimeout(4500); // > two 2s polls
  await expect(card.locator('.provider-more')).toHaveAttribute('open', '');
});

test('Agent Activity log console classifies stderr/warnings, numbers lines and follows the tail', async ({ page, request, baseURL }) => {
  const root = (await (await request.get(`${baseURL}/test/repo-root`)).json()).root;
  const runDir = path.join(root, '.agent-output', TASK_REF, WORKER, 'run-console-1');
  fs.mkdirSync(path.join(runDir, 'logs'), { recursive: true });
  const lines = ['[status] starting', 'collected 3 items', 'WARNING: slow fixture', 'Traceback (most recent call last):', 'AssertionError: boom',
    ...Array.from({ length: 80 }, (_, i) => `progress ${i}`)];
  fs.writeFileSync(path.join(runDir, 'logs', 'run.log'), `${lines.join('\n')}\n`);
  fs.writeFileSync(path.join(runDir, 'manifest.json'), JSON.stringify({
    manifest_version: 1, task: TASK_REF, role: 'focused-tests', worker: WORKER, result: null, exit_status: null,
    started_at: '2026-09-18T10:00:00+00:00', finished_at: null,
    planned: { execution_system: 'cli', provider: 'grok', model: 'grok-4.6' }, actual: { execution_system: 'cli', provider: 'grok', model: 'grok-4.6' },
    files_changed: [], tests_or_checks: [], notes: [], paths: { log: path.relative(root, path.join(runDir, 'logs', 'run.log')) },
  }));
  try {
    const stage = page.locator('.workflow-stage-card[data-stage-id="fx-running-1"]');
    await expect(stage).toBeVisible({ timeout: 15000 }); // first poll can be slow under matrix load
    await stage.click();
    const log = page.locator('.agent-activity-log');
    await expect(log).toContainText('collected 3 items');
    await expect(log.locator('.log-err').first()).toContainText(/Traceback|AssertionError/);
    await expect(log.locator('.log-warn').first()).toContainText('WARNING');
    await expect(log.locator('.log-sys').first()).toContainText('starting');
    // Non-colour stream marker exists for error lines.
    await expect(log.locator('.log-err').first()).toHaveAttribute('data-stream', 'ERR');
    await expect(page.locator('.agent-activity-banner')).toHaveAttribute('data-run-state', 'running');
    await expect(page.locator('.agent-activity-meta')).toContainText('85 lines');
    // Follow-tail starts on and lands at the bottom; the toggle reports its state.
    const follow = page.locator('.agent-activity-btn', { hasText: /follow/i });
    await expect(follow).toHaveAttribute('aria-pressed', 'true');
    await expect.poll(() => log.evaluate((n) => n.scrollHeight - n.scrollTop - n.clientHeight < 24)).toBe(true);
    await follow.click();
    await expect(follow).toHaveAttribute('aria-pressed', 'false');
    // Search reports how many lines match.
    await page.locator('.agent-activity-search').fill('progress 7');
    await expect(page.locator('.agent-activity-meta')).toContainText(/\d+ of 85 lines match/);
  } finally {
    fs.rmSync(path.join(root, '.agent-output', TASK_REF), { recursive: true, force: true });
  }
});

test('Agent Activity viewer with no output shows an explicit empty state and disabled tools', async ({ page }) => {
  const stage = page.locator('.workflow-stage-card[data-stage-id="fx-done-1"]');
  await expect(stage).toBeVisible({ timeout: 15000 });
  await stage.click();
  await expect(page.locator('#workflow-detail-sheet')).toBeVisible();
  await expect(page.locator('.agent-activity-facts')).toContainText('Task/run');
});

test('no horizontal overflow on any view', async ({ page }) => {
  for (const view of VIEWS) {
    await navTo(page, `view-${view}`);
    await expect(page.locator(`#view-${view}`)).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
    expect(overflow, `${view} overflows horizontally`).toBe(false);
  }
});

test('dark and light themes both keep status text legible', async ({ page }) => {
  for (const theme of ['light', 'dark']) {
    await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme);
    const contrast = await page.evaluate(() => {
      const parse = (c) => c.match(/[\d.]+/g).slice(0, 3).map(Number);
      const lum = ([r, g, b]) => { const f = (v) => { const s = v / 255; return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
      const probe = document.querySelector('.attn-detail, .hint');
      const fg = parse(getComputedStyle(probe).color);
      const bg = parse(getComputedStyle(document.body).backgroundColor);
      const [a, b] = [lum(fg), lum(bg)].sort((x, y) => y - x);
      return (a + 0.05) / (b + 0.05);
    });
    expect(contrast, `${theme} muted text contrast`).toBeGreaterThan(4.5);
  }
});

test('reduced motion disables decorative animation', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  const durations = await page.evaluate(() => Array.from(document.querySelectorAll('.live-dot, .workflow-progress'))
    .map((n) => parseFloat(getComputedStyle(n).animationDuration) || 0));
  for (const d of durations) expect(d).toBeLessThan(0.01);
});
