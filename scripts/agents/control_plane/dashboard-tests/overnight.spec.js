// @ts-check
// ENG-AO-05: the Runs view shows the durable overnight session (state, bounds, current task,
// stop reason) and its controls. /api/overnight and the overnight commands are intercepted so
// this stays a pure presentation check: nothing is scheduled and no provider is contacted.

import { test, expect } from '@playwright/test';

const session = (over = {}) => ({
  session_id: 'ovn-1', project_id: 'fixture', state: 'ACTIVE', started_at: '2026-01-01T00:00:00+00:00',
  deadline_at: '2026-01-01T10:00:00+00:00', time_remaining_seconds: 7500, accepted_count: 1, max_tasks: 3,
  current_task: { task_id: 'A-02', title: 'second task' }, merge_authorized: false,
  current_runbook: { id: 'rb-2', status: 'RUNNING', stage: 'PENDING', worker: 'claude-code', provider: 'Anthropic' },
  last_accepted: { task_id: 'A-01', runbook_id: 'rb-1', merged: true }, stop_reason: null, stop_kind: null,
  next_advancement: 'running A-02 as rb-2', resumable: false, ...over,
});

async function openRuns(page) {
  await page.goto('/');
  await expect(page.locator('h1')).toHaveText('Control Center');
  const direct = page.locator('[data-view="view-runs"]:visible');
  if (await direct.count()) {
    await direct.first().click();
  } else {
    await page.locator('#more-tab').click();
    await page.locator('.more-item[data-view="view-runs"]').click();
  }
}

test('active overnight session shows bounds, current work and live controls', async ({ page }) => {
  await page.route('**/api/overnight', (route) => route.fulfill({ json: { project_id: 'fixture', current: session(), sessions: [] } }));
  await openRuns(page);
  const body = page.locator('#overnight-body');
  await expect(page.locator('#overnight-state')).toHaveText('ACTIVE');
  for (const text of ['fixture', '2h 05m', 'A-02 — second task', 'rb-2 · RUNNING', 'claude-code / Anthropic', 'A-01 (merged)', 'owner merges (not authorized)', 'running A-02 as rb-2']) {
    await expect(body).toContainText(text);
  }
  await expect(page.locator('#overnight-form')).toBeHidden();
  await expect(page.locator('#overnight-pause')).toBeVisible();
  await expect(page.locator('#overnight-resume')).toBeHidden();
  await expect(page.locator('#overnight-stop-after')).toBeVisible();
  await expect(page.locator('#overnight-stop')).toBeVisible();
});

test('stopped session shows its stop reason and offers resume only when resumable', async ({ page }) => {
  const stopped = session({
    state: 'STOPPED', time_remaining_seconds: 0, current_runbook: null, resumable: true,
    stop_kind: 'owner_action_required', stop_reason: 'task accepted but its PR is not merged',
  });
  await page.route('**/api/overnight', (route) => route.fulfill({ json: { project_id: 'fixture', current: stopped, sessions: [] } }));
  await openRuns(page);
  await expect(page.locator('#overnight-state')).toHaveText('STOPPED');
  await expect(page.locator('#overnight-body')).toContainText('owner_action_required: task accepted but its PR is not merged');
  await expect(page.locator('#overnight-resume')).toBeVisible();
  await expect(page.locator('#overnight-pause')).toBeHidden();
  await expect(page.locator('#overnight-stop')).toBeHidden();
});

test('with no session the operator can start one; controls stay hidden', async ({ page }) => {
  const posted = [];
  await page.route('**/api/overnight', (route) => route.fulfill({ json: { project_id: 'fixture', current: null, sessions: [] } }));
  await page.route('**/api/commands/overnight_start', (route) => {
    posted.push(route.request().postDataJSON());
    return route.fulfill({ json: { ok: true, message: 'overnight session ovn-9 started', data: {} } });
  });
  await openRuns(page);
  await expect(page.locator('#overnight-state')).toHaveText('No session');
  await expect(page.locator('#overnight-controls')).toBeHidden();
  await page.locator('#overnight-duration').fill('6');
  await page.locator('#overnight-max-tasks').fill('2');
  await page.locator('#overnight-start').click();
  await expect(page.locator('#overnight-feedback')).toContainText('ovn-9 started');
  expect(posted).toEqual([{ duration: '6h', max_tasks: 2 }]);
});

test('stop asks for confirmation and sends confirm=true', async ({ page }) => {
  const posted = [];
  await page.route('**/api/overnight', (route) => route.fulfill({ json: { project_id: 'fixture', current: session(), sessions: [] } }));
  await page.route('**/api/commands/overnight_stop', (route) => {
    posted.push(route.request().postDataJSON());
    return route.fulfill({ json: { ok: true, message: 'stopped', data: {} } });
  });
  await openRuns(page);
  await page.locator('#overnight-stop').click();
  await page.locator('#confirm-ok').click();
  await expect(page.locator('#overnight-feedback')).toContainText('stopped');
  expect(posted).toEqual([{ confirm: true }]);
});
