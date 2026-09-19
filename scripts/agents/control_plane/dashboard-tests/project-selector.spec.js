// @ts-check
// ENG-CP-03 (issue #165): browser coverage for the managed-project selector,
// the Add Project flow, and project switching.
//
// The fixture (serve_fixture.py) registers two genuinely independent Git
// repositories: the OctaScene-shaped fixture root (which every other seeded
// task/runbook/worktree is migrated into, through the real
// migrate_legacy_state_to_project path) and "Fixture Project B", a generic
// repository with only a TASKS.md and a non-`main` default branch. That makes
// "no Project A data may remain displayed as if it belonged to Project B" an
// assertion against real state rather than a mock.

import { expect, test } from '@playwright/test';

const OCTASCENE_ID = 'octascene';
const PROJECT_B_ID = 'fixture-project-b';

/**
 * The project selector lives in the sidebar, which is off-canvas on mobile
 * viewports. Open the menu first when it is not already visible.
 */
async function openProjectSwitcher(page) {
  const select = page.locator('#project-select');
  if (await select.isVisible()) return select;
  await page.locator('#menu-toggle').click();
  await expect(page.locator('#sidebar')).toHaveClass(/open/);
  await expect(select).toBeVisible();
  return select;
}

async function selectedProjectId(page) {
  const body = await page.evaluate(async () => {
    const res = await fetch('/api/projects', { cache: 'no-store' });
    return res.json();
  });
  return body.selected_project_id;
}

test.beforeEach(async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('h1')).toHaveText('Control Center');
  // The markup ships a placeholder <option value="">Loading…</option> until
  // /api/projects lands. Wait for the real registry before any selector
  // assertion -- otherwise a fast test can snapshot the placeholder.
  await expect(page.locator('#project-select option[value="octascene"]')).toHaveCount(1);
});

test.afterEach(async ({ page }) => {
  // The fixture server is shared across tests in this file (workers: 1), so
  // always leave OctaScene selected -- otherwise a later spec would inherit
  // Project B and see an intentionally empty Control Center.
  await page.evaluate(
    async (id) => {
      await fetch('/api/projects/select', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_id: id }),
      });
    },
    OCTASCENE_ID
  );
});

test('the selected project is shown with its repository identity', async ({ page }) => {
  const select = await openProjectSwitcher(page);
  await expect(select).toHaveValue(OCTASCENE_ID);
  // "OctaScene / ACGE248/octages" -- display name plus repository identity.
  await expect(select.locator('option:checked')).toHaveText(/OctaScene \/ ACGE248\/octages/);
  await expect(page.locator('#project-identity-remote')).toHaveText('ACGE248/octages');
});

test('the selector offers every registered project', async ({ page }) => {
  const select = await openProjectSwitcher(page);
  await expect(select.locator(`option[value="${OCTASCENE_ID}"]`)).toHaveCount(1);
  await expect(select.locator(`option[value="${PROJECT_B_ID}"]`)).toHaveCount(1);
});

test('desktop: the topbar badge always shows the selected project', async ({ page }) => {
  const width = page.viewportSize()?.width || 0;
  test.skip(width < 1000, 'the topbar badge is a desktop-layout element');
  await expect(page.locator('#project-badge-label')).toHaveText(/OctaScene \/ ACGE248\/octages/);
});

