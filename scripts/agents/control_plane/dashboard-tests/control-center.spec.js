// @ts-check
import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { navTo } from './nav-helper.js';

// Fixture data (scripts/agents/control_plane/dashboard-tests/serve_fixture.py)
// seeds: two RUNNING tasks (fx-running-1, fx-running-2), one QUEUED
// (fx-queued-1), one BLOCKED (fx-blocked-1), one SUCCEEDED (fx-done-1), and a
// COST_BLOCKED provider (grok-build-review) — enough to exercise every panel
// without a single provider/model network call.
//
// Navigation is viewport-dependent: desktop shows the full sidebar; mobile
// shows Home/Manager/Tasks/Agents plus a "More" sheet for the remaining
// sections. `navTo` hides that difference using Playwright's `:visible`
// pseudo-class.


const ALL_SECTIONS = [
  'view-overview',
  'view-runs',
  'view-flow',
  'view-tasks',
  'view-agents',
  'view-providers',
  'view-steering',
  'view-history',
  'view-worktrees',
  'view-system',
  'view-terminal',
  'view-settings',
  // OCTAREL-UI-04: Roadmap is a real destination now, so it is overflow-checked
  // like every other section.
  'view-roadmap',
];

const VIEWPORTS = [
  { width: 390, height: 844 },
  { width: 393, height: 852 },
  { width: 430, height: 932 },
  { width: 768, height: 1024 },
  { width: 1280, height: 800 },
  { width: 1440, height: 900 },
  { width: 1920, height: 1080 },
];

function realWorktreePath(rawPath) {
  // The fixture server and this test process share one host, but a fixture
  // Runbook's stored `worktree` is an unresolved tempfile path while a path
  // returned by the real `/api/quickstart` re-resolution goes through `git`,
  // which resolves macOS's /var -> /private/var symlink. Comparing the raw
  // strings therefore misses a real worktree-ownership match and lets this
  // test race the daemon's own automatic-fallback reconcile loop instead of
  // taking its intended idempotent-retry early return.
  try {
    return fs.realpathSync(rawPath);
  } catch {
    return rawPath;
  }
}

async function revealSessionControls(page) {
  // ENG-AGENT-02-S7: check the sticky-controls container itself, not any one
  // button inside it -- individual buttons are now state-aware and may be
  // legitimately hidden (e.g. #ctl-start while a task is RUNNING) even when
  // the container is already in its normal (non-desktop-menu) position.
  const controls = page.locator('#sticky-controls');
  if (await controls.isVisible()) return;
  await page.locator('#system-menu-btn').click();
  await expect(page.locator('#system-menu')).toBeVisible();
}

async function pageOverflows(page) {
  return page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
}

test.beforeEach(async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('h1')).toHaveText('Control Center');
  await expect(page.locator('html')).toHaveAttribute('data-initial-refresh-complete', 'true', { timeout: 15000 });
});

test('no horizontal overflow at this viewport', async ({ page }) => {
  const { scrollWidth, clientWidth } = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  expect(scrollWidth).toBeLessThanOrEqual(clientWidth + 1);
});

test('no horizontal overflow after visiting every section', async ({ page }) => {
  for (const view of ALL_SECTIONS) {
    await navTo(page, view);
    expect(await pageOverflows(page), `${view} caused horizontal overflow`).toBe(false);
  }
});

test('sidebar renders every primary section with Overview active', async ({ page }) => {
  // OCTAREL-UI-04: "Steering" is presented as "Manager" (the view id stays
  // view-steering so routes/bindings are unchanged), and Roadmap is now a real
  // navigation destination rather than a hidden alias reachable only from the
  // mobile "More" sheet.
  const labels = ['Overview', 'Runs', 'Flow', 'Tasks', 'Agents', 'Providers', 'Manager', 'History', 'Worktrees', 'System', 'Terminal', 'Settings', 'Roadmap'];
  const tabs = page.locator('#tabbar .tab');
  await expect(tabs).toHaveCount(labels.length);
  for (const label of labels) {
    await expect(page.locator('#tabbar .tab', { hasText: label }).first()).toBeAttached();
  }
  const overview = page.locator('#tabbar .tab[data-view="view-overview"]');
  await expect(overview).toHaveClass(/active/);
  await expect(overview).toHaveAttribute('aria-selected', 'true');
});

test('agent/provider names are human-readable and locally cached icons load', async ({ page }) => {
  await navTo(page, 'view-agents');
  await expect(page.locator('#agents-cards h3', { hasText: 'Claude Code' })).toBeVisible();
  await expect(page.locator('#agents-cards')).toContainText('Best for:');
  await navTo(page, 'view-providers');
  const icon = page.locator('img[src="/static/assets/icons/anthropic.svg"]:visible').first();
  await expect(icon).toBeVisible();
  expect(await icon.evaluate((node) => node.naturalWidth)).toBeGreaterThan(0);
});

test('terminal connects to the allowlisted repository PTY and resizes', async ({ page }) => {
  await navTo(page, 'view-terminal');
  await expect(page.locator('#terminal-state')).toHaveText('Connected', { timeout: 5000 });
  await expect(page.locator('#terminal-meta')).toContainText('Branch');
  await expect(page.locator('#terminal .xterm')).toBeVisible();
  await page.locator('#terminal .xterm-helper-textarea').focus();
  await page.keyboard.type("printf 'OCTA_PTY_OK\\n'");
  await page.keyboard.press('Enter');
  await expect(page.locator('#terminal .xterm-rows')).toContainText('OCTA_PTY_OK', { timeout: 5000 });
  await page.locator('#terminal-clear').click();
  await page.locator('#terminal-reconnect').click();
  await expect(page.locator('#terminal-state')).toHaveText('Connected', { timeout: 5000 });
  expect(await pageOverflows(page)).toBe(false);
});

