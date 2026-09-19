# Local multi-provider delegation tooling (`ENG-AGENT-01`) + orchestrator control center (`ENG-AGENT-02`)

Read [`AGENTS.md`](../../AGENTS.md) first and compose the relevant canonical policies indexed by
[`.agents/README.md`](../../.agents/README.md). This directory is **development tooling only**.
It extends the existing control plane and is not a prose-policy source.

## Installing/running the Control Plane independently (`ENG-CP-02`)

`scripts/agents/requirements.txt` (runtime) and `scripts/agents/requirements-dev.txt` (adds `pytest`/`ruff`)
are the Control Plane's own dependency manifests — a small, statically-audited set (`fastapi`, `uvicorn`,
`httpx`, `psutil`, `PyJWT`) distinct from the repository root's `requirements*.txt`, which also pull in
OctaScene's full application/media/provider-SDK stack. Installing only `scripts/agents/requirements-dev.txt`
into a fresh virtualenv is sufficient to run `python -m scripts.agents.orchestrator dashboard` and this
directory's own tests; it does not require the OctaScene application to be importable. See
[`docs/engineering/ENG-CP-02.md`](../../docs/engineering/ENG-CP-02.md) for the full dependency/runtime
isolation contract, including the documented temporary `scripts.ci` intra-repository compatibility dependency.

## Managed projects (`ENG-CP-03`)

The Control Plane manages one or more **projects** (repositories), exactly one of which is selected at a time.
`control_plane/project_registry.py` owns the durable registry (a `projects` table built on `ENG-CP-01`'s
`ProjectContract`) and the add/select/edit/disable/remove/validate operations; the Control Center's sidebar
selector and Add Project dialog are its UI. An existing single-project installation auto-migrates on first
start: OctaScene is registered from its own `control_plane/octascene_project.py` adapter and every existing
task, runbook, worktree and history record is adopted into it — deterministically and idempotently, with no
manual reconfiguration.

A registry row records only *where and how* to obtain repository truth (checkout path, GitHub remote, default
branch, repository-relative policy/roadmap/task-source paths, an argv validation command). Roadmap, task,
ledger and policy **content** is never copied into Control Plane state; it is read from the managed repository
on every request. Provider definitions and genuinely global settings stay global rather than being duplicated
per project. See [`docs/engineering/ENG-CP-03.md`](../../docs/engineering/ENG-CP-03.md) for the registry
schema, selection semantics, global-vs-project-scoped audit, migration behavior, and the path/remote-access
security rules.

## Generic adapters (`ENG-CP-04`)

Task discovery, policy loading, and repository-owned validation go through selected-project adapters in
`control_plane/task_sources.py`, `control_plane/policy_loader.py`, and `control_plane/validation_adapter.py`.
Generic Control Plane code does not parse the OctaScene Video Editor ledger, assume `AGENTS.md`, or
hard-code `scripts/ci/local_gate.py`. OctaScene remains a compatibility adapter in
`control_plane/octascene_project.py`. See [`docs/engineering/ENG-CP-04.md`](../../docs/engineering/ENG-CP-04.md).

## What it does

`python -m scripts.agents.orchestrate` records a redacted, auditable manifest for
**one** delegated worker invocation and stores the detailed evidence under the
git-ignored `.agent-output/<TASK-ID>/<worker>/<RUN-ID>/` tree so CLI truncation
cannot lose it and retries cannot overwrite earlier evidence. Stdout gets only a
compact pointer block:

```
TASK: ENG-AGENT-01
ROLE: focused-tests
STATUS: PASS
SYSTEM: OpenCode2
PROVIDER: Google
MODEL: google/gemini-3.5-flash-lite
EXIT: 0
SUMMARY: .agent-output/ENG-AGENT-01/opencode2-gemini-flash-lite/<RUN-ID>/summary.md
MANIFEST: .agent-output/ENG-AGENT-01/opencode2-gemini-flash-lite/<RUN-ID>/manifest.json
LOG: .agent-output/ENG-AGENT-01/opencode2-gemini-flash-lite/<RUN-ID>/logs/run.log
```

## Commands

| Command | Purpose |
| --- | --- |
| `list-workers` | Print the registry and whether each worker's CLI is installed. |
| `route --role <role>` | Show the cheapest-capable-first worker order for a role. |
| `run --task <ID> --worker <name> --role <route> --why <reason> --scope <path> [--contract <path>] [--workflow <name>] [--fallback-reason <reason>] ... -- <prompt>` | Compose canonical policy and plan/execute one tightly scoped worker. |

