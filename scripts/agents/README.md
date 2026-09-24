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
- **Git-worktree safety.** Write-capable workers (`claude-code`, `codex-build`, `grok-build`, `grok-build-bots`)
  require `--allow-write`, refuse to run on `main`/`master`, and take a
  checkout-wide atomic lock so two write workers never share a checkout; a lock
  with a verifiably dead local PID is recovered on the next run.
- **Read-only Antigravity workers** run with `--mode plan --sandbox`; if the
  working tree changes during the run it is recorded as a `FAIL` contract
  violation.
- **Never in CI.** No GitHub workflow invokes this tooling or any worker CLI.

## Graphify repository intelligence (optional, ENG-AO-01)

`scripts/agents/graph_context.py` is one generic, optional adapter that gives every worker the same
bounded, graph-derived code context. It is **derived, advisory context only**; it never becomes a source of
task status, policy, roadmap, ADRs, validation, or exact-tree acceptance. Truth precedence is always:
(1) the current managed-project source tree, (2) the managed project's AGENTS/policy and maintained docs,
(3) task/program/ADR contracts, (4) Graphify context, (5) agent inference.

- **Optional.** Detected locally (`graphify` on `PATH`, or `OCTAREL_GRAPHIFY_BIN`); never auto-installed;
  `OCTAREL_GRAPHIFY=off` disables it. Absent, stale, or failing Graphify only records a reason
  (`unavailable`, `stale`, `skipped`, `failed-safe`) and AO proceeds with ordinary repository inspection.
- **Local and free.** Only the deterministic AST `graphify update` verb runs, with provider/API credential
  variables scrubbed from its environment. No API billing, LLM or semantic enrichment, or premium
  infrastructure; Graphify's own provider installers/hooks are never run, so no `AGENTS.md`, `CLAUDE.md`,
  provider policy, or `.opencode` file is written.
- **Selected project only.** The graph is built from the explicit selected checkout/worktree (never Octarel's
  cwd) using a snapshot of its non-sensitive files kept under Octarel state
  (`<state dir>/graph-context/<project>/<worktree>/<tree>/`, gitignored, `OCTAREL_STATE_DIR` aware). Secret
  paths (`.env`, `data/`, credentials, keys, ...) are excluded before Graphify sees them; nothing is written
  into the managed repository.
- **Freshness.** A cache is used only when project, repository, worktree, and content-tree identity all
  match. A changed tree is refreshed deterministically (`refreshed`); if it cannot be, the graph is skipped
  (`stale`) and the reason recorded. Graph nodes naming files missing from the current tree are dropped.
- **Delivery.** `orchestrate.run_delegation` and `run_session` append one bounded (~6 KB), redacted,
  clearly-labelled advisory section after the composed policy bundle. It is plain prompt text, so Claude Code,
  Codex, Gemini through OpenCode/Antigravity, native Grok (`grok-build`, `grok-build-review`), and Grok/xAI (or
  any future approved model) launched through OpenCode receive byte-identical context; there is no
  per-provider implementation and routing preference is unchanged. It never alters the preserved policy
  identity used for provider fallback.
- **Evidence.** The status, reason, tree/worktree/project keys, sizes, and `authoritative: false` are recorded
  in the run manifest under `policy_manifest.graph_context`.
- **Exact-tree acceptance** (`scripts/ci/local_gate.py`) never reads graph data; it verifies the real
  candidate tree.

## Controlled Grok bot fan-out (ENG-AO-02)

Normal Grok is unchanged: `grok-build` (write) and `grok-build-review` (read-only) keep `--no-subagents`,
their roles, and their routing priority. Bot mode is a **separate, explicit** route that AO chooses; it is
never automatic and never changes provider routing preference.

```text
AO -> grok-build-bots (primary, only writer, --no-subagents)
        <- concise findings <- 2-3 read-only grok-build-bot workers, in parallel, one level only
```

- **When AO may use it.** Only for `--worker grok-build-bots --bot-task-class <class>` with a class from
  `serious-integration`, `hard-debugging`, or `architecture-high-risk` (the existing usage-policy task
  classes). `mechanical`, `routine`, or no class declines deterministically and runs the ordinary single-agent
  primary; the decline is recorded. The control plane passes the decision as a `bot-class:<class>` task
  command entry. `--scope` (or `--bot-scope` for `session`) bounds every bot.