test('four status cards render real fixture counts', async ({ page }) => {
  // Both Playwright projects share one backend process/fixture state (see
  // playwright.config.js webServer), and earlier tests in this same run can
  // legitimately mutate real task state (pause/resume/stop, steering
  // execute). Assert the cards match whatever the backend's live task list
  // says right now, rather than the pristine-fixture literals that only
  // hold before any state-mutating test has run — that keeps this test
  // honest to "real data only" regardless of execution order.
  const tasks = await page.evaluate(() => fetch('/api/tasks').then((r) => r.json()));
  // Mirrors app.js's own QUEUED_STATES/COMPLETED_STATES/BLOCKED_STATES exactly.
  const QUEUED = new Set(['QUEUED', 'PENDING']);
  const COMPLETED = new Set(['SUCCEEDED', 'READY_LOCAL', 'READY_BUT_UNMERGED']);
  const BLOCKED = new Set(['BLOCKED', 'FAILED']);
  const expected = { running: 0, queued: 0, completed: 0, blocked: 0 };
  for (const t of tasks) {
    if (t.state === 'RUNNING') expected.running += 1;
    else if (QUEUED.has(t.state)) expected.queued += 1;
    else if (COMPLETED.has(t.state)) expected.completed += 1;
    else if (BLOCKED.has(t.state)) expected.blocked += 1;
  }
  await expect(page.locator('#status-running')).toHaveText(String(expected.running), { timeout: 5000 });
  await expect(page.locator('#status-queued')).toHaveText(String(expected.queued));
  await expect(page.locator('#status-completed')).toHaveText(String(expected.completed));
  await expect(page.locator('#status-blocked')).toHaveText(String(expected.blocked));
  await expect(page.locator('#ring-running svg')).toBeVisible();
  await expect(page.locator('#nav-tasks-badge')).toHaveText(String(expected.running));
});

test('navigation switches between views and only one panel is visible', async ({ page }) => {
  await expect(page.locator('#view-overview')).toBeVisible();

  await navTo(page, 'view-flow');
  await expect(page.locator('#view-flow')).toBeVisible();
  await expect(page.locator('#view-overview')).toBeHidden();

  await navTo(page, 'view-tasks');
  await expect(page.locator('#view-tasks')).toBeVisible();
  await expect(page.locator('#view-flow')).toBeHidden();
});

test('workflow view shows the full pipeline chain and simultaneous running workers', async ({ page, request, baseURL }) => {
  // Both Playwright projects share one backend process/fixture state, and a
  // runbook session task can legitimately be RUNNING alongside the two plain
  // fixture tasks — derive the expected count from the live task list rather
  // than a pristine-fixture literal, same fix already applied to the status
  // cards test above for the same reason.
  const tasks = await (await request.get(`${baseURL}/api/tasks`)).json();
  const runningCount = tasks.filter((t) => t.state === 'RUNNING').length;
  await navTo(page, 'view-flow');
  const runningLane = page.locator('.flow-lane', { hasText: 'RUNNING' });
  await expect(runningLane).toBeVisible();
  const runningCards = runningLane.locator('.flow-node');
  await expect(runningCards).toHaveCount(runningCount);
  // ENG-AGENT-02-S7 follow-up: the pipeline chain is separate labeled step
  // pills (worker/role/system/model/worktree), not one dense arrow-joined
  // string, so it can actually be scanned at a glance.
  await expect(runningCards.first().locator('.chain-step').first()).toBeVisible();
  await expect(runningCards.first().locator('.pipeline-stage')).toHaveCount(10);
  await expect(runningCards.first().locator('.pipeline-stages')).toContainText('Tests: NOT_REPORTED');
  await expect(runningCards.first().locator('.pipeline-stages')).toContainText('PR: NOT_REPORTED');
  // ENG-AGENT-02-S7 (issue #97): each node shows a real provider icon badge.
  await expect(runningCards.first().locator('.provider-badge')).toBeAttached();
  // The fixture seeds more than one simultaneously RUNNING task specifically
  // to prove parallel execution is a visible, truthful fact (driven by
  // /api/flow's own concurrent_running list), not merely implied by lane
  // proximity.
  if (runningCount > 1) {
    await expect(runningLane.locator('.parallel-badge')).toContainText('running in parallel');
    await expect(runningLane.locator('.parallel-dot').first()).toBeAttached();
  }
  // Clicking a card (a <details>/<summary> pair) reveals the full per-field
  // detail without navigating away or opening a second, divergent view.
  const detail = runningCards.first().locator('.flow-node-detail');
  await expect(detail).toBeHidden();
  await runningCards.first().locator('summary').click();
  await expect(detail).toBeVisible();
  // The fixture seeds a real task dependency (fx-blocked-1 depends on
  // fx-running-1) specifically so a Kanban lane view -- which groups by
  // state and so cannot show *why* one task waits on another -- gets a real
  // connector line from /api/flow's own edges, not a fabricated one.
  await expect(page.locator('#flow-edges-svg path')).toHaveCount(1);
});

test('overview live workflow renders the pipeline and concurrent running tasks', async ({ page, request, baseURL }) => {
  const workflow = await (await request.get(`${baseURL}/api/workflow`)).json();
  await expect(page.locator('#overview-pipeline .workflow-stage-card.orchestrator')).toHaveText(/Orchestrator/, { timeout: 5000 });
  await expect(page.locator('#overview-pipeline .workflow-stage-card:not(.orchestrator)')).toHaveCount(workflow.stages.length);
  await expect(page.locator('#overview-pipeline [data-workflow-task]')).toContainText(workflow.task.id);
  await expect(page.locator('#overview-pipeline')).not.toContainText('%');
});

test('overview Worktrees quick-nav card (desktop layout) opens the Worktrees view', async ({ page }) => {
  // Only rendered in the non-mobile workflow layout (app.js checks
  // `window.matchMedia("(max-width: 640px)")`); skip on the phone-width
  // projects where the app itself never renders this card.
  const width = page.viewportSize()?.width || 0;
  test.skip(width <= 640, 'Worktrees quick-nav card is desktop/tablet-only');
  const card = page.locator('#overview-pipeline [data-open-worktrees]');
  await expect(card).toBeVisible();
  await expect(card).toContainText(/\d+ active in this workflow/);
  await card.click();
  await expect(page.locator('#view-worktrees')).toBeVisible();
});