## Managed dispatch and admission (`ENG-AGENT-10`)

Use `python -m scripts.agents.orchestrator dispatch --id <instance> --task-ref <stable-id>
--owner-ref <issue-or-task> --role <role> ... -- <prompt>` for an operator-managed create-and-launch action.
The command uses the same `control_plane.commands` → `control_plane.dispatch.managed_admit` path as the
Control Center, Quick Start, Runbooks, and daemon loop. It checks stable-ID ownership before launch, selects
only an eligible non-API/non-overflow route, records scoring and alternatives, and applies real dependency
waves plus global/write/read/provider/heavy caps before the existing Supervisor launch.

`python -m scripts.agents.orchestrator set-concurrency --global N --write N --read N --provider N --heavy N`
updates any supplied cap through that shared command layer. `GET /api/dispatch` exposes the same durable
claims, decisions, waves, caps, load, worktree, alternatives, and queue/block reasons without provider calls.
See [`docs/engineering/ENG-AGENT-10.md`](../../docs/engineering/ENG-AGENT-10.md).

## Guarantees

- **One worker per attempt.** A direct delegated `run` never silently starts another worker. A durable
  implementation Runbook may start exactly one next same-role attempt after a recoverable provider/session
  failure, but only through the policy gates below; paid/API/overflow fallback remains forbidden.
- **Explicit policy composition.** `policy.py` deterministically injects universal + relevant core +
  canonical role + workflow + selected provider + bounded contracts. Root `CLAUDE.md` and provider-native
  auto-discovery are not required. The same manifest records capability, context size, exclusions, and
  fallback reason.
- **One immutable evidence directory per attempt.** A timestamp/PID/random run
  id prevents retries from overwriting earlier manifests or logs.
- **Redaction and environment isolation.** The recorded command and captured
  output are passed through `redaction.py` before being written; the worker
  subprocess receives a minimal environment allowlist rather than unrelated
  credential-bearing variables.
- **Strict validation.** Task ids must match `ENG-AGENT-01`-style references;
  `--scope` paths are resolved and rejected if they escape the repo or touch
  `data/`, `.env`, or any secret-bearing file. Scope is a prompt/context bound,
  not a filesystem read jail; the wrapper also uses each CLI's safe sandbox,
  minimal environment, and post-run worktree fingerprint.
- **Git-worktree safety.** Write-capable workers (`claude-code`, `codex-build`, `grok-build`)
  require `--allow-write`, refuse to run on `main`/`master`, and take a
  checkout-wide atomic lock so two write workers never share a checkout; a lock
  with a verifiably dead local PID is recovered on the next run.
- **Read-only Antigravity workers** run with `--mode plan --sandbox`; if the
  working tree changes during the run it is recorded as a `FAIL` contract
  violation.
- **Never in CI.** No GitHub workflow invokes this tooling or any worker CLI.

## Worker registry (`workers.json`)

Routing (cheapest capable worker first):

| Role | Preference |
| --- | --- |
| `primary-implementation` | `claude-code` → `codex-build` → `grok-build` (explicit/policy-gated fallback) |
| `secondary-implementation` | `grok-build` → `claude-code` → `codex-build` (policy-gated) |
| `mechanical-testing`, `focused-tests` | `opencode2-gemini-flash-lite` → `antigravity-focused-tests` |
| `impact-search` | `antigravity-impact-search` → `opencode2-gemini-flash-lite` |
| `doc-drift-review` | `antigravity-doc-drift` |
| `diff-review` | `antigravity-diff-review` → `opencode2-gemini-flash-lite-review` → `grok-build-review` → `codex-review` |
| `overflow` | `deepseek-overflow` (disabled by default) |

The existing **OpenCode2 + Gemini 3.5 Flash Lite** focused-test path is preserved
and remains first choice for mechanical testing. Antigravity supplements
it with bounded, repository-scoped, read-only workers (focused tests / simple
diagnosis, repository impact search, documentation-drift review, diff review) that
follow `AGENTS.md`: they never weaken tests, never read secrets, never make live
product-provider calls, and never commit or push. Codex may remain the
orchestrator, while its registry entries are explicitly separated by capability:
subscription-authenticated `codex-review` is read-only and `codex-build` is the
explicit write-capable implementation fallback. Each entry references one provider policy and declares
canonical allowed roles, auth mode, repository-data authorization mode, API-billing prohibition, and
worktree-isolation requirement; explanatory behavior lives under `.agents/providers/`.

