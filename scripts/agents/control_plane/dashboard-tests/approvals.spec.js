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