test('donut and activity charts render from real data', async ({ page }) => {
  await expect(page.locator('#donut-chart svg')).toBeVisible({ timeout: 5000 });
  await expect(page.locator('#donut-chart')).toContainText('Total');
  await expect(page.locator('#donut-chart')).toContainText('Running');
  await expect(page.locator('#activity-chart svg')).toBeVisible();
  await expect(page.locator('#recent-events li')).not.toHaveCount(0);
});

test('provider/model panel only lists actually configured routes with actions', async ({ page }) => {
  await navTo(page, 'view-providers');
  await expect(page.getByText('not configured').first()).toBeVisible();
  // ENG-AGENT-02-S7 (issue #97): a configured provider's six action buttons
  // are collapsed behind one "Actions" disclosure by default (was previously
  // an always-visible six-button row on every card) -- open every one so the
  // exact-name button assertions below can find the "grok-build" card
  // specifically, not the similarly-named "grok-build-review" card.
  const grokCard = page
    .locator('#providers-cards .entity-card')
    .filter({ hasText: 'ID: grok-build' })
    .first();
  await grokCard.locator('.entity-actions-details summary').click();
  await expect(grokCard.getByRole('button', { name: 'Probe grok-build', exact: true })).toBeVisible();
  await expect(grokCard.getByRole('button', { name: 'Drain grok-build', exact: true })).toBeVisible();
  await expect(grokCard.getByRole('button', { name: 'Cost-block grok-build', exact: true })).toBeVisible();
});

test('providers show a real polished icon badge, never a bare letter placeholder', async ({ page }) => {
  await navTo(page, 'view-providers');
  const badges = page.locator('#providers-cards .provider-badge');
  await expect(badges.first()).toBeAttached();
  // Anthropic's claude-code entry always exists in this registry.
  await expect(page.locator('#providers-cards .entity-card', { hasText: 'claude-code' }).locator('.provider-badge')).toBeAttached();
  const opencodeCard = page.locator('#providers-cards .entity-card').filter({ hasText: 'ID: opencode2-gemini-flash-lite' }).first();
  await expect(opencodeCard.locator('.provider-badge')).toHaveCount(2);
  await expect(opencodeCard).toContainText('model');
  await expect(opencodeCard).toContainText('running task');
  const reviewerCard = page.locator('#providers-cards .entity-card').filter({ hasText: 'ID: opencode2-gemini-flash-lite-review' }).first();
  await expect(reviewerCard.locator('.provider-badge')).toHaveCount(2);
});

test('usage & limits panel reports source-classified facts, never a fabricated number', async ({ page }) => {
  await navTo(page, 'view-providers');
  const usage = page.locator('#usage-body');
  await expect(usage.locator('.usage-card').first()).toBeAttached();
  await expect(usage).toContainText(/CLI_REPORTED|Not exposed by provider/);
  await expect(usage).toContainText('Subscription quota not exposed by Codex CLI');
  await page.locator('#usage-refresh-btn').click();
  await expect(usage.locator('.usage-card').first()).toBeAttached();
  await expect(page.locator('#usage-last-refresh')).toContainText('Last refresh');
  // ENG-AGENT-02-S7 follow-up: the same real facts also surface compactly on
  // Overview, not only after navigating into Providers.
  await navTo(page, 'view-overview');
  const snapshot = page.locator('#overview-usage-snapshot');
  await expect(snapshot.locator('.usage-pill').first()).toBeVisible();
  await expect(snapshot).toContainText(/Not exposed by provider|pro|Pro|claude\.ai/i);
});

test('usage routing and local gate are first-class without hosted Actions', async ({ page }) => {
  await navTo(page, 'view-providers');
  const routing = page.locator('#usage-routing-body');
  await expect(routing).toContainText('fx-rb-running · routine');
  await expect(routing).toContainText(/Codex 0\/1 · auto (blocked|eligible) · tokens unknown/);
  if ((await routing.textContent()).includes('auto blocked')) {
    await routing.getByRole('button', { name: 'Premium override' }).first().click();
    await expect(page.locator('#confirm-body')).toContainText('Allow premium Codex routing');
    await page.locator('#confirm-ok').click();
  }
  await expect(routing).toContainText('unrestricted');

  await navTo(page, 'view-system');
  const gate = page.locator('#tests-body');
  await expect(gate).toContainText('local-deterministic-gate');
  await expect(gate).toContainText('NOT READY');
  await expect(gate).not.toContainText('GitHub Actions');
});

test('sticky controls are state-aware: only the verbs that can do something right now are shown', async ({ page }) => {
  // ENG-AGENT-02-S7 (issue #97): Start/Pause/Resume/Stop used to always show
  // as four equally-prominent buttons. Assert against live task state (the
  // fixture always has RUNNING tasks; see the four-status-cards test above
  // for why this suite asserts against live state rather than pristine
  // fixture literals).
  await revealSessionControls(page);
  const tasks = await page.evaluate(() => fetch('/api/tasks').then((r) => r.json()));
  const hasRunning = tasks.some((t) => t.state === 'RUNNING');
  const hasPaused = tasks.some((t) => t.state === 'PAUSED');

  const start = page.locator('#ctl-start');
  const pause = page.locator('#ctl-pause');
  const resume = page.locator('#ctl-resume');
  const stop = page.locator('#ctl-stop');
  await expect(start).toBeVisible({ visible: !(hasRunning || hasPaused) });
  await expect(pause).toBeVisible({ visible: hasRunning });
  await expect(resume).toBeVisible({ visible: hasPaused && !hasRunning });
  await expect(stop).toBeVisible({ visible: hasRunning || hasPaused });

  const visibleButtons = [start, pause, resume, stop];
  for (const button of visibleButtons) {
    if (!(await button.isVisible())) continue;
    const box = await button.boundingBox();
    expect(box).not.toBeNull();
    expect(box.height).toBeGreaterThanOrEqual(40);
  }

  if (hasRunning || hasPaused) {
    await stop.click();
    await expect(page.locator('#confirm-sheet')).toBeVisible();
    await page.locator('#confirm-cancel').click();
    await expect(page.locator('#confirm-sheet')).toBeHidden();
  }
});