ACGE248/octages persistently pre-authorizes configured Google and xAI providers for minimized, redacted,
task-relevant repository-data reuse. `workers.json` records that grant once at provider level, and policy
composition, managed dispatch, fallback, testing, review, Runbooks, managed CLI, and Control Center projections
all resolve the same effective authorization. This removes repeated per-task repository-data prompts only;
worker enablement/authentication/health, role/capability, permission profile, worktree/write lock, cost,
concurrency, secret exclusions, and separate live/billable/API authorization remain mandatory.

## Usage governance (`ENG-AGENT-03`)

Runbooks classify work before selecting a provider/model and persist their
decision record in SQLite. Unattended runs default to Codex `conserve`, are not
Codex-auto-eligible unless explicitly configured, and have a per-run maximum
invocation count. `balanced` excludes mechanical work; `unrestricted` still
respects the count. Quota/rate/auth/outage failures may reroute only to an equivalent eligible role;
context overflow first requires minimization, and safety/policy/worktree/authorization failures block.
These failures never justify stronger-model escalation by themselves. Control Center's premium override
is confirmed, reasoned, and audited.

`ENG-AGENT-06` makes this recovery operationally explicit. Failed tasks retain route-specific attribution
(worker, execution system, provider, known model, category, sanitized reason, and provider-sourced reset data),
while the recommended retry route is separately identified. Recoverable quota/rate/auth/outage failures may
automatically select the next same-role eligible worker under the existing Codex/invocation, permission,
repository-authorization, worktree-lock, and no-API-billing gates. Safety/policy/secret/worktree failures block.

`ENG-AGENT-07` completes that automatic path. The supervisor finalizes each route in durable `route_history`;
reconciliation excludes every attempted/reserved worker, checks current CLI/provider eligibility, preserves the
original policy manifest, reserves any bounded Codex invocation before launch, and starts at most one replacement
on the same task/Runbook/branch/worktree. Restart sees the persisted decision and cannot duplicate it. The Runs
view displays the failed attempt, automatic transition, replacement status, and exact blocked reasons while
retaining the original `ENG-AGENT-06` attribution. See `docs/engineering/ENG-AGENT-07.md`.

`ENG-AGENT-08` feeds known recoverable pre-launch unavailability into that same path. The preferred worker is
persisted as an `UNAVAILABLE` attempt before the existing selector runs, so an eligible same-role replacement can
start automatically and a restart cannot loop back to the unavailable route. The same task, Runbook, contracts,
branch/worktree, permission profile, budget, and writer lock are retained; no API-billed or safeguard-bypassing
route is enabled. See `docs/engineering/ENG-AGENT-08.md`.

`ENG-AGENT-09` activates that path for the explicit Continue Video Editor Quick Start. Its prepared Runbook uses
the balanced Codex policy, is automatically eligible for at most one subscription-authenticated Codex invocation,
and still delegates replacement choice to the canonical role-first selector at launch time. Provider state,
repository authorization, permission compatibility, budget, and exclusive worktree ownership remain mandatory;
API billing and paid overflow remain disabled. The Prepared Run labels its worker as preferred and shows this
fallback allowance. Custom Run is a separate manual configuration surface: its preset and worker values never
edit or affect a prepared Quick Start. See `docs/engineering/ENG-AGENT-09.md`.

Automatic fallback decisions use a durable compare-and-set claim. This matters for the dashboard, where the
explicit Start Development request and the background reconciliation tick may observe the same new provider
failure concurrently: only the claim winner may select or launch a replacement, while the other path returns the
winner's persisted result. Restart remains at-most-once and can reconcile a legacy live-replacement/failed-Runbook
presentation without launching another process.

The Usage & Routing panel reports classification, policy, eligibility,
invocations, exact/estimated/unknown token quality, route history, and durable
escalation state. Context manifests describe only bounded relevant material and
exclude `.env`, `data/`, credentials, secrets, keys, and their contents.

## Authoritative local gate (`ENG-GH-03`)

GitHub Actions is not used for validation. Stage the complete candidate and run
`python scripts/ci/local_gate.py --docs-reviewed`; T2/T3 high/critical work also supplies
`--independent-review-provider <provider> --independent-review-evidence <.agent-output/.../manifest.json>`.
Evidence under `.local-gate/` is
bound to the Git tree. Selection records T0/T1/T2/T3, review level, and product versus Control Center
audit scope. `prepare_merge` and the Control Center Local Gate card
reject absent, failed, or stale evidence. Full operating detail is in
`docs/engineering/GITHUB_DEVELOPMENT_WORKFLOW.md`.

