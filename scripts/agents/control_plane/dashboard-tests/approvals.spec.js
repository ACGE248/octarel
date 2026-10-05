// @ts-check
// ENG-PC-10: pending typed decisions render in Attention and the shared Run
// Detail inspector. Resolution stays server-owned; this browser test rejects
// the fixture request so no operational handler or provider can run.

import { test, expect } from '@playwright/test';
import { navTo } from './nav-helper.js';

test.beforeEach(async ({ page, request }) => {
  const reset = await request.post('/__fixture__/reset');
  expect(reset.ok()).toBeTruthy();
  await page.goto('/');
  await expect(page.locator('html')).toHaveAttribute('data-initial-refresh-complete', 'true', { timeout: 15_000 });
});

test('Attention previews impact and Run Detail retains the typed resolution', async ({ page }) => {
  const approval = page.locator('#overview-attention-list [data-attention-kind="approval"]');
  await expect(approval).toBeVisible();
  await expect(approval).toContainText('AMBIGUOUS_PRODUCT_ARCHITECTURE_CHOICE');
  await expect(approval).toContainText('Extend the shipped Run Detail inspector');
  await expect(approval).toContainText('deterministic review and gate requirements remain unchanged');

  await approval.getByLabel('Approval resolution note').fill('Fixture choice declined after impact review');
  await Promise.all([
    page.waitForResponse((response) => response.url().includes('/api/approvals/') && response.url().endsWith('/resolve')),
    approval.getByRole('button', { name: 'Reject' }).click(),
  ]);
  await expect(approval).toHaveCount(0);

  await navTo(page, 'view-runs');
  await page.getByRole('button', { name: 'View detail for Fixture overnight run' }).click();
  const detail = page.locator('#run-detail');
  await expect(detail).toContainText('Approvals & decisions');
  await expect(detail.locator('[data-approval-state="REJECTED"]')).toContainText('Fixture choice declined after impact review');
  await expect(detail).toContainText('fixture-operator');
});

test('Attention popover keeps pending approval controls when advancement adds a derived row', async ({ page }) => {
  await page.route('**/api/runbooks', async (route) => {
    const response = await route.fetch();
    const rows = await response.json();
    rows[0].advancement = {
      state: 'OWNER_DECISION_REQUIRED',
      reason: 'Synthetic advancement row must remain Overview-only',
    };
    await route.fulfill({ response, json: rows });
  });
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-initial-refresh-complete', 'true', { timeout: 15_000 });

  await page.locator('#notif-bell').click();
  const popoverApproval = page.locator('#attention-list [data-attention-kind="approval"]');
  await expect(popoverApproval).toBeVisible();
  await expect(popoverApproval.getByLabel('Approval resolution note')).toBeVisible();
  await expect(popoverApproval.getByRole('button', { name: 'Approve' })).toBeVisible();
  await expect(page.locator('#attention-list')).not.toContainText('Synthetic advancement row');
  await expect(page.locator('#overview-attention-list')).toContainText('Synthetic advancement row');
});

test('Run Detail preserves the approval note and focus across polling before resolution', async ({ page }) => {
  await navTo(page, 'view-runs');
  await page.getByRole('button', { name: 'View detail for Fixture overnight run' }).click();
  const detail = page.locator('#run-detail');
  const note = detail.getByLabel('Approval resolution note');
  await expect(note).toBeVisible();
  await note.fill('Run Detail note survives the polling refresh');
  await expect(note).toBeFocused();

  await page.waitForTimeout(2_500);

  await expect(note).toHaveValue('Run Detail note survives the polling refresh');
  await expect(note).toBeFocused();
  await Promise.all([
    page.waitForResponse((response) => response.url().includes('/api/approvals/') && response.url().endsWith('/resolve')),
    detail.getByRole('button', { name: 'Reject' }).click(),
  ]);
  await expect(detail.locator('[data-approval-state="REJECTED"]')).toContainText(
    'Run Detail note survives the polling refresh',
  );
});

test('Premium override creates an approval request without claiming routing changed', async ({ page }) => {
  await navTo(page, 'view-providers');
  const routing = page.locator('#usage-routing-body');
  const override = routing.getByRole('button', { name: 'Premium override' }).first();
  await expect(override).toBeVisible();
  await override.click();
  await expect(page.locator('#confirm-body')).toContainText('Allow premium Codex routing');
  await page.locator('#confirm-ok').click();

  await expect(routing.locator('.approval-initiation-status').first()).toContainText(
    'Usage governance remains unchanged until it is approved',
  );
  await navTo(page, 'view-overview');
  const requested = page.locator('#overview-attention-list [data-attention-kind="approval"]')
    .filter({ hasText: 'METERED_OVERFLOW_ROUTE' });
  await expect(requested).toBeVisible();
  await expect(requested).toContainText('PENDING');
});

test('Cleanup preview renders a server refusal instead of a false empty preview', async ({ page }) => {
  await page.route('**/api/commands/worktree_cleanup', async (route) => {
    await route.fulfill({
      status: 409,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'Fixture cleanup preview was refused safely' }),
    });
  });
  await navTo(page, 'view-worktrees');

  await page.getByRole('button', { name: 'Preview cleanup' }).click();

  const result = page.locator('#worktrees-cleanup-result');
  await expect(result).toContainText('Fixture cleanup preview was refused safely');
  await expect(result).not.toContainText('0 eligible');
});

test('Overview retains failed unscoped cleanup targets and reasons without bell inflation', async ({ page }) => {
  await page.route('**/api/attention', async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.approval_results = [{
      id: 'approval-failed-cleanup',
      action_type: 'DESTRUCTIVE_CLEANUP',
      risk: 'HIGH',
      state: 'FAILED_SAFE',
      result_summary: {
        ok: false,
        message: 'removed 1 eligible finished worktree(s); 1 removal(s) failed',
        removed: ['/fixture/removed-clean'],
        failed: [{
          path: '/fixture/still-busy',
          classification: 'FINISHED_CLEAN',
          reason: 'worktree is unexpectedly busy',
        }],
      },
    }];
    await route.fulfill({ response, json: body });
  });
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-initial-refresh-complete', 'true', { timeout: 15_000 });

  const result = page.locator('#overview-attention-list [data-attention-kind="approval_failure"]');
  await expect(result).toContainText('/fixture/still-busy');
  await expect(result).toContainText('worktree is unexpectedly busy');
  await page.locator('#notif-bell').click();
  await expect(page.locator('#attention-list [data-attention-kind="approval_failure"]')).toHaveCount(0);
});