- **AO owns the fan-out.** The native Grok CLI has no bound on subagents, so AO runs the bots itself as
  registry worker `grok-build-bot` (read-only, `--permission-mode plan --sandbox read-only --no-subagents
  --disable-web-search`). Count is `min(max_bots, 3, applicable roles)`: `dependency` (blast radius),
  `tests` (regression risk), and `impact` (UI/API/docs, matched on whole path segments, only when such a surface is in scope). Registry
  loading fails closed if a `subagents` block allows more than 3 bots, more than one level, a non-read-only
  or itself fan-out-capable bot, API billing, or a mechanical/routine class.
- **One writer.** Bots run inside the primary's checkout write lock and finish (and are verified) before the
  primary starts, so they never overlap the writer. A working-tree/index/HEAD change during the fan-out is a
  contract violation: findings are discarded and the primary is `BLOCKED`. Bots never commit, push, merge, or
  touch branches, receive no provider/API credentials (minimal worker environment), and inherit path/secret
  filtering: scope paths and graph slices are filtered with the same sensitive-path rules.
- **Graphify.** Each bot gets its own bounded slice of the ENG-AO-01 graph (`dependency`, `tests`, or
  `impact` focus) instead of the broad section. If Graphify is unavailable/stale the bots still run with only
  their non-sensitive scope paths (recorded as `graph_context.supplied: false` with the reason). Graphify stays
  advisory below the source tree, policy, and task contracts.
- **Failure handling.** Each bot gets exactly one attempt and a bounded timeout (`bot_timeout_seconds`,
  capped at one third of the run timeout, with a join deadline: a bot that outlives its bound is recorded as `TIMEOUT` and the run fails closed (`BLOCKED`, primary not launched, lock released) rather than starting the writer beside a possibly-live bot. Each bot leads its own process group, which is killed on timeout, on a leftover child, and on that fail-closed path; the primary's budget is reduced by fan-out time). A failed/timed-out bot is
  recorded (with a failure category such as `quota`) and never retried, never re-run on a stronger model, and
  never falls back to an API key. If no bot yields findings the primary continues single-agent.
- **Evidence.** `policy_manifest.bot_fanout` in the normal run manifest records the decision and reason, task
  class, bot count and roles/scopes, provider/model, start/finish/result, failure reason, Graphify status per
  bot, token usage when the transport reports it, and per-bot logs under `bots/` in the primary's evidence
  directory. The dashboard reuses the existing stage `subagents` field and attempt details. No second
  history system exists.
- **Not review.** Bot findings are unverified assistance to the primary. `independent_review` is always
  `false`; provider-diverse independent review and exact-tree acceptance are separate, unchanged stages.
- **Transport seam.** `scripts/agents/subagents.py` keeps launch/parse behind `BotTransport`; native Grok
  (`grok-cli`) is the only transport today. A bot-capable OpenCode/xAI transport can `register_transport`
  and reuse planning, bounds, read-only verification, Graphify scoping, and evidence unchanged.

## Dynamic OpenCode model catalog and free fallback (ENG-AO-03)

`opencode-free-review` and `opencode-free-tests` are read-only workers whose model is **not** in `workers.json`: it is
chosen per run from the installed OpenCode's *current* catalog by `scripts/agents/model_catalog.py`, so a model that
appears or disappears in OpenCode needs no repository edit. Configured workers (the Gemini Flash Lite routes,
Antigravity, native Grok, Codex, Claude) keep their identities and priority; the pool ranks (`free-dynamic`) behind the
configured free/supplemental workers and ahead of metered/subscription ones, so it is only ever a fallback.

```text
opencode models --verbose + providers list  ->  classify cost/auth + capability  ->  bounded qualification (cached)
      ->  eligible free pool  ->  run_delegation (model, evidence)   |   dispatch (cache-only admission)
```

- **Discovery** uses OpenCode's local machine-readable commands (`--version`, `models --verbose`, `providers list`,
  `debug agent`); never the interactive UI, `auth.json`, a generation, or (unless `--remote`) the network. Absent,
  unauthenticated, failing, empty, or unparseable OpenCode yields status `absent`/`unavailable`/`empty`/`unparseable`
  and no candidates. The snapshot is cached under `<state>/opencode-models/catalog.json` (30 minute freshness; refresh
  with `python -m scripts.agents.model_catalog refresh`; dispatch admission uses a snapshot up to 24 h old).
