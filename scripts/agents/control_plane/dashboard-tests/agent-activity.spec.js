// @ts-check
// OCTAREL-UI-01: Agent Activity viewer (clickable live output / details /
// evidence for a Live Workflow worker card).
//
// This viewer is read-only and reuses the existing .agent-output evidence
// tree (see scripts/agents/control_plane/agent_activity.py) -- it never
// launches a worker or makes a provider/network call. These tests write a
// real .agent-output/<task_ref>/<worker>/<run_id>/ fixture tree next to the
// fixture server's own repo_root (exposed only for tests via the
// /test/repo-root route in serve_fixture.py) so the viewer has real evidence
// to open, the same way control-center.spec.js's realWorktreePath() reads
// real filesystem state shared between the fixture server and this test
// process.
//
// Run against the two viewports OCTAREL-UI-01 explicitly requires:
//   npx playwright test --config scripts/agents/control_plane/playwright.config.js \
//     --project=desktop-1440 --project=iphone-16-pro-393 \
//     scripts/agents/control_plane/dashboard-tests/agent-activity.spec.js

import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

const TASK_REF = 'ENG-AGENT-02'; // fx-running-1/fx-running-2/fx-blocked-1/fx-done-1 all share this ref.
const RUNNING_WORKER = 'grok-build'; // fx-running-1's worker.
const RUNNING_STAGE_ID = 'fx-running-1';
const DONE_WORKER = 'opencode2-gemini-flash-lite'; // fx-done-1's worker (also fx-running-2's).
const DONE_STAGE_ID = 'fx-done-1';

async function repoRoot(request, baseURL) {
  const body = await (await request.get(`${baseURL}/test/repo-root`)).json();
  return body.root;
}

function writeAttempt(root, { worker, runId, result, exitStatus, finishedAt, logText, notes, startedAt = '2026-09-18T10:00:00+00:00' }) {
  const runDir = path.join(root, '.agent-output', TASK_REF, worker, runId);
  fs.mkdirSync(runDir, { recursive: true });
  const manifest = {
    manifest_version: 1,
    task: TASK_REF,
    role: 'focused-tests',
    worker,
    planned: {
      execution_system: 'cli', provider: 'grok', model: 'grok-build', intensity: 'standard',
      why_this_worker: 'fixture',
    },
    requested_command: ['grok-build', 'run'],
    actual: { execution_system: 'cli', provider: 'grok', model: 'grok-build', intensity: 'standard' },
    result: result ?? null,
    exit_status: exitStatus ?? null,
    duration_seconds: 5,
    started_at: startedAt,
    finished_at: finishedAt === undefined ? '2026-09-18T10:00:05+00:00' : finishedAt,
    files_changed: [],
    tests_or_checks: [],
    notes: notes || [],
    candidate_tree_sha: 'deadbeef',
    policy_manifest: {},
    paths: {},
    redaction_applied: true,
    ci_invocation_allowed: false,
  };
  if (logText !== null && logText !== undefined) {
    const logsDir = path.join(runDir, 'logs');
    fs.mkdirSync(logsDir, { recursive: true });
    const logPath = path.join(logsDir, 'run.log');
    fs.writeFileSync(logPath, logText, 'utf-8');
    manifest.paths.log = path.relative(root, logPath);
  }
  const manifestPath = path.join(runDir, 'manifest.json');
  const summaryPath = path.join(runDir, 'summary.md');
  manifest.paths.manifest = path.relative(root, manifestPath);
  manifest.paths.summary = path.relative(root, summaryPath);
  fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2) + '\n', 'utf-8');
  fs.writeFileSync(summaryPath, `# Delegation summary — ${TASK_REF} / ${worker}\n\nresult: ${result}\n`, 'utf-8');
}