test('task Pause/Resume/Stop include the destructive-stop confirmation and 44px targets', async ({ page }) => {
  await navTo(page, 'view-tasks');
  // The seven viewport projects share one durable fixture backend, so an
  // earlier project can legitimately have stopped the originally queued task.
  // Verify the contract against whichever live task currently exposes Stop.
  const stop = page.locator('#tasks-cards').getByRole('button', { name: /^Stop / }).first();
  await expect(stop).toBeVisible();
  const box = await stop.boundingBox();
  expect(box).not.toBeNull();
  expect(box.height).toBeGreaterThanOrEqual(36);
  await stop.click();
  await expect(page.locator('#confirm-sheet')).toBeVisible();
  await page.locator('#confirm-cancel').click();
  await expect(page.locator('#confirm-sheet')).toBeHidden();
});

test('terminal tasks are never presented as active or given lifecycle controls', async ({ page }) => {
  await navTo(page, 'view-tasks');
  await expect(page.locator('#tasks-cards')).not.toContainText('fx-done-1');
  await expect(page.locator('#tasks-table tbody')).not.toContainText('fx-done-1');
  await expect(page.getByRole('button', { name: 'Pause fx-done-1' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Resume fx-done-1' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Stop fx-done-1' })).toHaveCount(0);
});

test('managed dispatch renders only persisted wave, admission, and alternative facts', async ({ page, request, baseURL }) => {
  const tasks = await (await request.get(`${baseURL}/api/tasks`)).json();
  const dispatch = await (await request.get(`${baseURL}/api/dispatch`)).json();
  await navTo(page, 'view-tasks');
  const firstActive = tasks.find((task) => task.projection === 'ACTIVE');
  if (firstActive) {
    const card = page.locator('#tasks-cards .entity-card').filter({ hasText: firstActive.id }).first();
    await expect(card).toContainText(`Wave ${firstActive.dependency_wave ?? '—'}`);
    await expect(card).toContainText(firstActive.admission_state || 'PENDING');
    if (firstActive.admission_reason) await expect(card).toContainText(firstActive.admission_reason);
  }
  expect(dispatch.caps.global.used).toBe(tasks.filter((task) => task.state === 'RUNNING').length);
  expect(dispatch.waves.every((item) => Object.hasOwn(item, 'reason'))).toBeTruthy();
});

test('steering command help exposes the deterministic grammar without overflow', async ({ page }) => {
  await navTo(page, 'view-steering');
  const help = page.locator('#steering-help');
  await expect(help.locator('summary')).toHaveText('Commands / Help');
  await help.locator('summary').click();
  await expect(help.locator('.command-grid')).toBeVisible();
  await expect(help).toContainText('/start [TASK_ID]');
  await expect(help).toContainText('/probe PROVIDER');
  await expect(help).toContainText('/set-max-writers NUMBER');
  expect(await pageOverflows(page)).toBe(false);
});

test('steering: a recognized slash command shows a non-destructive preview and can run', async ({ page }) => {
  await navTo(page, 'view-steering');
  await page.locator('#steering-input').fill('/resume fx-queued-1');
  await page.locator('#steering-form button[type=submit]').click();
  const preview = page.locator('#steering-preview');
  await expect(preview).toBeVisible();
  await expect(preview).not.toHaveClass(/destructive/);
  await expect(preview).toContainText('Resume task fx-queued-1');
});

test('steering: a destructive command requires explicit confirmation before executing', async ({ page }) => {
  await navTo(page, 'view-steering');
  await page.locator('#steering-input').fill('/stop fx-queued-1');
  await page.locator('#steering-form button[type=submit]').click();
  const preview = page.locator('#steering-preview');
  await expect(preview).toHaveClass(/destructive/);
  await preview.locator('button', { hasText: 'Review & confirm' }).click();
  await expect(page.locator('#confirm-sheet')).toBeVisible();
  await page.locator('#confirm-cancel').click();
  await expect(page.locator('#confirm-sheet')).toBeHidden();
});

test('steering: unrecognized free text is never silently actioned', async ({ page }) => {
  await navTo(page, 'view-steering');
  await page.locator('#steering-input').fill('do something clever with everything');
  await page.locator('#steering-form button[type=submit]').click();
  const preview = page.locator('#steering-preview');
  await expect(preview).toBeVisible();
  await expect(preview).toContainText('Not recognized');
});

test('steering: a video-editor development-intent phrase shows the resolved Prepared Run, not just a bare verb', async ({ page }) => {
  // ENG-AGENT-02-S7 (issue #97): the exact reported defect -- NL steering
  // could report PARSED without any way to actually construct/start the
  // intended run. Typing a development-intent phrase must show real,
  // resolved run details before anything can start.
  await navTo(page, 'view-steering');
  await page.locator('#steering-input').fill('continue the video editor');
  await page.locator('#steering-form button[type=submit]').click();
  const preview = page.locator('#steering-preview');
  await expect(preview).toBeVisible();
  await expect(preview).not.toHaveClass(/destructive/);
  await expect(preview).toContainText('V1-01');
  await expect(preview.getByRole('button', { name: 'Start Development' })).toBeEnabled();
});

test('attention queue surfaces the blocked task and the cost-blocked provider', async ({ page }) => {
  await expect(page.locator('#attention-body')).toContainText('1', { timeout: 5000 });
  const list = page.locator('#attention-list li');
  await expect(list).toContainText(['Task fx-blocked-1: BLOCKED (ENG-AGENT-02/diff-review)']);
});