- **Cost/auth classification** comes from OpenCode's own metadata, never a display name: `free-opencode` = OpenCode Zen
  transport whose declared cost is zero on every price field (tier descriptors ignored); `subscription` = provider
  authenticated by OAuth; `metered` = API-key credential or a priced Zen model; `unknown` = missing/incomplete cost,
  unreadable credentials, or no reported credential. Only `free-opencode` can enter automatic free fallback.
- **Capability filter**: `active` status, tool calling, text in/out, context >= 128k tokens.
- **Qualification** (`qualify_model`) runs once per free, capable model and profile (`review` = diff/doc review with the
  `reviewer` preset, `tests` = tests/search/research with the `tester` preset): the preset exists, `opencode debug
  agent` shows edit/task (and bash for review) denied, the model launches with a scrubbed environment, no default-agent
  substitution, no denied tool or billing/API-key signal, the probe workspace is unchanged, and the response satisfies
  the strict review contract (`scripts/ci/review_contract.py`) or the tester instruction. Non-free models are never
  launched. Results are cached in `qualifications.json`, keyed by OpenCode version, model identity (id, transport URL,
  prices, context, status), profile, and preset content: 7 days when qualified, 1 hour when not. A run may probe at most
  two unqualified candidates; dispatch and the dashboard only read the cache.
- **Provider diversity**: a candidate whose provider/model/family names match the avoided vendor (for example `xAI` or
  `grok`) is rejected even if free; `--avoid-provider` (set by the supervisor from the task) carries the constraint into
  the run. If no diverse qualified model exists the stage blocks; nothing weaker is substituted.
- **No silent escalation**: a quota/rate-limit/context/outage failure puts that model in a one-hour cooldown so the next
  attempt takes another free model; no stronger, subscription, or paid model is used because of a failure, there is
  no in-run retry, and no API-key billing, purchase, or top-up exists in this path. An explicit `--model` on a pool
  worker goes through the same gate.
- **Evidence** is the normal run manifest: `policy_manifest.model_selection` records the catalog version/refresh time and
  fingerprint, every candidate (id, display name, provider, cost class and reason, qualification and reason,
  selected/rejected and why), and the run's `actual_provider`/`actual_model` (`OpenCode Zen` / the discovered id).
- **OpenCode + Grok/xAI (and any other provider)** is discovered by the same generic code (OAuth-authenticated xAI is
  `subscription`). It is shown in the catalog but not auto-routed and never replaces native `grok-build`/
  `grok-build-review`; a future explicit route or `BotTransport` can select it from the same catalog.
- **Dashboard/API**: `GET /api/opencode-models` returns the cached inventory (per model: cost class, credential, context,
  capability, per-profile state `qualified-free-fallback` / `free-untested` / `unqualified` / `cooling-down` /
  `subscription-backed` / `metered-ineligible` / `unknown-cost-ineligible` / `incapable`) beside the configured
  OpenCode workers (`configured-worker` vs `dynamic-pool-worker`). It never spawns a process.

Deferred: promoting subscription-backed OpenCode models (for example OpenCode+xAI) to an automatic route needs its own
explicit authorization mapping; discovered models are never write-capable.

## Native Grok/Codex model freshness (ENG-AO-04)

`grok-build*` and `codex-*` keep stable identities, roles, permissions, and routing. Their `default_model`
(`grok-4.6`, `gpt-5.6-sol`) is the **last verified baseline**; `native_model_family` (`grok` / `codex`) lets the
installed native CLI advance the *effective* model without a registry edit (`scripts/agents/native_models.py`):

    native CLI -> local enumeration -> candidate -> compatibility verification -> verified record -> effective model

- **Enumeration** uses only local, non-billable commands: `--version`, `--help`, `grok models`,
  `codex login status`, `codex debug models`. No prompt or generation is ever sent (`probe: "none"`), and API-key
  variables are stripped from the child environment so the answer describes the existing subscription/session only.
- **Candidate** = the newest *strictly newer, same-tier* model the CLI itself enumerates (`grok-4.6` -> `grok-4.7`;
  `gpt-5.6-sol` -> a newer `gpt-N-sol`). Variant ids such as `-build-fast` are ignored, and newer models of another
  tier (for example `gpt-6-astra`) are reported (`newer_other_tier`) but never adopted: choosing a stronger tier is
  routing policy, not freshness.