ENG-AGENT-05 makes those tiers control actual breadth through `scripts/ci/test_impact.py`: T1 owned selectors
plus anchors, T2 subsystem groups, and T3 complete applicable suites, with unknown executable paths escalating.
`python3 -m scripts.ci.environment --prepare --browser` prepares/verifies a worktree using local caches only;
the final gate never installs. `local_gate.py --dry-run` exposes readiness, reviewer health, concrete selectors,
the phase DAG, isolated dynamic ports, and historical estimates before launch. Green phase evidence may be
reused only across identical command/environment/relevant-input fingerprints, and immutable run history records
reuse, invalidation, duration, and rerun reasons. The Control Center Local Gate view reads these same facts and
makes zero provider calls.

The orchestration entry for the OpenCode2 tester is read-only: it does not
require `--allow-write`, and any worktree/index/HEAD change makes the run fail.
Use a separately authorized write-capable worker for repairs.

If the verified Antigravity CLI (`agy`) is not installed, its workers report
`UNSUPPORTED` and the run stops. `agy 1.1.28` exposes an `agents` discovery
command, but it returned no repository agents in this installation, so the
registry uses the verified default agent plus the wrapper's bounded role prompt
instead of claiming unsupported named-agent discovery. The authenticated
ENG-AGENT-01 capability check selected `gemini-3.8-flash-medium`, medium effort,
plan mode, and sandboxing; the bounded diff-review worker reviewed only the
redacted scoped diff supplied in its prompt, returned `Ready`, exited zero, and
changed no files.

## Orchestrator daemon + control center (`ENG-AGENT-02`)

`scripts/agents/orchestrator.py` extends the tooling above with a durable local
process (`scripts/agents/control_plane/`) that survives a chat session ending:
a SQLite-backed task/provider/worktree/event store, adaptive routing, a
dependency-safe concurrency-capped scheduler, process supervision, restart
recovery, and an independent developer-only control-center dashboard. See
[`docs/engineering/ENG-AGENT-02.md`](../../docs/engineering/ENG-AGENT-02.md)
for its historical decision record; `ENG-AGENT-03` and `ENG-GH-03` own current
usage routing and local-gate authority.

State lives under the git-ignored, root-anchored `.orchestrator-state/`
directory — **separate from** `ENG-AGENT-01`'s `.agent-output/` audit-evidence
tree. Operator holds, stop-after-current, and the write-worker limit are also
stored there so dashboard/CLI commands reach the independent daemon. Shared
SQLite-connection access is serialized, cancelled child outcomes are
preserved, disappeared recovered PIDs fail closed, and blocked tasks await
explicit operator action rather than retrying continuously. `psutil`
(`requirements-dev.txt` only, never the shipped app) powers the dashboard's
resource panel.

### Control Center finish (`ENG-AGENT-02-S8`)

Agent display names, descriptions, best-use guidance, provider identity,
models, intensity and capabilities now come from `workers.json`; machine IDs
remain stable internal keys. Provider cards distinguish configured state from
locally detectable availability and expose route type, registered agents/models,
quota visibility and spend-safety text without treating an installed CLI as
proof of quota.

The Terminal view is a real PTY-backed zsh session rooted to the resolved
repository worktree. It activates `.venv` only when `.venv/bin/python` exists,
inherits a minimal non-secret environment, accepts no client-supplied cwd, and
records only open/close lifecycle metadata—not commands or output. Direct use
requires a loopback peer and loopback Origin; remote WebSocket handshakes must
pass the existing Cloudflare Access JWT audience/issuer/signature/allowlist plus
exact Origin check. Runtime xterm.js and licensed provider SVGs are cached
locally; see `control_plane/dashboard/ASSET_LICENSES.md`.

S8 final integration hardening also persists pause/stop/write-limit controls
across processes, serializes shared SQLite access, preserves cancelled results,
reconciles recovered PIDs that later exit, leaves blocked work for explicit
operator action, disambiguates scope-like prompt text, rejects detached-HEAD
write workers, and fully redacts quoted multiword secret assignments.