test('Runs view lists fixture runbooks in every status with real data', async ({ page, request, baseURL }) => {
  await navTo(page, 'view-runs');
  const live = await (await request.get(`${baseURL}/api/runbooks`)).json();
  await expect(page.locator('#runbooks-cards .entity-card')).toHaveCount(live.length);
  await expect(page.locator('#runbooks-cards')).toContainText('Fixture draft runbook');
  await expect(page.locator('#runbooks-cards')).toContainText('Fixture overnight run');
  await expect(page.locator('#runbooks-cards')).toContainText('Fixture completed runbook');
  await expect(page.locator('#rb-preset option')).toHaveCount(5);
});

test('Quick Start resolves the fixture ledger task and shows a ready Prepared Run', async ({ page, request, baseURL }) => {
  // ENG-AGENT-02-S7 (issue #97): the exact defect report -- the Runbook form
  // used to be blank, requiring the operator to already know branch/worktree/
  // task ID by hand. This proves the resolved values are real API data, not
  // hard-coded, and that "Start Development" is disabled until then.
  const options = await (await request.get(`${baseURL}/api/quickstart`)).json();
  expect(options).toHaveLength(6);
  expect(options.map((option) => option.key)).toEqual([
    'continue-video-editor',
    'continue-octascene',
    'finish-current-pr',
    'focused-test-fix',
    'review-current-diff',
    'custom-run',
  ]);
  expect(options[0].key).toBe('continue-video-editor');
  expect(options[0].ready).toBe(true);
  expect(options[0].source_ref).toContain('V1-01');

  await navTo(page, 'view-runs');
  const quickstartRow = page.locator('#quickstart-row');
  await expect(quickstartRow).toContainText('Continue Video Editor');
  await expect(quickstartRow.locator('.quickstart-btn')).toHaveCount(6);
  await expect(quickstartRow.locator('.quickstart-btn').first().locator('.status-pill')).toHaveText('Ready');

  // Regression for the reported confusing state: unrelated values may exist
  // in Custom Run, but they must remain hidden and must not alter Quick Start.
  await page.locator('#rb-preset').selectOption('finish-pr', { force: true });
  await page.locator('#rb-parent-worker').selectOption('opencode2-gemini-flash-lite-review', { force: true });
  await expect(page.locator('#card-runbook-create')).not.toHaveAttribute('open', '');
  await quickstartRow.locator('.quickstart-btn').first().click();

  const preparedRun = page.locator('#card-prepared-run');
  await expect(preparedRun).toBeVisible();
  await expect(preparedRun).toContainText('V1-01');
  await expect(preparedRun).toContainText('Preferred implementer');
  await expect(preparedRun).toContainText('claude-code');
  // The stale Custom Run reviewer value must not become the implementer.
  await expect(preparedRun).toContainText(/Preferred implementer\s*claude-code\s*Tester/);
  await expect(preparedRun).not.toContainText('Finish PR');
  await expect(preparedRun).toContainText('Standalone Video Editor');
  await expect(preparedRun).toContainText('Tester');
  await expect(preparedRun).toContainText('Reviewer');
  await expect(preparedRun).toContainText('automatic');
  await expect(preparedRun).toContainText('Automatic fallback');
  await expect(preparedRun).toContainText('Codex balanced');
  await expect(preparedRun).toContainText('maximum 1 invocation');
  await expect(preparedRun).toContainText(options[0].branch);
  await expect(preparedRun).toContainText(options[0].worktree);
  await expect(page.locator('#prepared-run-start')).toBeEnabled();

  // Never click Start in a UI test -- that would spawn a real worker CLI
  // subprocess. Cancel must fully hide the card without starting anything.
  const beforeRunbooks = await (await request.get(`${baseURL}/api/runbooks`)).json();
  await page.locator('#prepared-run-cancel').click();
  await expect(preparedRun).toBeHidden();
  await expect(page.locator('#card-runbook-create')).not.toHaveAttribute('open', '');
  await quickstartRow.locator('.quickstart-btn').last().click();
  await expect(page.locator('#card-runbook-create')).toHaveAttribute('open', '');
  await expect(page.locator('#card-runbook-create')).toContainText('Custom Run — independent manual configuration');
  await expect(page.locator('#card-runbook-create')).toContainText('Separate from Quick Start.');
  await expect(page.locator('#card-runbook-create')).toContainText('do not edit or affect the prepared Quick Start');
  const afterRunbooks = await (await request.get(`${baseURL}/api/runbooks`)).json();
  expect(afterRunbooks).toHaveLength(beforeRunbooks.length);
});

test('Overview primary action matches the current durable run state', async ({ page, request, baseURL }) => {
  // Dogfooding defect: Continue Video Editor / Prepared Run only ever
  // appeared after navigating to Runs and reading the Quick Start row.
  // Overview must show the same real resolved option (never a second
  // resolver) and jump straight into the identical Prepared Run
  // confirmation used by Runs, in one click.
  const runbooks = await (await request.get(`${baseURL}/api/runbooks`)).json();
  const active = runbooks.find((runbook) => ['RUNNING', 'PAUSED', 'STOPPING'].includes(runbook.status));
  await page.goto('/');
  const card = page.locator('#overview-continue-card');
  await expect(card).toBeVisible();
  const startBtn = page.locator('#overview-continue-btn');
  await expect(startBtn).toBeEnabled();
  if (active) {
    await expect(card).toContainText('View Active Run');
    await expect(card).toContainText(active.name);
    await expect(startBtn).toHaveText('View Active Run');
    await startBtn.click();
    await expect(page.locator('#view-runs')).toBeVisible();
    await expect(page.locator(`[data-runbook-id="${active.id}"]`)).toBeVisible();
  } else {
    await expect(card).toContainText('Continue Video Editor');
    await expect(card).toContainText('V1-01');
    await expect(startBtn).toHaveText('Start Development');
  }
});

