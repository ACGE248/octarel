// @ts-check
// OCTAREL-OPS-02: the Runs view shows the persisted advancement state
// (Completed -> Advancing / Next task selected / Blocked / No eligible task).
// The /api/runbooks response is intercepted so this stays a pure presentation
// check: no orchestration runs and no provider is contacted.

import { test, expect } from '@playwright/test';

const base = {
  preset: 'overnight-development', objective: 'x', source_ref: 'TASKS.md', branch: 'b', worktree: '/w',
  parent_worker: 'claude-code', permission_profile: 'standard', max_duration_minutes: 60, status: 'SUCCEEDED',
  phases: [], acceptance_evidence: {}, remaining_seconds: null,
};
const runbooks = [
  { ...base, id: 'rb-selected', name: 'Run selected', advancement: {
      state: 'NEXT_SELECTED', next_task: { task_id: 'A-02', title: 'second task' },
      selection_reason: 'A-02 is the first eligible task', dependency_status: [{ task_id: 'A-01', status: 'satisfied' }],
      intended_worker: 'claude-code', intended_provider: 'Anthropic' } },
  { ...base, id: 'rb-blocked', name: 'Run blocked', advancement: {
      state: 'BLOCKED', reason: 'A-02 waits on unmet dependencies: A-09', next_task: { task_id: 'A-02', title: 't' } } },
  { ...base, id: 'rb-none', name: 'Run none', advancement: { state: 'NO_ELIGIBLE_TASK', reason: 'no eligible task' } },
];

test('Runs view distinguishes selected / blocked / no-eligible advancement', async ({ page }) => {
  await page.route('**/api/runbooks', (route) => route.fulfill({ json: runbooks }));
  await page.goto('/');
  await expect(page.locator('h1')).toHaveText('Control Center');
  const direct = page.locator('[data-view="view-runs"]:visible');
  if (await direct.count()) {
    await direct.first().click();
  } else {
    await page.locator('#more-tab').click();
    await page.locator('.more-item[data-view="view-runs"]').click();
  }

  const card = (id) => page.locator(`[data-runbook-id="${id}"] .runbook-advancement`);
  await expect(card('rb-selected')).toContainText('Next task selected');
  await expect(card('rb-selected')).toContainText('A-02');
  await expect(card('rb-selected')).toContainText('Why: A-02 is the first eligible task');
  await expect(card('rb-selected')).toContainText('A-01: satisfied');
  await expect(card('rb-selected')).toContainText('Anthropic');
  await expect(card('rb-blocked')).toContainText('Blocked');
  await expect(card('rb-blocked')).toContainText('reason: A-02 waits on unmet dependencies: A-09');
  await expect(card('rb-none')).toContainText('No eligible task');
  await expect(card('rb-none')).not.toContainText('Next task selected');
});
