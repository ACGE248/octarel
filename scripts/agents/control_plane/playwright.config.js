// ENG-AGENT-02-S4: responsive Playwright coverage for the developer-only
// orchestrator control-center dashboard. Deliberately separate from the
// product's root playwright.config.js / ui-audit/ harness: this dashboard is
// its own localhost service (default port 8877; tests use 8899 to avoid
// colliding with a developer's real running daemon) with its own lifecycle,
// independent of the OctaScene app's port/process per docs/engineering/ENG-AGENT-02.md.
import { defineConfig } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
// Anchored at the repo root (not this config's own directory) so results
// land under the already-gitignored top-level /test-results/ tree.
// OCTAREL-TEST-01: the matrix runner (scripts/ci/playwright_matrix.py) gives every
// concurrent viewport lane its own results directory and fixture root through
// these variables. Unset, behavior is exactly the serial single-fixture default.
const laneResults = process.env.OCTAREL_PLAYWRIGHT_RESULTS_DIR;
const laneFixtureRoot = process.env.OCTAREL_CONTROL_CENTER_FIXTURE_ROOT;
const resultsRoot = laneResults || path.join(repoRoot, 'test-results', 'control-center');
const port = process.env.OCTAGES_CONTROL_CENTER_TEST_PORT || '8899';
const origin = `http://127.0.0.1:${port}`;
const pythonBin = process.env.OCTAGES_PYTHON_BIN || process.env.OCTAREL_PYTHON_BIN || 'python3';
const pythonArgs = [];

export default defineConfig({
  testDir: './dashboard-tests',
  outputDir: `${resultsRoot}/artifacts`,
  timeout: 30_000,
  expect: { timeout: 5_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [
    ['line'],
    ['html', { outputFolder: `${resultsRoot}/html`, open: 'never' }],
    ...(laneResults ? [['json', { outputFile: `${resultsRoot}/report.json` }]] : []),
  ],
  use: {
    baseURL: origin,
    actionTimeout: 5_000,
    navigationTimeout: 10_000,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: [
      pythonBin, ...pythonArgs, 'scripts/agents/control_plane/dashboard-tests/serve_fixture.py', '--port', port,
      ...(laneFixtureRoot ? ['--root', JSON.stringify(laneFixtureRoot)] : []),
    ].join(' '),
    cwd: repoRoot,
    url: `${origin}/api/overview`,
    reuseExistingServer: false,
    timeout: 30_000,
    stdout: 'pipe',
    stderr: 'pipe',
  },
  // ENG-AGENT-02-S7 (issue #97) acceptance matrix: iPhone 16 Pro (393x852) is
  // the explicit primary mobile target; 390x844/430x932 cover the adjacent
  // iPhone/Android "standard" and "plus/max" size classes; 768x1024 covers
  // tablet; 1280x800/1440x900/1920x1080 cover desktop. Run the full matrix
  // with a bare `playwright test`, or select one viewport for fast local
  // iteration with `--project=<name>`.
  projects: [
    { name: 'desktop-1920', use: { viewport: { width: 1920, height: 1080 } } },
    { name: 'desktop-1440', use: { viewport: { width: 1440, height: 900 } } },
    { name: 'desktop-1280', use: { viewport: { width: 1280, height: 800 } } },
    { name: 'tablet-768', use: { viewport: { width: 768, height: 1024 }, isMobile: true, hasTouch: true } },
    { name: 'android-430', use: { viewport: { width: 430, height: 932 }, isMobile: true, hasTouch: true } },
    { name: 'iphone-390', use: { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } },
    { name: 'iphone-16-pro-393', use: { viewport: { width: 393, height: 852 }, isMobile: true, hasTouch: true } },
  ],
});