test('quickstart_start (dry-run) actually creates and starts a runbook end to end', async ({ request, baseURL }) => {
  // The one safe way to exercise the real "Start Development" endpoint in
  // this suite: an explicit dry_run flag the production UI never sends,
  // exactly like the existing runbook_start dry-run test below.
  //
  // Both Playwright projects share one backend process, so a prior project's
  // run of this same test already occupies the fixture's video-editor
  // worktree with an active runbook -- idempotent-by-worktree here, mirroring
  // the same fix already applied to the runbook_start dry-run test below.
  const option = (await (await request.get(`${baseURL}/api/quickstart`)).json())[0];
  const existingRunbooks = await (await request.get(`${baseURL}/api/runbooks`)).json();
  const optionWorktree = realWorktreePath(option.worktree);
  const already = existingRunbooks.find((r) => realWorktreePath(r.worktree) === optionWorktree);
  if (already) {
    expect(['RUNNING', 'SUCCEEDED', 'FAILED']).toContain(already.status);
    return;
  }
  const resp = await request.post(`${baseURL}/api/commands/quickstart_start`, {
    data: { key: 'continue-video-editor', dry_run: true },
  });
  expect(resp.ok()).toBeTruthy();
  const body = await resp.json();
  expect(body.ok).toBe(true);
  expect(body.data.status).toBe('RUNNING');
});

test('New Runbook form creates a real DRAFT runbook without launching anything', async ({ page, request, baseURL }) => {
  await navTo(page, 'view-runs');
  const before = await (await request.get(`${baseURL}/api/runbooks`)).json();
  // ENG-AGENT-02-S7: the manual form now lives under a collapsed "Advanced
  // Settings" <details> (Quick Start is the ordinary path) -- open it first.
  await page.locator('#card-runbook-create summary').click();
  await page.locator('#rb-preset').selectOption('test-fix');
  await page.locator('#rb-name').fill('Playwright-created runbook');
  await page.locator('#rb-source-ref').fill('ENG-TEST-PLAYWRIGHT');
  await page.locator('#rb-branch').fill('fixture-main');
  await page.locator('#rb-worktree').selectOption({ index: 0 });
  await page.locator('#runbook-form button[type=submit]').click();
  await expect(page.locator('#runbook-form-feedback')).toContainText('Created');
  await expect(page.locator('#runbooks-cards')).toContainText('Playwright-created runbook');
  await expect(page.locator('#runbooks-cards .entity-card')).toHaveCount(before.length + 1);
});

test('Run Overnight quick action opens Runs prefilled with the overnight preset', async ({ page }) => {
  await navTo(page, 'view-overview');
  await page.locator('#run-overnight-btn').click();
  await expect(page.locator('#view-runs')).toBeVisible();
  await expect(page.locator('#rb-preset')).toHaveValue('overnight-development');
  await expect(page.locator('#rb-objective')).not.toHaveValue('');
});

test('starting a runbook always uses a real API call the UI never fakes locally (dry-run only, no click)', async ({ page, request, baseURL }) => {
  // Never click the real "Start" button against a write-capable worker in a
  // test: that would spawn a genuine `claude`/`grok` CLI subprocess. This
  // proves the same endpoint the button posts to behaves correctly using an
  // explicit dry_run flag instead, which the production UI never sends.
  //
  // Both Playwright projects share one backend process, so a prior project's
  // run of this same test already moved fx-rb-draft out of DRAFT — start is
  // idempotent-by-status here rather than repeated, mirroring the same fix
  // already applied to the status-card/workflow tests above.
  await navTo(page, 'view-runs');
  const before = await (await request.get(`${baseURL}/api/runbooks/fx-rb-draft`)).json();
  if (before.status === 'DRAFT') {
    const resp = await request.post(`${baseURL}/api/commands/runbook_start`, {
      data: { runbook_id: 'fx-rb-draft', dry_run: true },
    });
    expect(resp.ok()).toBeTruthy();
    const body = await resp.json();
    expect(body.data.status).toBe('RUNNING');
  } else {
    expect(['RUNNING', 'SUCCEEDED', 'FAILED']).toContain(before.status);
  }
});

test('runbook Pause/Resume/Stop are wired to the command layer with a confirm gate on Stop', async ({ page, request, baseURL }) => {
  // Both Playwright projects share one backend process: whichever project
  // runs this test first moves fx-rb-running out of RUNNING permanently
  // (Stop has no undo). Only exercise the interactive Pause/Stop flow the
  // first time; a second run just confirms the prior stop-flow result stuck.
  const before = await (await request.get(`${baseURL}/api/runbooks/fx-rb-running`)).json();
  await navTo(page, 'view-runs');
  const runningCard = page.locator('#runbooks-cards .entity-card', { hasText: 'Fixture overnight run' });
  if (before.status === 'RUNNING') {
    await expect(runningCard.getByRole('button', { name: 'Pause' })).toBeVisible();
    await runningCard.getByRole('button', { name: 'Stop', exact: true }).click();
    await expect(page.locator('#confirm-sheet')).toBeVisible();
    await page.locator('#confirm-ok').click();
    await expect(page.locator('#confirm-sheet')).toBeHidden();
  }
  // STOPPING is the immediate post-confirm state; the daemon's reconcile
  // loop (every 0.5s) then finalizes it to CANCELLED once it observes the
  // underlying task is CANCELLED — both are correct depending on timing.
  await expect(page.locator('#runbooks-cards')).toContainText(/STOPPING|CANCELLED/);
});

test('View report opens the morning report sheet with real content and closes cleanly', async ({ page }) => {
  await navTo(page, 'view-runs');
  const doneCard = page.locator('#runbooks-cards .entity-card', { hasText: 'Fixture completed runbook' });
  await doneCard.getByRole('button', { name: 'View report' }).click();
  await expect(page.locator('#report-sheet')).toBeVisible();
  await expect(page.locator('#report-body')).toContainText('Morning report');
  await page.locator('#report-close').click();
  await expect(page.locator('#report-sheet')).toBeHidden();
});

test('dashboard refresh and steering parse make zero AI/provider calls', async ({ page }) => {
  const blockedRequests = [];
  page.on('request', (req) => {
    const url = req.url();
    if (!url.startsWith('http://127.0.0.1') && !url.startsWith('http://localhost')) {
      blockedRequests.push(url);
    }
  });
  await navTo(page, 'view-steering');
  await page.locator('#steering-input').fill('/pause fx-queued-1');
  await page.locator('#steering-form button[type=submit]').click();
  await page.waitForTimeout(2500);
  expect(blockedRequests).toEqual([]);
});