Final acceptance on that hardened tree passed the production build, 187 V2
frontend tests (one intentional skip), Ruff, compilation, 2,475 Python tests,
and the seven-project Control Center matrix (279 passed, eight intentional
skips). The approved free OpenCode2/Google Gemini 3.5 Flash Lite fallback review
returned `READY` with no findings and passed 133 focused tests after the
preferred Antigravity review route hit its documented headless read-permission
limitation. The existing Cloudflare Access-authenticated
`dev.octascene.com` session had already verified human names plus remote PTY
connection, benign command output, and reconnect.

### Control Center operations (`ENG-AGENT-02-S9`, issue #103)

S9 implementation is complete and locally verified in PR #104. The Worktrees
view discovers every real repository worktree and
separates read-only discoveries from explicitly managed/adopted entries. The
same developer-only surface adds fixed-argv Git operations with durable stage
results, structured development history, doc-derived roadmap metrics labelled
`DERIVED`, managed `make run` lifecycle controls, and sanitized metadata for
commands entered through the Control Center PTY only. It never imports ordinary
shell history and hides secret-bearing command lines completely.

Failed Runbooks retain their original durable Runbook and Task records. The
Runs view shows the sanitized wrapper/worker reason plus recovery actions and
allows an operator to retry the same task, branch and worktree with another
eligible write worker. The wrapper always executes from the Control Center
checkout and receives the target feature worktree through an explicit
`--repo-root`, so a feature branch created before `scripts/agents/` existed can
still run. A bounded post-exit pipe drain preserves the wrapper's redacted
manifest pointer even when pipe EOF arrives just after process exit.
`codex-build` is the explicit write-capable fallback: it requires
`codex login status` to report ChatGPT subscription authentication, uses the
workspace-write sandbox, receives no API-key environment variables, and is
separate from the unchanged read-only `codex-review` worker.

The earlier statement that the dashboard only probes port 8765 is superseded:
it may manage the one local development process it starts itself, but will not
stop an externally started process or anything outside the canonical repository
launch path. Port 8877 remains loopback-only and independent.

The Overview's Live Workflow is backed by `/api/workflow`, a presentation-ready
read model grouped from durable Tasks and Runbooks. It never infers active
workers from provider configuration and never fabricates progress, ETA,
current files/actions, results, or sub-agent counts. Desktop, tablet, and phone
layouts share the same normalized payload and stage-detail interaction; only
their presentation changes at the responsive breakpoints.

### Commands

Invoke as a module — like `scripts.agents.orchestrate` — so its relative
imports resolve: `python -m scripts.agents.orchestrator <command>`. A bare
`python scripts/agents/orchestrator.py ...` fails with `ImportError: attempted
relative import with no known parent package`.

| Command | Purpose |
| --- | --- |
| `run [--dry-run] [--once] [--poll-interval SECONDS]` | Run the scheduling loop in the foreground. `--dry-run` (also available as a bare top-level flag, e.g. `python -m scripts.agents.orchestrator --dry-run`) plans one iteration against an ephemeral in-memory store: zero writes to `.orchestrator-state/`, zero subprocesses started. |
| `dashboard [--host 127.0.0.1] [--port 8877]` | Serve the control-center dashboard: a second, fully independent FastAPI/uvicorn process. |
| `status` | Print current tasks and provider states. |
| `enqueue --id ID --task-ref REF --role ROLE --worker NAME [--kind write\|read\|heavy] [--priority N] [--dep ID]... [--scope PATH]... [-- PROMPT...]` | Create a new durable task. Never launches anything by itself. |
| `start --id ID [--dry-run]` | Launch a pending/queued task now. |
| `pause` / `resume` / `stop --id ID` | Hold, release, or cancel a task. |
| `stop-after-current` | Stop scheduling new tasks once current work finishes. |
| `provider-enable` / `provider-disable` / `provider-drain --name NAME` | Change a provider's routing eligibility. Refuses to enable a `NOT_CONFIGURED` catalog-only row. |
| `probe --name NAME` | Check whether a worker's CLI is installed/authorized. Never a billable call. |
| `set-max-writers --count N` | Change the write-worker concurrency cap. |
| `prioritize --id ID --priority N` / `defer --id ID` | Reorder the queue. |

Every mutating verb above is dispatched through one shared
`control_plane/commands.py` layer that both the CLI and the dashboard's
`POST /api/commands/<verb>` endpoint call — the two surfaces cannot drift.

### Provider states and routing