async function openWorkerCard(page, stageId) {
  await page.goto('/');
  await expect(page.locator('h1')).toHaveText('Control Center');
  // Live Workflow lives on Overview; the worker card is also reachable via
  // the dedicated Flow view. Overview is simplest and matches how a person
  // actually finds it. data-stage-id is the underlying task id (see
  // dashboard_api.py's workflow(): stages[].id == item.id) -- more reliable
  // than matching on a worker's rendered display name/meta text.
  const card = page.locator(`.workflow-stage-card[data-stage-id="${stageId}"]`);
  await expect(card).toBeVisible({ timeout: 5000 });
  await card.click();
  await expect(page.locator('#workflow-detail-sheet')).toBeVisible();
}

test.describe('OCTAREL-UI-01 Agent Activity viewer', () => {
  // OCTAREL-TEST-01: every test writes its own real evidence tree, so start and end
  // each test with none. Without this a later test (and the History view seen by
  // coverage-manifest.spec.js) observes attempts left behind by earlier tests in the
  // same fixture, making results depend on test order.
  async function clearEvidence(request, baseURL) {
    fs.rmSync(path.join(await repoRoot(request, baseURL), '.agent-output', TASK_REF), { recursive: true, force: true });
  }
  test.beforeEach(async ({ request, baseURL }) => clearEvidence(request, baseURL));
  test.afterEach(async ({ request, baseURL }) => clearEvidence(request, baseURL));

  test('opening a running worker card shows live output and closes cleanly', async ({ page, request, baseURL }) => {
    const root = await repoRoot(request, baseURL);
    writeAttempt(root, {
      worker: RUNNING_WORKER, runId: 'run-running-1', result: null, exitStatus: null,
      finishedAt: null, logText: 'starting up\ncompiling...\n',
    });

    await openWorkerCard(page, RUNNING_STAGE_ID);
    await expect(page.locator('.agent-activity-facts')).toBeVisible();
    await expect(page.locator('.agent-activity-tab', { hasText: 'Live Output' })).toHaveClass(/active/);
    await expect(page.locator('.agent-activity-log')).toContainText('compiling');

    // Escape closes it (keyboard accessibility requirement).
    await page.keyboard.press('Escape');
    await expect(page.locator('#workflow-detail-sheet')).toBeHidden();
  });

  test('completed output remains inspectable after the run finishes', async ({ page, request, baseURL }) => {
    const root = await repoRoot(request, baseURL);
    writeAttempt(root, {
      worker: DONE_WORKER, runId: 'run-done-1', result: 'PASS', exitStatus: 0,
      logText: 'all checks passed\n',
    });

    await openWorkerCard(page, DONE_STAGE_ID);
    await page.locator('.agent-activity-tab', { hasText: 'Details' }).click();
    // renderPanel() (app.js) replaces panels.innerHTML with only the active
    // tab's single .agent-activity-panel -- there is no [hidden] sibling or
    // per-tab data attribute to select on.
    await expect(page.locator('.agent-activity-panel')).toContainText('PASS');
    await page.locator('.agent-activity-tab', { hasText: 'Live Output' }).click();
    await expect(page.locator('.agent-activity-log')).toContainText('all checks passed');
    await page.locator('#workflow-detail-close').click();
    await expect(page.locator('#workflow-detail-sheet')).toBeHidden();
  });

  test('failed output is shown, not hidden or replaced with a generic error', async ({ page, request, baseURL }) => {
    const root = await repoRoot(request, baseURL);
    writeAttempt(root, {
      worker: DONE_WORKER, runId: 'run-failed-1', result: 'FAIL', exitStatus: 1,
      logText: 'Traceback (most recent call last):\nAssertionError: expected true\n',
    });

    await openWorkerCard(page, DONE_STAGE_ID);
    await expect(page.locator('.agent-activity-log')).toContainText('AssertionError');
  });

  test('a run with no surviving log shows an explicit missing state, never blank', async ({ page, request, baseURL }) => {
    const root = await repoRoot(request, baseURL);
    writeAttempt(root, { worker: DONE_WORKER, runId: 'run-pruned-1', result: 'PASS', logText: null });

    await openWorkerCard(page, DONE_STAGE_ID);
    await expect(page.locator('.agent-activity-log, .agent-activity-empty')).toContainText(/missing|no output/i);
  });

  test('multiple attempts are distinguishable and switching preserves the correct output', async ({ page, request, baseURL }) => {
    const root = await repoRoot(request, baseURL);
    writeAttempt(root, { worker: DONE_WORKER, runId: 'run-multi-a', result: 'FAIL', logText: 'first attempt failed\n', startedAt: '2026-09-18T10:00:00+00:00' });
    writeAttempt(root, { worker: DONE_WORKER, runId: 'run-multi-b', result: 'PASS', logText: 'second attempt passed\n', startedAt: '2026-09-18T10:05:00+00:00' });

    await openWorkerCard(page, DONE_STAGE_ID);
    const pills = page.locator('.agent-activity-attempt-pill');
    await expect(pills).toHaveCount(2, { timeout: 5000 });
    await expect(page.locator('.agent-activity-log')).toContainText('second attempt passed');
    await pills.last().click();
    await expect(page.locator('.agent-activity-log')).toContainText('first attempt failed');
  });

  test('long output is bounded and never freezes the dialog', async ({ page, request, baseURL }) => {
    const root = await repoRoot(request, baseURL);
    writeAttempt(root, {
      worker: DONE_WORKER, runId: 'run-long-1', result: 'PASS',
      logText: 'line\n'.repeat(20000),
    });

    await openWorkerCard(page, DONE_STAGE_ID);
    await expect(page.locator('.agent-activity-log')).toBeVisible({ timeout: 5000 });
    const length = await page.locator('.agent-activity-log').evaluate((n) => n.textContent?.length || 0);
    expect(length).toBeLessThan(300_000); // MAX_OUTPUT_BYTES server-side cap plus formatting slack.
  });

  test('secrets in output are never rendered even if an old manifest predates redaction', async ({ page, request, baseURL }) => {
    const root = await repoRoot(request, baseURL);
    writeAttempt(root, {
      worker: DONE_WORKER, runId: 'run-secret-1', result: 'PASS',
      logText: 'Authorization: Bearer sk-ABCDEFGHIJKLMNOPQRSTUVWX\n',
    });

    await openWorkerCard(page, DONE_STAGE_ID);
    const text = await page.locator('.agent-activity-log').innerText();
    expect(text).not.toContain('sk-ABCDEFGHIJKLMNOPQRSTUVWX');
  });

  test('search, wrap, and copy controls operate on the currently displayed output', async ({ page, request, baseURL }) => {
    const root = await repoRoot(request, baseURL);
    writeAttempt(root, { worker: DONE_WORKER, runId: 'run-controls-1', result: 'PASS', logText: 'needle\nhaystack\nhaystack\n' });

    await openWorkerCard(page, DONE_STAGE_ID);
    await page.locator('.agent-activity-search').fill('needle');
    // The Live panel's search filters to matching lines in place (app.js
    // rewrites the <code> textContent) rather than wrapping matches in
    // <mark>, so assert on the filtered content instead of a highlight node.
    await expect(page.locator('.agent-activity-log')).toContainText('needle');
    await expect(page.locator('.agent-activity-log')).not.toContainText('haystack');
    await page.locator('.agent-activity-search').fill('');
    await page.locator('.agent-activity-toggle', { hasText: /wrap/i }).click();
    await expect(page.locator('.agent-activity-log.wrap')).toBeVisible();
  });

  test('no horizontal overflow while the viewer is open', async ({ page, request, baseURL }) => {
    const root = await repoRoot(request, baseURL);
    writeAttempt(root, { worker: DONE_WORKER, runId: 'run-overflow-1', result: 'PASS', logText: 'x'.repeat(500) });

    await openWorkerCard(page, DONE_STAGE_ID);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
    expect(overflow).toBe(false);
  });
});