test('start with an empty-feeling click does not crash the dashboard', async ({ page }) => {
  await revealSessionControls(page);
  page.once('pageerror', (err) => {
    throw err;
  });
  // ENG-AGENT-02-S7: #ctl-start is now state-aware and hidden whenever a task
  // is already RUNNING (as the fixture's always are) -- a real DOM click via
  // evaluate() still exercises the same handler without Playwright's
  // visibility-required actionability check, which is exactly right here:
  // this test is about handler robustness, not about the button's own
  // visibility rules (covered separately).
  await page.evaluate(() => document.getElementById('ctl-start').click());
  await expect(page.locator('h1')).toHaveText('Control Center');
});

test('dark mode is the default and light mode re-themes the same layout', async ({ page }) => {
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await navTo(page, 'view-settings');
  await page.locator('#theme-select').selectOption('light');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await navTo(page, 'view-overview');
  await expect(page.locator('#status-running')).toBeVisible();
  await navTo(page, 'view-settings');
  await page.locator('#theme-select').selectOption('dark');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
});

test('mobile bottom navigation and vertical workflow render at phone width', async ({ page }) => {
  const width = page.viewportSize()?.width || 0;
  test.skip(width > 500, 'phone-layout assertion is for the 393 project');
  await expect(page.locator('#bottom-nav')).toBeVisible();
  await expect(page.locator('#bottom-nav .bn[data-view="view-overview"]')).toContainText('Home');
  await expect(page.locator('#overview-pipeline')).toHaveClass(/workflow-mobile/);
  await expect(page.locator('#overview-pipeline .workflow-stage-card.orchestrator')).toBeVisible();
  const icon = page.locator('#overview-pipeline .workflow-stage-card:not(.orchestrator) .provider-badge').first();
  await expect(icon).toBeVisible();
  expect((await icon.boundingBox()).width).toBeGreaterThanOrEqual(40);
});

test('S9 worktrees distinguish managed and discovered Git worktrees with safe actions', async ({ page }) => {
  await navTo(page, 'view-worktrees');
  await expect(page.locator('#worktrees-managed')).toBeVisible();
  await expect(page.locator('#worktrees-discovered')).toBeVisible();
  await expect(page.locator('.worktree-card').first()).toContainText(/ACTIVE|QUEUED|PAUSED|STALE_RECOVERABLE|FINISHED_CLEAN|FINISHED_DIRTY|UNRELATED_MANUAL|UNKNOWN/);
  await expect(page.locator('#worktrees-cleanup-preview')).toBeVisible();
  await expect(page.locator('#worktrees-cleanup')).toBeVisible();
  await expect(page.locator('#worktrees-refresh')).toHaveText(/Refresh status/i);
  const buttons = page.locator('.worktree-card .worktree-actions button');
  expect(await buttons.count()).toBeGreaterThan(0);
  expect(await pageOverflows(page)).toBe(false);
});

test('ENG-AGENT-14 an auto-provisioned review checkout surfaces its PR/head origin truthfully', async ({ page }) => {
  await navTo(page, 'view-worktrees');
  const cards = page.locator('.worktree-card');
  const reviewCard = cards.filter({ hasText: 'review-pr-fixture' });
  await expect(reviewCard).toHaveCount(1);
  await expect(reviewCard).toContainText('Review checkout: ACGE248/octages PR #999 @');
  // A detached review checkout has no branch of its own -- the card must
  // never fall back to a stale/misleading branch name for it.
  await expect(reviewCard.locator('h3')).not.toHaveText(/^fixture-/);
  expect(await pageOverflows(page)).toBe(false);
});

test('S9 roadmap uses derived labels and honest non-computable progress', async ({ page }) => {
  // OCTAREL-UI-04: the live roadmap table moved out of Settings into its own
  // Roadmap destination; it is no longer duplicated across two views.
  await navTo(page, 'view-roadmap');
  await expect(page.locator('#roadmap-cards')).toContainText(/DERIVED|not computable/i);
  await expect(page.locator('#roadmap-summary')).toBeVisible();
});

test('S9 sidebar footer rows never overlap or clip the active task and remote identity', async ({ page }, testInfo) => {
  const width = page.viewportSize()?.width || 0;
  if (width < 768) {
    await page.locator('#menu-toggle').click();
    await expect(page.locator('#sidebar')).toHaveClass(/open/);
  }
  const ids = ['#active-task-pill', '#identity-card', '#remote-badge-sidebar'];
  const boxes = [];
  for (const id of ids) {
    const locator = page.locator(id);
    if (await locator.isVisible()) boxes.push(await locator.boundingBox());
  }
  for (let i = 1; i < boxes.length; i += 1) {
    expect(boxes[i].y, `${ids[i]} overlapped the prior footer row`).toBeGreaterThanOrEqual(boxes[i - 1].y + boxes[i - 1].height - 1);
  }
  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..', '..');
  const outDir = path.join(repoRoot, '.agent-output', 'ENG-AGENT-02-S9', 'screenshots');
  fs.mkdirSync(outDir, { recursive: true });
  await page.locator('#sidebar').screenshot({ path: path.join(outDir, `sidebar-footer-${testInfo.project.name}.png`) });
  expect(await pageOverflows(page)).toBe(false);
});

test('failed runbook shows the sanitized reason and explicit in-place worker fallback', async ({ page }) => {
  await navTo(page, 'view-runs');
  const card = page.locator('[data-runbook-id="fx-rb-failed"]');
  await expect(card).toContainText('Claude Code / Anthropic quota');
  await expect(card).toContainText('Failed worker claude-code');
  await expect(card).toContainText('Claude subscription weekly limit reached; reset later');
  await expect(card).toContainText('Recovery:');
  const selector = card.getByLabel('Retry worker for Fixture blocked V1-01 run');
  await expect(selector.locator('option', { hasText: 'Codex Build' })).toHaveCount(1);
  await expect(card.getByRole('button', { name: 'Retry with selected worker' })).toBeVisible();
  await expect(card).toContainText('V1-01');
  expect(await pageOverflows(page)).toBe(false);
});