test('switching projects refreshes every project-dependent view with no stale data', async ({ page }) => {
  // OctaScene (the fixture root) owns all the seeded tasks and runbooks.
  const select = await openProjectSwitcher(page);
  const octasceneTasks = await page.evaluate(async () => (await (await fetch('/api/tasks')).json()).length);
  expect(octasceneTasks).toBeGreaterThan(0);

  await select.selectOption(PROJECT_B_ID);
  await expect.poll(async () => selectedProjectId(page)).toBe(PROJECT_B_ID);

  // Project B is a separate repository with no Control Plane work of its own:
  // none of Project A's tasks or runbooks may remain visible.
  await expect
    .poll(async () => page.evaluate(async () => (await (await fetch('/api/tasks')).json()).length))
    .toBe(0);
  await expect
    .poll(async () => page.evaluate(async () => (await (await fetch('/api/runbooks')).json()).length))
    .toBe(0);

  // The visible Tasks view must agree with the API, not keep rendering
  // Project A's rows from before the switch.
  await page.locator('[data-view="view-tasks"]:visible').first().click();
  await expect(page.locator('#view-tasks')).not.toContainText('fx-running-1');

  // And the identity shown everywhere follows the switch.
  const switched = await openProjectSwitcher(page);
  await expect(switched).toHaveValue(PROJECT_B_ID);
  await expect(page.locator('#project-identity-remote')).toHaveText('fixture-org/project-b');
});

test('switching back to OctaScene restores its own work', async ({ page }) => {
  const select = await openProjectSwitcher(page);
  await select.selectOption(PROJECT_B_ID);
  await expect.poll(async () => selectedProjectId(page)).toBe(PROJECT_B_ID);

  const back = await openProjectSwitcher(page);
  await back.selectOption(OCTASCENE_ID);
  await expect.poll(async () => selectedProjectId(page)).toBe(OCTASCENE_ID);
  await expect
    .poll(async () => page.evaluate(async () => (await (await fetch('/api/tasks')).json()).length))
    .toBeGreaterThan(0);
});

test('Add Project detects a repository and shows the detected values before saving', async ({ page }) => {
  await openProjectSwitcher(page);
  await page.locator('#project-add-open').click();
  await expect(page.locator('#project-add-sheet')).toBeVisible();

  // Saving is impossible until a real repository has actually been detected.
  await expect(page.locator('#project-add-save')).toBeDisabled();

  // Detect the fixture's own second repository (a real Git checkout).
  const projectBRoot = await page.evaluate(async (id) => {
    const body = await (await fetch('/api/projects')).json();
    return body.projects.find((p) => p.project_id === id).local_repo_root;
  }, PROJECT_B_ID);

  await page.locator('#project-add-path').fill(projectBRoot);
  await page.locator('#project-add-detect').click();

  const detected = page.locator('#project-detected-list');
  await expect(detected).toBeVisible();
  // Detected values are shown for review, including this repository's own
  // non-`main` default branch -- nothing is assumed to be OctaScene-shaped.
  await expect(detected).toContainText('trunk');
  await expect(detected).toContainText('TASKS.md');
  await expect(page.locator('#project-add-save')).toBeEnabled();

  await page.locator('#project-add-cancel').click();
  await expect(page.locator('#project-add-sheet')).toBeHidden();
});

test('Add Project reports an actionable error for a folder that is not a Git repository', async ({ page }) => {
  await openProjectSwitcher(page);
  await page.locator('#project-add-open').click();
  await page.locator('#project-add-path').fill('/tmp');
  await page.locator('#project-add-detect').click();

  const error = page.locator('#project-add-error');
  await expect(error).toBeVisible();
  await expect(error).toContainText(/Git|does not exist/);
  await expect(page.locator('#project-add-save')).toBeDisabled();

  await page.locator('#project-add-cancel').click();
});

test('Add Project refuses a duplicate repository with a message naming the existing project', async ({ page }) => {
  await openProjectSwitcher(page);
  await page.locator('#project-add-open').click();

  const projectBRoot = await page.evaluate(async (id) => {
    const body = await (await fetch('/api/projects')).json();
    return body.projects.find((p) => p.project_id === id).local_repo_root;
  }, PROJECT_B_ID);

  await page.locator('#project-add-path').fill(projectBRoot);
  await page.locator('#project-add-detect').click();
  await expect(page.locator('#project-add-save')).toBeEnabled();
  // Keep the detected id, which collides with the already-registered project.
  await page.locator('#project-add-id').fill(PROJECT_B_ID);
  await page.locator('#project-add-save').click();

  await expect(page.locator('#project-add-error')).toContainText('already registered');
  await page.locator('#project-add-cancel').click();
});