`control_plane/provider_state.py` seeds one row per `workers.json` worker
(`AVAILABLE`/`DISABLED`/`NOT_CONFIGURED` by CLI presence and registry
`enabled`), plus a fixed catalog of providers with **no adapter in this
repository**: GLM, DeepSeek (a catalog-only row, distinct from
the registry's own already-disabled `deepseek-overflow` route), OmniRoute, and
Cheaper Inference — all seeded `NOT_CONFIGURED`, `configured=False`, no CLI, no
network calls. OpenAI/Codex is **not** in this catalog: `codex-review` is a
real, subscription-authenticated registry worker (Codex CLI via the
maintainer's existing ChatGPT session — never an API key). Every worker's
`Worker.availability_reason()` (ENG-AGENT-02-S7) reports one of `AVAILABLE`/
`DISABLED`/`CLI_MISSING`/`NOT_AUTHENTICATED`/`LAUNCH_ENVIRONMENT_ERROR`/
`API_ONLY_NOT_AUTHORIZED`/`UNSUPPORTED`/`CONFIGURATION_ERROR`/`CATALOG_ONLY` instead of collapsing every
unavailability cause into `NOT_CONFIGURED`; `reconcile_provider_states()` adds
a worker newly introduced to `workers.json` into an already-existing
`.orchestrator-state` database on the next daemon start, without disturbing
an existing row's runtime state. `control_plane/routing.py` computes five adaptive modes
(single-primary, even-split, cost-weighted, capability-priority decay,
failover-chain), each restricted to `configured` + routable
(`AVAILABLE`/`BUSY`) providers only — a `NOT_CONFIGURED` or `DISABLED` provider
is structurally absent from every mode's percentage map, never present at
`0%`.

ENG-AGENT-11 (issue #131) split the previously-conflated `NOT_AUTHENTICATED`
signal in two. `claude auth status` (or any worker's declared `auth_check`)
failing to spawn, timing out, or returning no parseable output with no
explicit negative status line is now `LAUNCH_ENVIRONMENT_ERROR` — a
launcher/sandbox session-visibility defect — never `NOT_AUTHENTICATED`, which
is reserved for a probe that actually ran and understood a negative answer.
A worker may additionally declare `cli.launch_probe` (today, only
`claude-code`, using `claude -p "Reply with exactly: CLAUDE_OK"`): when the
cheap metadata check is ambiguous, this one real, harmless, non-billable
authoritative invocation gets the final say, and a successful reply reports
`AVAILABLE` even though the metadata heuristic disagreed. `LAUNCH_ENVIRONMENT_ERROR`
is a `control_plane.provider_state` `STATE_LAUNCH_ENVIRONMENT_ERROR` row —
non-routable like `NOT_CONFIGURED`, so same-role fallback selection is
unaffected, but visible to the operator (`/api/attention`) as its own
truthful category rather than a bare "not configured" or a false "sign in
again."

### Control-center dashboard guarantees

- **Independent process, independent port.** `dashboard_api.py` never imports
  `app/` or `frontend/`. S9 may start, stop, or restart only the canonical
  local `make run` process that its lifecycle manager owns and validates by
  PID creation time, command line, repository cwd, and port state; an external
  process on `127.0.0.1:8765` remains informational and cannot be stopped.
- **Stays up regardless of the product app.** The dashboard keeps serving
  every read/control endpoint whether the OctaScene app is running, stopped,
  or restarting.
- **Zero AI calls on ordinary refresh.** Every dashboard read is a local
  SQLite/registry/`git`/`psutil` query; no polling path makes a provider or
  model network call.
- **Reconciles what it explicitly launches, schedules nothing on its own.**
  `orchestrator.py run` remains the only automatic queue scheduler — the
  dashboard never starts pending/queued work by itself. But a task started
  through the dashboard's own `POST /api/commands/start` or
  `/api/steering/execute` is a real subprocess owned by that dashboard
  process's `Supervisor`, so a FastAPI `lifespan`-scoped background loop calls
  `ctx.supervisor.poll_once()` roughly every 0.5s for the life of the dashboard
  process and is cancelled on shutdown, reconciling only that Supervisor's own
  process table (never another one's).
- **No build step.** `control_plane/dashboard/index.html`/`app.js`/`styles.css`
  are static, polling the JSON read endpoints client-side every 1–5s.
- **Exact-tree local-gate read model.** The System view reads the same
  `.local-gate/latest.json` evidence enforced by merge preparation and marks a
  different-tree result stale; it never queries a hosted status rollup.
- **Canonical operational state.** `RUNNING` alone is Active; pending/queued, paused, needs-attention, and
  historical records have separate counts. Worktrees expose deterministic lifecycle classifications and a
  preview-first cleanup action. Only adopted `FINISHED_CLEAN` worktrees can be removed, using Git's worktree
  lifecycle; dirty, active, canonical, manual, and unknown entries are protected. Clean idle `main` is shown as
  `IDLE — no active candidate` without manufacturing passing gate evidence.

### Telemetry (`ENG-AGENT-02-S3`)

`control_plane/telemetry.py` adds token accounting (`EXACT` from a worker's
structured JSON, else `UNKNOWN`; `ESTIMATED` only when a caller explicitly
labels one), execution-route classification (`SUBSCRIPTION`/`API`/`FREE`/
`UNKNOWN` from `workers.json` `cost_class`), cost accounting that never bills
subscription/free usage, hard budget checks feeding a new `COST_BLOCKED`
provider state (`provider-cost-block`/`provider-cost-clear` CLI verbs), a
`claude_5h`/`claude_weekly` quota reader that is honestly `UNKNOWN` until a
real local source exists, and `git`-only checkpoint status per worktree. The
dashboard's `/api/telemetry` endpoint composes all of it; see
[`docs/engineering/ENG-AGENT-02.md`](../../docs/engineering/ENG-AGENT-02.md)
for details and what remains deferred within this slice.

### Deferred to later slices

Adaptive-routing-mode visualization beyond the `/api/routing` preview,
workload target-vs-actual distribution, and durable historical spend/token
persistence remain open within the telemetry area. Natural-language
steering, deep mobile polish (safe-area insets, drawers, per-target touch
audit), and Playwright responsive-viewport coverage of the dashboard shipped
in Slice 4 (`ENG-AGENT-02-S4`), not this slice.

### Runbooks / unattended development sessions (`ENG-AGENT-02-S5`)

`control_plane/runbooks.py` adds a durable `Runbook` model (new `runbooks`
SQLite table alongside the existing task/provider/worktree/event tables),
five saved presets (Overnight Development, Finish PR, Test & Fix, UI Polish,
Review Only), and a constructed unattended-session prompt embedding the
editable objective, mandatory safety profile, stop conditions, and stop
deadline. Starting a runbook creates exactly one ordinary `Task`
(`launch_mode=session`) that the existing `Scheduler`/`Supervisor` own like
any other task — no second scheduler. A new `orchestrate.py session`
subcommand is a whole-worktree sibling to `run` for exactly the case that
tool's mandatory `--scope` narrowing cannot express; `session` (never `run`)
is also the only verb that can carry a non-default `permission_profile`.
Overnight Development's `repo_configured_auto` profile launches `claude-code`
with `--dangerously-skip-permissions` instead of `--permission-mode manual`
so a genuinely unattended session never blocks on a headless tool-permission
prompt. S9 later adds a separately declared workspace-write profile for the
subscription-authenticated `codex-build` fallback; both worker/profile pairs
are re-validated at every create/update/start/retry boundary and neither is
automatic. The dashboard gained a `Runs` surface with a `Run
Overnight` quick action, live runbook cards, and a morning-report viewer.
See [`docs/engineering/ENG-AGENT-02.md`](../../docs/engineering/ENG-AGENT-02.md)
for full detail.

### Secure remote Control Center (`ENG-AGENT-02-S6`)

`control_plane/remote_access.py` lets the maintainer operate this same
dashboard from another computer or phone at `https://dev.octascene.com`
through Cloudflare Tunnel + Access, without exposing port 8877 publicly —
the dashboard's own bind stays `127.0.0.1:8877` (`orchestrator.py` now
refuses a non-loopback `--host` without an explicit override). Disabled by
default; set `OCTAGES_ORCH_REMOTE_ENABLED=1` plus the Cloudflare team
domain/audience/allowlist to turn it on (full variable list and Cloudflare
setup walkthrough:
[`docs/engineering/ENG-AGENT-02-S6-remote-access.md`](../../docs/engineering/ENG-AGENT-02-S6-remote-access.md);
zero-secret config templates:
[`control_plane/cloudflare/`](control_plane/cloudflare/)). A request only
enters the remote code path if it itself carries a Cloudflare Access
identity assertion, which is then cryptographically verified (signature,
audience, issuer, expiry) against Cloudflare's published keys before
anything is trusted — an ordinary local request, or a local script spoofing
the same header, is unaffected either way. CSRF/origin protection and
in-process rate limiting apply to remote state-changing requests, which are
also audited (identity/verb/target/result) through the existing events
table; a `REMOTE ACCESS` badge shows the verified identity in the UI.

### Control Center UX rework (`ENG-AGENT-02-S7`)

`control_plane/quickstart.py` adds a "Continue Video Editor" Quick Start
resolver: `GET /api/quickstart` re-parses
[`docs/video-editor/IMPLEMENTATION_STATUS_V2.md`](../../docs/video-editor/IMPLEMENTATION_STATUS_V2.md)'s
ledger tables on every call for the first `pending` task in document order
— never a hard-coded task ID — and checks whether a matching git worktree
already exists. `control_plane/provisioning.py`'s `provision_worktree()` is
the one write-shaped git action the Control Center can trigger on the
operator's behalf (only a path/branch the process itself derived, only a
sibling directory of the repository root); the new `quickstart_start`
command (reachable through the existing `/api/commands/{verb}` endpoint,
same `dry_run` flag as `runbook_start`) re-resolves the option fresh,
provisions a worktree only if needed, then creates and starts the Runbook
through the unchanged Slice 5 path. NL steering gained one bounded pattern
mapping a development-intent phrase that names the Video Editor onto the
same `quickstart_start` verb, with `/api/steering/parse` attaching the same
resolved Prepared Run the Quick Start button shows.

Every `Worker` gained `check_auth()`/`availability_reason()`
(`AVAILABLE`/`DISABLED`/`CLI_MISSING`/`NOT_AUTHENTICATED`/
`API_ONLY_NOT_AUTHORIZED`/`UNSUPPORTED`/`CONFIGURATION_ERROR`/
`CATALOG_ONLY`), replacing the old binary configured/not-configured signal;
passive callers (provider-state seeding, `--dry-run`) never probe (no
subprocess spawn), only the explicit `probe` command does. Claude's probe
requires the local `claude.ai` subscription result from `claude auth status`;
binary presence alone and API-key auth are both insufficient. A real
`codex-review` worker and the separate write-capable `codex-build` fallback
(both Codex CLI via the maintainer's existing ChatGPT session, never an API
key) replaced the permanent `openai-codex`
catalog-only row this fixed. `telemetry.claude_account_facts()` reads
`claude auth status --json` (a local, non-billable status query) for
subscription/auth-method/organization facts, exposed via `GET /api/usage`
(60s server-side cache); every fact without a real local source is reported
`NOT_EXPOSED`, never fabricated. The UI gained a Quick Start row + Prepared
Run card ahead of a collapsed "Advanced Settings" manual form, one shared
original provider-icon badge registry everywhere a provider appears,
collapsed per-card provider actions on mobile, and a state-aware
Start/Pause/Resume/Stop control bar. Terminal tasks are excluded from Active
Tasks and never receive Pause/Resume/Stop controls. Playwright coverage
(`control_plane/playwright.config.js`) expanded from 2 to 7 viewport
projects (desktop 1920/1440/1280, tablet 768x1024, mobile
430x932/390x844/393x852 iPhone-16-Pro-class).

The completed issue #97 surface exposes six Quick Start cards. Continue Video
Editor and Continue OctaScene resolve the next eligible maintained editor task
from the canonical ledger; Finish Current PR, Focused Test & Fix, and Review
Current Diff resolve the checkout's real branch/worktree; Custom Run opens the
collapsed Advanced Settings form. Every executable key is re-resolved by the
server before start. Prepared Run includes the program/task/dependency and
implementer/tester/reviewer/check/PR expectations needed to start without
knowing orchestrator internals. Overview changes to View Active Run while a
Runbook is active.

Provider cards pair locally defined execution-system and provider glyphs and
show the real worker model/intensity/roles/route, availability reason, last
probe, and running count. `/api/usage` returns only source-labelled facts:
Claude CLI account facts when exposed, `LOCAL_ACCOUNTING` running-task counts,
and explicit provider-specific `NOT_EXPOSED` values for session/window/weekly/
reset/rate/balance/spend/limit metrics that the configured CLIs do not reveal.
`/api/flow` has one canonical per-task stage list; task/provider/local-git facts
are populated, while tests/review/PR/CI stay `NOT_REPORTED` until a real source
exists. The dashboard never converts planned Runbook phases into fake progress.
