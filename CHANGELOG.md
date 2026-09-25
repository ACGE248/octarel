# Changelog

All notable changes to Octarel are documented in this file.

## [Unreleased]

### Added

- ENG-AO-05: continuous overnight project advancement. A durable, daemon-advanced session (`octarel overnight start <project> --duration 10h [--max-tasks N]`, Runs-view card, `overnight_*` commands, `GET /api/overnight`) resolves each next eligible task from the managed project's current repository truth after every verified merge, runs one write-capable task at a time through the normal runbook/acceptance path, honours duration/task-limit/pause/stop-after-current bounds, survives daemon restart without duplicating work, never uses paid/API fallback, and merges only with explicit per-session authorization plus the project's `overnight_merge` capability (otherwise stops at owner action).
- CPX-07 public-safety scanner, clean-room acceptance, and public-history snapshot helper
- `octarel project`, `octarel providers`, `octarel public-safety`, and `octarel service canonicalize-state`
- Maintained install, architecture, provider, release, state, and publication docs
- OCTAREL-OPS-02: after an accepted run, Octarel re-reads the managed project's current repository truth and records the next eligible task (or the exact reason advancement stopped: blocked, no eligible task, owner decision required). Shown on the Runs view; `auto_advance=true` project capability opts in to starting it automatically; `POST /api/runbooks/{id}/advance` re-evaluates.
- OCTAREL-TEST-01: `npm run test:dashboard:matrix` (`scripts/ci/playwright_matrix.py`) runs the seven Control Center viewport projects as isolated bounded-parallel lanes (default concurrency 3, `OCTAREL_PLAYWRIGHT_MATRIX_CONCURRENCY`); the local gate's Control Center phase uses it. Fixed the Agent Activity viewer for symlinked repo roots, made its specs order-independent, and raised modal sheets above the sidebar so they are clickable at 768px.
- OCTAREL-UI-02: Control Center polish pass. Overview now shows the controlled project, an Active Work panel (task, stage, agents, elapsed, branch, next stage, blocking reason) and a Needs-attention list using one glyph+label language (owner decision, auth, provider, blocked, failed test/review, quota, stale repository); Live Workflow gains a Plan→Implement→Test→Review→Integrate phase track and non-colour-only stage states; Providers are compact rows (auth, availability with reason, model, role, usage, fallback); the Agent Activity viewer is a log console (line numbers, stderr/warn markers, follow-tail, search count) with structured Details/Evidence; sidebar selection, contrast, focus and reduced-motion refined. No backend or API changes.
- OCTAREL-UI-03: UI polish phase 2. Task/run cards lead with ID, title, state, stage, agent and elapsed and disclose scheduling/run detail; "View Active Run" now lands on the run (selected, focused, details open); the Telemetry table stacks into labelled rows on phones; the Agent Activity log states that stdout/stderr are recorded together, labels only wrapper lines and error/warning keyword matches, and preserves raw text exactly; mobile page padding follows the measured session bar and nav; Workflow reflows into a wrapping grid instead of compressing at ~1280px; Terminal, Settings, Worktrees and System get a deliberate hierarchy with feedback and warnings-only emphasis. Fixes a stale-socket terminal state race. No backend changes.
- ENG-AO-02: controlled Grok bot fan-out. New explicit `grok-build-bots` route (separate from unchanged `grok-build`/`grok-build-review`, which keep `--no-subagents`) lets AO, for serious-integration/hard-debugging/architecture-high-risk work, run at most three one-level read-only `grok-build-bot` workers in parallel with per-bot scoped Graphify slices before the single-writer primary; failures are recorded once with no retry, escalation, or API billing, bot output is not independent review, and evidence reuses the run manifest and dashboard `subagents` field. A `BotTransport` seam keeps future OpenCode/xAI bots compatible.
- ENG-AO-01: optional, provider-neutral Graphify repository intelligence. One generic adapter (`scripts/agents/graph_context.py`) builds a local, deterministic, tree-matched graph of the selected managed project under Octarel state and appends bounded advisory context to the normal worker bundle for Claude, Codex, Gemini/OpenCode, native Grok, and Grok through OpenCode; status is recorded in the run manifest. Never installs Graphify, uses API/LLM enrichment, rewrites policy files, or affects exact-tree acceptance.

### Changed

- Control Center Overview: grouped the sidebar's 12 flat nav items into Monitor/Work/Configure/System sections, added connector chevrons between Live Workflow pipeline stages on desktop/tablet, collapsed the Provider Usage row's not-exposed tiles into one summary chip, added a blocked-status border accent to the status cards, and added a leading icon to the steering command bar. No API, data, or route changes.

### Fixed

- ENG-AO-07: single-writer runbook advancement. A daemon and a dashboard reconciling together could both advance one runbook into acceptance and run the exact-tree gate concurrently in one managed worktree (colliding `npm ci` runs left `node_modules` half-installed and falsely BLOCKED the runbook). `reconcile_runbooks` and `POST /api/runbooks/{id}/advance` now take a durable per-runbook `flock` lease (crash-safe, re-read after winning, non-owners observe and skip), and the dashboard only observes while a live daemon holds `daemon-authority.lock`.
- ENG-AO-06: unattended daemon launch and non-blocking provider probes. `octarel daemon start|status|stop` launches `octarel run` detached (own session, no stdin/terminal, unbuffered log, pid file, duplicate/non-canonical refusal, `SUSPENDED` detection); `octarel run` ignores SIGTTIN/SIGTTOU; every provider CLI probe now runs with closed stdin, its own session and a hard timeout via `scripts/agents/probe.py`.
- Write the launchd dashboard pid file under `~/Library/Application Support/Octarel/` (boot volume). launchd cannot write onto an external-volume state directory; loopback health remains the listener authority.

### Security

- Public history is produced with `scripts/ci/prepare_public_history.py` as a fresh `git init` of `git archive HEAD`, so unpublished merge-commit emails and historical operator paths never enter the public object store.

## [0.1.0] — unreleased

First intended public tag. Create `v0.1.0` only after every gate in
`docs/PUBLICATION.md` is green on the public-safe snapshot.