- **Verified** only when the session is authenticated (Grok: `grok models` confirms a logged-in session; Codex:
  `codex login status` exits 0 and reports the ChatGPT subscription session; negated phrases and API-key modes are
  ineligible), every flag in every worker template of the family is still documented by the CLI's `--help`, and each
  route still carries exactly its required guards (review: read-only sandbox, plus plan mode for Grok; implementation: its
  worktree sandbox, default permission mode, and isolated worktree; Grok: `--no-subagents`, web search off) with no
  conflicting later value and no write or approval-bypass flag.
- **Otherwise the configured model stays in force** and the reason is recorded (`unverified` / `unavailable`: CLI
  missing, unauthenticated, malformed or incomplete listing, missing flag, stale configured model). Nothing raises into
  AO startup, and the previous verified model is never removed because a newer one exists.
- **OpenCode is not native truth.** Newer OpenCode labels ("Grok 4.7", "GPT-6 Sol") are recorded as `opencode_hints`
  only; the native identifier is whatever the native CLI enumerates. #7's free reviewer fallback is untouched.
- **Where it applies.** `Worker.effective_model` is the verified model when a valid record exists, else `default_model`;
  it feeds command building, run/session planning, `grok-build-bot` (so `grok-build-bots` bots inherit the verified Grok
  model), and status/dashboard rows. An explicit `--model` still wins. Every live `run`/`session` re-verifies the family
  first (CLI version, sign-in state, model list, flags, permission shape), so a logged-out or API-key session can never
  ride a cached advance; managed admission may reuse a record up to five minutes old (invalidated by a changed CLI
  `--version`, configured default, or worker shape). A dry run and the dashboard only read the cache, and a cached
  advance is ignored for a worker whose flags/permissions changed since verification or once older than seven days.
- **Evidence.** Each run manifest's `policy_manifest.actual_model` is the model actually requested, and
  `policy_manifest.model_freshness` records provider, configured default, actual/effective model, candidate, last
  verified model, status, reason, CLI version, verification timestamp, and fingerprint. No second history exists.
- **Operate.** `python -m scripts.agents.native_models refresh|list`; `GET /api/native-models` (read-only, never runs a
  CLI); `/api/models` rows carry `effective_model` and `native_model_family`.

Limit: enumeration by the authenticated CLI is entitlement evidence, not a generation, so a model the CLI lists but the
account cannot actually run is only discovered on first use (the run fails visibly; no API-key or paid fallback occurs).

## Continuous overnight advancement (ENG-AO-05)

A durable **overnight session** keeps advancing one managed project through its eligible tasks within explicit
bounds. It is a wrapper over the existing Runbook / Quick Start / `advance_after_success` / acceptance /
event machinery, not another queue: `scripts/agents/control_plane/overnight.py`, one `overnight_sessions` SQLite
table, and `overnight` events in the ordinary event log.

- **Start (AO or operator).** `python -m octarel overnight start <project> --duration 10h [--max-tasks 5]`
  (project defaults to the selected one), the Runs view card, or `POST /api/commands/overnight_start`
  (`{"duration": "10h", "max_tasks": 5, "project_id": "..."}`). Control: `overnight pause | resume |
  stop-after-current | stop`, `overnight status`, `GET /api/overnight`. AO never builds branches/worktrees/runbooks:
  the session resolves task, branch/worktree, runbook, provider and stages through the normal Quick Start path.
- **Daemon is the authority.** `octarel run` (the daemon) calls `overnight.tick` every poll. The dashboard, API and
  CLI only create/control the durable record; a session does nothing while no daemon runs.
- **Current truth, never a list.** Nothing is planned in advance. Before every product task the daemon fetches, fast-
  forwards a clean default-branch checkout, then resolves the next eligible task from the project's own current task
  source with the same gates as `advance_after_success` (dependencies, owner decisions, stale/already-accepted, active
  writer, provider route). If merged truth changed the order, the new truth wins.