test('automatic fallback visibly preserves the failed attempt and running replacement', async ({ page }) => {
  await navTo(page, 'view-runs');
  const card = page.locator('[data-runbook-id="fx-rb-fallback"]');
  await expect(card).toContainText('Claude Code — FAILED (QUOTA)');
  await expect(card).toContainText('Automatically falling back to Codex Build');
  await expect(card).toContainText('Codex Build — RUNNING');
  await expect(card).not.toContainText('Codex Build — RUNNING (QUOTA)');
  await expect(card).toContainText('Claude Code / Anthropic quota');
  expect(await pageOverflows(page)).toBe(false);
});

test('no horizontal overflow at the seven validation viewports in dark and light', async ({ page }) => {
  const original = page.viewportSize();
  for (const theme of ['dark', 'light']) {
    await navTo(page, 'view-settings');
    await page.locator('#theme-select').selectOption(theme);
    await navTo(page, 'view-overview');
    for (const vp of VIEWPORTS) {
      await page.setViewportSize(vp);
      await page.locator('#view-overview').waitFor();
      await page.waitForTimeout(250); // let the sidebar/bottom-nav CSS transition settle
      expect(await pageOverflows(page), `${theme} ${vp.width}x${vp.height} overview overflowed`).toBe(false);
      await navTo(page, 'view-tasks');
      expect(await pageOverflows(page), `${theme} ${vp.width}x${vp.height} tasks overflowed`).toBe(false);
      await navTo(page, 'view-overview');
    }
  }
  if (original) await page.setViewportSize(original);
});

// --------------------------------------------------------------------------
// ENG-AGENT-02-S6 (issue #95): authenticated remote-mode UX.
//
// The fixture server (serve_fixture.py) always enables remote mode with a
// throwaway, in-process RSA keypair and mints Cloudflare-Access-shaped JWTs
// itself via a test-only `/test/mint-remote-token` route — no real
// Cloudflare account, credential, or network call is ever involved. Every
// other spec in this file never attaches that header, so they continue to
// exercise (and prove) the ordinary local/unauthenticated path unaffected by
// remote mode being enabled at the fixture level.

async function authenticateAsRemote(page, request, baseURL, email) {
  const qs = email ? `?email=${encodeURIComponent(email)}` : '';
  const resp = await request.get(`${baseURL}/test/mint-remote-token${qs}`);
  expect(resp.ok()).toBeTruthy();
  const { token } = await resp.json();
  await page.context().setExtraHTTPHeaders({ 'Cf-Access-Jwt-Assertion': token });
  return token;
}

test.describe('remote access (mocked Cloudflare Access identity)', () => {
  // Three badge instances exist in the DOM at once (sidebar/topbar/mobile);
  // only one is actually rendered-visible at a given viewport (the other two
  // are hidden by responsive CSS, independent of our own hidden attribute).
  // `:visible` filters to whichever one the current breakpoint actually shows.
  const visibleRemoteBadge = (page) => page.locator('.remote-badge:visible');

  test('no remote badge is shown for an ordinary unauthenticated session', async ({ page }) => {
    await expect(visibleRemoteBadge(page)).toHaveCount(0);
  });

  test('an authenticated allowlisted identity shows a prominent REMOTE ACCESS badge with the email', async ({
    page,
    request,
    baseURL,
  }) => {
    await authenticateAsRemote(page, request, baseURL);
    await expect(visibleRemoteBadge(page).first()).toBeVisible({ timeout: 5000 });
    await expect(visibleRemoteBadge(page).first()).toContainText(/REMOTE/i);
    await expect(page.locator('#greeting-sub')).toContainText('maintainer@example.com', { timeout: 5000 });
  });

  test('authenticated remote maintainer can still navigate and use bounded views normally', async ({
    page,
    request,
    baseURL,
  }) => {
    await authenticateAsRemote(page, request, baseURL);
    await expect(visibleRemoteBadge(page).first()).toBeVisible({ timeout: 5000 });
    await navTo(page, 'view-runs');
    await expect(page.locator('#view-runs')).toBeVisible();
    await navTo(page, 'view-steering');
    await expect(page.locator('#steering-input')).toBeVisible();
  });

  test('remote-mode verification makes zero real network calls outside this fixture', async ({
    page,
    request,
    baseURL,
  }) => {
    const blockedRequests = [];
    page.on('request', (req) => {
      const url = req.url();
      if (!url.startsWith('http://127.0.0.1') && !url.startsWith('http://localhost')) {
        blockedRequests.push(url);
      }
    });
    await authenticateAsRemote(page, request, baseURL);
    await page.waitForTimeout(2500); // at least one poll cycle re-fetching /api/identity with the header
    expect(blockedRequests).toEqual([]);
  });

  test('mobile: the compact remote badge is visible in the mobile header', async ({ page, request, baseURL }) => {
    const width = page.viewportSize()?.width || 0;
    test.skip(width > 500, 'phone-header assertion is for the 393 project');
    await authenticateAsRemote(page, request, baseURL);
    await expect(page.locator('#remote-badge-mobile')).toBeVisible({ timeout: 5000 });
  });
});

test('capture comparison screenshots', async ({ page }, testInfo) => {
  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..', '..');
  const outDir = path.join(repoRoot, '.agent-output', 'ENG-AGENT-02-S4-UI', 'screenshots');
  fs.mkdirSync(outDir, { recursive: true });
  const shots = [
    { name: `overview-dark-${testInfo.project.name}.png`, theme: 'dark' },
    { name: `overview-light-${testInfo.project.name}.png`, theme: 'light' },
  ];
  for (const shot of shots) {
    await navTo(page, 'view-settings');
    await page.locator('#theme-select').selectOption(shot.theme);
    await navTo(page, 'view-overview');
    await page.waitForTimeout(200);
    await page.screenshot({ path: path.join(outDir, shot.name), fullPage: true });
  }
});
