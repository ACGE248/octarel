// @ts-check
// Shared-fixture isolation for the Control Center viewport matrix.
// Playwright uses one serve_fixture process for every project. Reset the
// deterministic seed at the start of each viewport so later projects do not
// inherit paused/started runbooks from earlier ones.
import { test, expect } from '@playwright/test';

test('reset fixture seed before this viewport project', async ({ request }) => {
  const res = await request.post('/__fixture__/reset');
  expect(res.ok()).toBeTruthy();
  const body = await res.json();
  expect(body.ok).toBe(true);
  const draft = await (await request.get('/api/runbooks/fx-rb-draft')).json();
  expect(draft.status).toBe('DRAFT');
});