- **One writer.** At most one product runbook of the project is active. A foreign active runbook/write task (or a DRAFT this session left behind
  by an interrupted start) stops the session (`active_writer_conflict`); a run is never adopted: the session only owns a runbook its own guarded start returned. A live run for the next task it cannot prove is its own (someone else's, or its own orphaned by a crash before the pointer was saved) is left running untouched and the session stops (`active_writer_conflict`), so a crash can never cause a takeover or a duplicate. Read-only reviewer/test stages and heavy jobs keep the normal scheduler caps;
  the session never changes concurrency. While a session owns a runbook, generic `auto_advance` is suppressed so the
  successor cannot start before bounds and merge are checked.
- **Acceptance is not weakened.** A task counts only when its runbook is `SUCCEEDED` with acceptance stage `DONE`
  and no failed stage (focused tests, independent review, exact-tree gate, checkpoint, PR readiness all ran in the
  normal pipeline). `OWNER_ACTION_REQUIRED`, `BLOCKED`, `FAILED` and `CANCELLED` stop the session; there is no retry loop.
- **Merge authorization.** Octarel never auto-merges by default. A session may merge only if the operator passes
  `--authorize-merge` / `authorize_merge` for that session **and** the project opted in with capability
  `overnight_merge=true`; the merge then uses the existing gated `prepare_merge` -> confirmed `merge` operations with all
  their blockers, and must be verified in repository truth. Otherwise, after acceptance the session stops as
  `owner_action_required` ("merge the PR, then resume"); `resume` re-checks truth and continues. The dashboard cannot
  grant merge authorization.
- **Bounds.** `--duration` (up to 48h) and optional `--max-tasks`; whichever is reached first ends the session
  `COMPLETE` (`deadline_reached` / `task_limit_reached`). At the deadline no new task starts; a task already in flight is
  never killed: the session goes `STOPPING` (`stop_requested=deadline`, recorded as an event) and finishes the task to
  its normal checkpoint (through merge handling when authorized), then completes. (If the deadline elapses during the pre-task fetch, that one task may still begin and is run to its checkpoint.)
- **States.** `ACTIVE`, `PAUSED` (holds counting, merge and next-task; the in-flight runbook is untouched),
  `STOPPING` (stop-after-current or deadline), `STOPPED` (needs attention: `owner_action_required`, `merge_blocked`,
  `blocking_failure`, `provider_unavailable`, `policy_blocked`, `project_unavailable`, `active_writer_conflict`,
  `stale_repository_state` (the pre-task fetch/fast-forward failed, or the canonical checkout of a project with an `origin` is dirty or off its default branch and is left untouched: it never advances from stale truth),
  `operator_stop`, `operator_cancelled`, `internal_error`), `COMPLETE` (`deadline_reached`, `task_limit_reached`,
  `no_eligible_task`). `stop` (confirmed) cancels the current runbook through the existing owned-process termination.
  Owner-action stops (awaiting merge, owner decision) are resumable while inside the deadline.
- **Restart safety.** The session stores counters and pointers only (`session_id`, project + identity fingerprint,
  start/deadline, `max_tasks`, `accepted_count`, `current_runbook_id`, `last_accepted`, stop reason, state). On daemon
  start live sessions record a recovery event; a live runbook keeps its worker and is never relaunched, an accepted
  task is not double-counted, and truth is refreshed before any further task. A project that is removed, disabled or
  whose root/remote/default branch changed fails the session closed.
- **No paid/API fallback.** The session names no provider and never touches billing switches; the route gate rejects
  API-billing and optional-overflow workers, so an only-paid route stops the session `provider_unavailable`.
  Graphify context (ENG-AO-01), Grok bot fan-out (ENG-AO-02), qualified free OpenCode reviewers (ENG-AO-03) and current
  native model versions (ENG-AO-04) apply inside the normal runbooks exactly as without a session.
- **macOS.** Octarel does not change power settings. Keep the Mac awake (for example `caffeinate -i` started by you)
  for uninterrupted execution; if the daemon or machine restarts, restart `octarel run` and recovery continues safely.

## Worker registry (`workers.json`)

Routing (cheapest capable worker first):

| Role | Preference |
| --- | --- |
| `primary-implementation` | `claude-code` → `codex-build` → `grok-build` (explicit/policy-gated fallback) |
| `secondary-implementation` | `grok-build` → `claude-code` → `codex-build` (policy-gated) |
| `mechanical-testing`, `focused-tests` | `opencode2-gemini-flash-lite` → `antigravity-focused-tests` → `opencode-free-tests` (runtime free pool) |
| `impact-search` | `antigravity-impact-search` → `opencode2-gemini-flash-lite` → `opencode-free-tests` (runtime free pool) |
| `doc-drift-review` | `antigravity-doc-drift` → `opencode-free-review` (runtime free pool) |
| `diff-review` | `antigravity-diff-review` → `opencode2-gemini-flash-lite-review` → `opencode-free-review` (runtime free pool) → `grok-build-review` → `codex-review` |
| `overflow` | `deepseek-overflow` (disabled by default) |
| `bot-implementation`, `bot-investigation` | `grok-build-bots`, `grok-build-bot` (explicit only; never in another route) |

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
