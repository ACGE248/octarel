// @ts-check
// OCTAREL-UI-05 (issue #24): Manager Chat.
//
// The fixture server injects a deterministic interpreter (see
// serve_fixture.py's _fixture_manager_invoker), so nothing here makes a live
// or billable provider call. What these tests pin is the behaviour the owner
// decision requires: a deterministic fast path that costs no model call, an
// automatic natural-language route whose worker/provider/model is visible, and
// a destructive request that still has to be confirmed.

import { test, expect } from '@playwright/test';
import { navTo } from './nav-helper.js';

const THREAD = '#manager-thread';

async function send(page, text) {
  await page.fill('#steering-input', text);
  await page.press('#steering-input', 'Enter');
}

test.beforeEach(async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('html')).toHaveAttribute('data-initial-refresh-complete', 'true', { timeout: 15000 });
  await navTo(page, 'view-steering');
  await expect(page.locator('#view-steering')).toBeVisible();
});

test('the interpreter route is shown before anything is sent', async ({ page }) => {
  const label = page.locator('#manager-route');
  await expect(label).toBeVisible();
  // Either a real route or an honest statement that none is available — never
  // silence about how a sentence would be interpreted.
  await expect(label).toContainText(/Interpreter:|No interpreter available/);
});

test('a slash command is answered deterministically with no model call', async ({ page }) => {
  await send(page, '/pause fx-running-1');

  const managerTurn = page.locator(`${THREAD} .manager-msg-manager`).last();
  await expect(managerTurn).toContainText('pause');
  // The fast path must say so: a recognized command costs zero AI calls.
  await expect(managerTurn.locator('.manager-route-line')).toContainText('deterministic');
  await expect(managerTurn.locator('.manager-route-line')).toContainText('no model call');
});

test('natural language is routed automatically and names the model that read it', async ({ page }) => {
  await send(page, 'take a break on that task');

  const managerTurn = page.locator(`${THREAD} .manager-msg-manager`).last();
  await expect(managerTurn).toContainText('pause');
  /* The owner decision requires the selected provider/model to be visible in
     the thread, so a fallback to a different worker is never silent. */
  const route = managerTurn.locator('.manager-route-line');
  await expect(route).not.toContainText('deterministic');
  await expect(route).toContainText('·');
});

test('a natural-language destructive request is proposed, never executed', async ({ page }) => {
  /* The central safety property: natural language may produce a proposed
     execution plan, but the existing server-enforced confirmation still
     applies. Sending the message must issue no command of any kind. */
  const commandRequests = [];
  page.on('request', (request) => {
    const url = request.url();
    if (url.includes('/api/commands/') || url.includes('/api/steering/execute')) {
      commandRequests.push(`${request.method()} ${url}`);
    }
  });

  await send(page, 'can you wind things down for me');

  const managerTurn = page.locator(`${THREAD} .manager-msg-manager`).last();
  await expect(managerTurn).toContainText('stop');
  await expect(managerTurn.locator('.manager-destructive')).toContainText('requires explicit confirmation');

  // The preview offers review, not a one-click destructive run.
  await expect(page.locator('#steering-preview')).toContainText('Review & confirm');
  expect(commandRequests).toEqual([]);
});

test('an uninterpretable message is reported honestly, not guessed', async ({ page }) => {
  await send(page, 'what is the weather in Lisbon');

  const managerTurn = page.locator(`${THREAD} .manager-msg-manager`).last();
  await expect(managerTurn).toHaveClass(/is-unmatched/);
  await expect(managerTurn).toContainText(/no single matching command|No bounded action matched/);
  // Nothing executable is offered for text the Manager could not map.
  await expect(managerTurn.locator('.manager-verb')).toHaveCount(0);
});

test('the conversation keeps both turns in order', async ({ page }) => {
  await send(page, '/pause fx-running-1');
  await expect(page.locator(`${THREAD} .manager-msg`)).toHaveCount(2);

  await send(page, 'take a break on that task');
  await expect(page.locator(`${THREAD} .manager-msg`)).toHaveCount(4);

  const kinds = await page.locator(`${THREAD} .manager-msg`).evaluateAll((nodes) =>
    nodes.map((n) => (n.classList.contains('manager-msg-you') ? 'you' : 'manager')),
  );
  expect(kinds).toEqual(['you', 'manager', 'you', 'manager']);
});
